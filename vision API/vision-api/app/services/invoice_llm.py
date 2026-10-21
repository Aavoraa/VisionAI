"""Phase 2: a local reasoning model reads the invoice fields too, as a cross-check on the rules.

The model (Qwen3-VL-2B via llama.cpp by default) reads the OCR text of the header, the
non-item table rows and the footer, and returns the fields that need understanding rather
than parsing: invoice number and date, seller and buyer, GSTINs, place of supply, delivery
note, payment terms, and the grand total as a check. llama.cpp constrains decoding to a JSON
schema, so the output always parses. Items and tax amounts are not asked for: the rules
parse the table exactly and cross-check the tax summary themselves (a 2B model was seen to
confuse "2.50 %" rates with CGST amounts).

``merge`` combines the two readings per field, never letting the model silently win:

* both agree          -> the value is confirmed (status unchanged, note says so)
* only the model      -> its value, always ``check`` ("found only by the reasoning model");
                         dropped (with a warning) if it is not in the OCR text (invented),
                         repeats another field's value (read from the wrong box), is printed
                         boilerplate ("E & OE", amounts in words, captions), is not GSTIN-shaped
                         for a GSTIN, or is a name / address without a word in it (a code or
                         hash) or an address that is only a date
* only the rules      -> the rule value, unchanged
* they disagree       -> the rule value, ``check``, with both readings in the note
"""

import asyncio
import base64
import datetime as dt
import difflib
import io
import json
import re
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
from PIL import Image

from app.models.base import ModelError, ModelUnavailableError
from app.schemas.invoice import Extracted, Invoice
from app.services.invoice_extraction import (
    _ITEM_START,
    find_gstin,
    is_label,
    parse_amount,
    parse_date,
    parse_otsl,
)
from app.services.invoice_normalize import format_state, normalize_invoice_number

if TYPE_CHECKING:
    from app.services.invoice_bands import Box, TableLocation

LOCATOR_MAX_SIDE = 1280  # enough to see table rows; larger only slows the request down


def _locator_image(page: bytes) -> str:
    """The page as a base64 JPEG no larger than ``LOCATOR_MAX_SIDE`` (boxes are relative)."""
    with Image.open(io.BytesIO(page)) as opened:
        image = opened.convert("RGB")
    image.thumbnail((LOCATOR_MAX_SIDE, LOCATOR_MAX_SIDE))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return base64.b64encode(buffer.getvalue()).decode("ascii")

# Field -> what the model should copy. Order matters: it is the order of the JSON output.
FIELD_GUIDE: dict[str, str] = {
    "invoice_number": "the invoice number exactly as printed under or next to 'Invoice No.'",
    "invoice_date": "the invoice date as printed under or next to 'Dated' / 'Invoice Date'",
    "vendor_name": "the seller's company name; the 'for <name>' line above 'Authorised Signatory' is the most complete",
    "vendor_address": "the seller's street, area, city and PIN lines, comma separated (not the name, GSTIN, phone, e-mail or state)",
    "vendor_gstin": "the seller's GSTIN (15 characters, in the seller block)",
    "buyer_name": "the buyer's company name (first line under 'Buyer' / 'Bill to')",
    "buyer_address": "the buyer's street, area, city and PIN lines, comma separated (not the name, GSTIN, phone, e-mail or state)",
    "buyer_gstin": "the buyer's GSTIN (15 characters, in the buyer block)",
    "place_of_supply": "the value after a printed 'Place of Supply' label; null if that label is not printed",
    "delivery_note": "a short reference written in the 'Delivery Note' box; null if the box is empty",
    "payment_terms": "terms written in the 'Mode/Terms of Payment' box; null if the box is empty",
    "total_amount": "the grand total amount on the 'Total' row (a money amount, not a quantity or rate)",
}
AMOUNT_FIELDS = {"total_amount"}
IDENTIFIER_FIELDS = {"invoice_number", "vendor_gstin", "buyer_gstin"}
TEXT_FIELDS = {"vendor_name", "vendor_address", "buyer_name", "buyer_address", "place_of_supply", "delivery_note", "payment_terms"}

SYSTEM_PROMPT = (
    "You extract fields from the OCR text of an Indian GST tax invoice. Copy each value exactly "
    "as it appears in the text; do not translate, calculate or guess. Invoices print many labels "
    "with empty boxes: when a label is directly followed by another label, its box is empty, so "
    "the field is null. 'E & OE', declarations and amounts in words are never field values. "
    "If a field is not present, use null."
)


def response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {name: {"type": ["string", "null"]} for name in FIELD_GUIDE},
        "required": list(FIELD_GUIDE),
        "additionalProperties": False,
    }


def document_text(header_text: str, tables: list[str], footer_text: str) -> str:
    """Readable OCR text for the model: header, table rows except line items, footer."""
    rows: list[str] = []
    for table in tables:
        for row in parse_otsl(table):
            cells = [c for c in row if c]
            if cells and not _ITEM_START.match(cells[0]):
                rows.append(" | ".join(cells))
    return "\n\n".join(
        part
        for part in (
            f"[Top of the invoice]\n{header_text.strip()}",
            "[Table boxes and totals]\n" + "\n".join(rows),
            f"[Bottom of the invoice]\n{footer_text.strip()}",
        )
        if part.split("\n", 1)[-1].strip()
    )


class LlmFieldMapper:
    """Calls a llama.cpp server (OpenAI-compatible chat API with JSON-schema output)."""

    def __init__(
        self, name: str, base_url: str, *, timeout_seconds: float, transport: httpx.MockTransport | None = None
    ) -> None:
        self.name = name
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=httpx.Timeout(timeout_seconds), transport=transport
        )

    async def read_fields(self, document: str) -> tuple[dict[str, str | None], int, int]:
        """(fields, input tokens, output tokens). Raises ``ModelError`` on any failure."""
        guide = "\n".join(f"- {name}: {meaning}" for name, meaning in FIELD_GUIDE.items())
        payload = {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Fields:\n{guide}\n\nOCR text:\n<<<\n{document}\n>>>"},
            ],
            "temperature": 0.0,
            "max_tokens": 700,
            "response_format": {"type": "json_schema", "json_schema": {"name": "invoice", "schema": response_schema()}},
            "stream": False,
        }
        try:
            response = await self._client.post("/v1/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise ModelUnavailableError("reasoning model not reachable") from exc
        if response.status_code != 200:
            raise ModelError(f"reasoning model returned HTTP {response.status_code}")
        try:
            body = response.json()
            fields = json.loads(body["choices"][0]["message"]["content"])
            usage = body.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelError("reasoning model returned malformed output") from exc
        if not isinstance(fields, dict):
            raise ModelError("reasoning model returned malformed output")
        clean = {name: (str(v).strip() or None) if v is not None else None for name, v in fields.items() if name in FIELD_GUIDE}
        return clean, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))

    async def locate_table(self, page: bytes) -> tuple["TableLocation | None", int, int]:
        """Where the item table's caption and total rows are on ``page`` (a vision request).

        Returns (location or None, input tokens, output tokens). Raises ``ModelError`` on failure.
        """
        from app.services.invoice_bands import LOCATE_PROMPT, parse_location

        text, used_in, used_out = await self._look(page, LOCATE_PROMPT, 200)
        return parse_location(text), used_in, used_out

    async def locate_regions(self, page: bytes) -> tuple[dict[str, "Box"], int, int]:
        """Boxes of the seller, buyer, invoice-details and totals blocks (see ``REGIONS``)."""
        from app.services.invoice_bands import REGIONS_PROMPT, parse_regions

        text, used_in, used_out = await self._look(page, REGIONS_PROMPT, 300)
        return parse_regions(text), used_in, used_out

    async def locate_field(self, page: bytes, what: str) -> tuple["Box | None", int, int]:
        """Box of one printed value, described by ``what`` ("the seller's GSTIN ...")."""
        from app.services.invoice_bands import FIELD_PROMPT, parse_field_box

        text, used_in, used_out = await self._look(page, FIELD_PROMPT.format(what=what), 120)
        return parse_field_box(text), used_in, used_out

    async def _look(self, page: bytes, prompt: str, max_tokens: int) -> tuple[str, int, int]:
        """One vision request about ``page``: (answer text, input tokens, output tokens)."""
        image = await asyncio.to_thread(_locator_image, page)
        payload = {
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
                {"type": "text", "text": prompt},
            ]}],
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "stream": False,
        }
        try:
            response = await self._client.post("/v1/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise ModelUnavailableError("reasoning model not reachable") from exc
        if response.status_code != 200:
            raise ModelError(f"reasoning model returned HTTP {response.status_code}")
        try:
            body = response.json()
            text = body["choices"][0]["message"]["content"]
            usage = body.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelError("reasoning model returned malformed output") from exc
        return str(text), int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))

    async def close(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------


def _typed(name: str, raw: str, document: str) -> Any | None:
    """The model's text as the field's type, normalised like the rule values."""
    if name in AMOUNT_FIELDS:
        return parse_amount(raw)
    if name == "invoice_date":
        return parse_date(raw)
    if name in ("vendor_gstin", "buyer_gstin"):
        # Not GSTIN-shaped (an e-mail address, a hash from another slip): no value at all.
        found = find_gstin(raw.replace(" ", ""))
        return found.value if found else None
    if name == "place_of_supply":
        return format_state(raw)
    if name == "invoice_number":
        return normalize_invoice_number(raw.split()[0], document)[0]
    return " ".join(raw.split())


_BOILERPLATE = re.compile(
    r"^e\.?\s*&\s*o\.?\s*e\.?$"          # "E & OE": errors and omissions excepted
    r"|\bonly\.?$"                            # amounts in words: "... Nine Only"
    r"|^(?:declaration|authori[sz]ed signatory|this is a computer generated)",
    re.I,
)


def _boilerplate(raw: str) -> bool:
    """Printed notices, amounts in words and field captions are never field values."""
    return bool(_BOILERPLATE.search(raw.strip())) or is_label(raw)


_WORD = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{3,}(?![A-Za-z0-9])")
_ONLY_DATE = re.compile(r"\d{1,2}[-/. ](?:\d{1,2}|[A-Za-z]{3,9})[-/. ]\d{2,4}")


def _implausible(name: str, raw: str) -> str | None:
    """Why the model's text cannot be this name or address (None: it can)."""
    if name not in ("vendor_name", "buyer_name", "vendor_address", "buyer_address"):
        return None
    if not _WORD.search(raw):  # "7de9e32d8ea6...": a code or hash, no word in it
        return "has no word in it"
    if name.endswith("_address") and _ONLY_DATE.fullmatch(raw.strip()):
        return "is a date"
    return None


def _flat(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _agree(name: str, a: Any, b: Any) -> bool:
    if isinstance(a, Decimal) or isinstance(a, dt.date):
        return a == b
    if name in TEXT_FIELDS:
        x, y = _flat(str(a)), _flat(str(b))
        return x == y or difflib.SequenceMatcher(None, x, y).ratio() >= 0.9
    return _flat(str(a)) == _flat(str(b))


def _grounded(name: str, raw: str, value: Any, document: str) -> bool:
    """Does the model's value actually occur in the OCR text (not invented)?"""
    flat_doc = _flat(document)
    if isinstance(value, Decimal):
        digits = re.sub(r"[^0-9]", "", raw)
        return bool(digits) and digits in re.sub(r"[^0-9]", "", document.replace("\n", " "))
    if name in ("vendor_address", "buyer_address"):
        parts = [p for p in re.split(r"[,\n]", raw) if p.strip()]
        return bool(parts) and all(_flat(p) in flat_doc for p in parts)
    return _flat(raw) in flat_doc


def merge(invoice: Invoice, model_fields: dict[str, str | None], document: str, model_name: str) -> tuple[Invoice, list[str]]:
    """Cross-check rule values with the reasoning model's; see the module docstring."""
    updates: dict[str, Extracted[Any]] = {}
    warnings: list[str] = []
    disagreements = 0
    known = {_flat(str(getattr(invoice, n).value)) for n in Invoice.model_fields if n != "items" and getattr(invoice, n).value is not None}
    for name in FIELD_GUIDE:
        rule: Extracted[Any] = getattr(invoice, name)
        raw = model_fields.get(name)
        value = _typed(name, raw, document) if raw else None
        if value is None:
            if raw and name in ("vendor_gstin", "buyer_gstin"):
                warnings.append(f"{model_name} suggested {raw!r} for {name}, which is not a GSTIN; ignored")
            continue  # the model found nothing usable: the rule reading stands as it is
        if rule.value is None:
            if _boilerplate(raw):  # type: ignore[arg-type]
                warnings.append(f"{model_name} suggested {raw!r} for {name}, which is printed boilerplate; ignored")
            elif reason := _implausible(name, raw):  # type: ignore[arg-type]
                warnings.append(f"{model_name} suggested {raw!r} for {name}, which {reason}; ignored")
            elif not _grounded(name, raw, value, document):  # type: ignore[arg-type]
                warnings.append(f"{model_name} suggested {raw!r} for {name}, which is not in the OCR text; ignored")
            elif _flat(str(value)) in known:
                warnings.append(f"{model_name} suggested {raw!r} for {name}, the value of another field; ignored")
            else:
                updates[name] = Extracted.check(value, f"found only by {model_name}; verify", raw)
        elif _agree(name, rule.value, value):
            updates[name] = rule.model_copy(update={"note": _join(rule.note, f"confirmed by {model_name}")})
        else:
            disagreements += 1
            updates[name] = Extracted.check(
                rule.value, _join(rule.note, f"{model_name} read {value!s}"), rule.source_text
            )
    if disagreements:
        warnings.insert(0, f"{disagreements} field(s) read differently by {model_name}; marked 'check'")
    return invoice.model_copy(update=updates), warnings


def _join(*notes: str | None) -> str:
    return "; ".join(n for n in notes if n)
