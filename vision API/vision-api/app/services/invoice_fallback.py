"""Hybrid extraction: a larger vision model reads the item table when the rules' reading fails.

PaddleOCR-VL + the rules are the most accurate reader overall, but some layouts defeat them
(size matrices, consignment tables, rows split over several lines). For those, a bigger vision
model (Qwen3-VL-8B via llama.cpp by default) reads each page image directly and returns the items
and totals as JSON. Its reading replaces the rules' only when it passes MORE of the validation
checks that the rules' reading failed (line amounts, items adding up, grand total): the checks
decide, not the model. Adopted lines keep their numbers but their line number is ``check``, so
the invoice always goes to a reviewer.

The 8B model does not fit on an 8 GB GPU next to PaddleOCR-VL and the reasoning model, so it is
normally served with only part of its layers on the GPU (see HOW_TO_RUN.txt): slower, but it only
runs for the invoices that need it.
"""

import base64
import io
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from PIL import Image

from app.models.base import ModelError, ModelUnavailableError
from app.schemas.invoice import Extracted, Invoice, LineItem
from app.services.invoice_validation import validate_invoice

FALLBACK_CHECKS = ("line_arithmetic", "items_sum", "line_numbers", "grand_total")
TOTAL_FIELDS = ("total_quantity", "subtotal", "cgst", "sgst", "igst", "round_off", "total_amount")
ITEM_NUMBERS = ("quantity", "rate", "discount_percent", "taxable_value", "tax_rate")
ITEM_TEXTS = ("description", "hsn_sac", "unit")
MAX_SIDE = 1600  # enough to read small print; larger images only cost time
MAX_TOKENS = 6000

PROMPT = """You read the item table and the totals of an Indian GST purchase invoice (or a transport freight bill) from the image and return JSON.
Rules:
- Copy text and numbers exactly as printed. Do not translate, guess or calculate.
- A value that is not printed is null. Amounts are plain numbers without commas or currency signs.
- items: one entry per numbered line of the item table, in order. line_number is the printed serial number.
  description = the item's description as printed (for a size-matrix garment invoice: the style name, one entry per
  style). quantity = the line's quantity; unit as printed (pcs, nos ...); rate = the printed rate (null if the line
  prints several rates, e.g. one per size); discount_percent = the line's discount %; tax_rate = the line's GST %;
  taxable_value = the line's amount after discount, before GST.
- Quantities printed with four decimals ("15,0000" or "15.0000") are 15, not 150000.
- On a transport freight bill, quantity = the packages (Pkgs) count, not the charged weight.
- Then the totals block at the bottom: total_quantity, subtotal (taxable total before GST), cgst, sgst, igst
  (amounts), round_off, total_amount (the grand total / net amount payable).
"""


TOTALS_PROMPT = """Read ONLY the totals block at the bottom of this Indian GST invoice page and return JSON:
total_quantity, subtotal (taxable total before GST), cgst, sgst, igst (amounts), round_off, total_amount (the grand
total / net amount payable). Copy numbers exactly as printed, without commas or currency signs; null when not printed.
"""
TOTALS_MAX_TOKENS = 400


def totals_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {name: {"type": ["number", "null"]} for name in TOTAL_FIELDS},
            "required": list(TOTAL_FIELDS)}


def response_schema() -> dict[str, Any]:
    def nullable(kind: str) -> dict[str, Any]:
        return {"type": [kind, "null"]}

    item_keys = ["line_number", *ITEM_TEXTS, *ITEM_NUMBERS]
    item = {
        "type": "object",
        "properties": {key: nullable("string" if key in ITEM_TEXTS else "number") for key in item_keys},
        "required": item_keys,
    }
    properties: dict[str, Any] = {"items": {"type": "array", "items": item}}
    properties.update({name: nullable("number") for name in TOTAL_FIELDS})  # after the items: the totals come last
    return {"type": "object", "properties": properties, "required": ["items", *TOTAL_FIELDS]}


def _page_jpeg(page: bytes) -> str:
    with Image.open(io.BytesIO(page)) as opened:
        image = opened.convert("RGB")
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class FallbackReader:
    """Reads one page's items and totals with a vision model on a llama.cpp server."""

    def __init__(
        self, name: str, base_url: str, *, timeout_seconds: float, transport: httpx.MockTransport | None = None
    ) -> None:
        self.name = name
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=httpx.Timeout(timeout_seconds), transport=transport
        )

    async def read_page(self, page: bytes, scope: str = "items") -> tuple[dict[str, Any], int, int]:
        """(reading, input tokens, output tokens). ``scope`` "items" = the item table and totals;
        "totals" = the totals block only (a short answer). Raises ``ModelError`` on any failure."""
        totals_only = scope == "totals"
        payload = {
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_page_jpeg(page)}"}},
                {"type": "text", "text": TOTALS_PROMPT if totals_only else PROMPT},
            ]}],
            "temperature": 0.0,
            "max_tokens": TOTALS_MAX_TOKENS if totals_only else MAX_TOKENS,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "invoice", "schema": totals_schema() if totals_only else response_schema()}},
            "stream": False,
        }
        try:
            response = await self._client.post("/v1/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise ModelUnavailableError("fallback model not reachable") from exc
        if response.status_code != 200:
            raise ModelError(f"fallback model returned HTTP {response.status_code}")
        try:
            body = response.json()
            choice = body["choices"][0]
            if choice.get("finish_reason") == "length":  # ran away (repeating itself): no usable JSON
                raise ModelError("fallback model output was cut off")
            reading = json.loads(choice["message"]["content"])
            usage = body.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelError("fallback model returned malformed output") from exc
        if not isinstance(reading, dict):
            raise ModelError("fallback model returned malformed output")
        return reading, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))

    async def close(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# From the model's JSON to items, and the decision
# ---------------------------------------------------------------------------


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(re.sub(r"[^\d.\-]", "", str(value))) if isinstance(value, str) else Decimal(str(value))
    except InvalidOperation:
        return None
    return number.quantize(Decimal(1)) if number == number.to_integral_value() else number


def _line_amount(quantity: Decimal, rate: Decimal, discount: Decimal | None) -> Decimal:
    return quantity * rate * (100 - (discount or Decimal(0))) / 100


def fix_quantity(quantity: Decimal | None, rate: Decimal | None, discount: Decimal | None, amount: Decimal | None) -> Decimal | None:
    """"15,0000" (a quantity printed with four decimals) read as 150000: the line's own
    arithmetic says which scale is right."""
    if quantity is None or rate is None or amount is None or not rate:
        return quantity
    tolerance = max(Decimal(1), abs(amount) * Decimal("0.005"))
    if abs(_line_amount(quantity, rate, discount) - amount) <= tolerance:
        return quantity
    for divisor in (Decimal(10000), Decimal(1000), Decimal(100)):
        scaled = quantity / divisor
        if abs(_line_amount(scaled, rate, discount) - amount) <= tolerance:
            return scaled.quantize(Decimal(1)) if scaled == scaled.to_integral_value() else scaled
    return quantity


def fallback_note(name: str) -> str:
    return f"read by {name} (the first reading failed its checks); verify"


def to_items(readings: list[dict[str, Any]], name: str) -> list[LineItem]:
    """The model's lines from every page, in order, as ``LineItem``s."""
    items: list[LineItem] = []
    source = f"read by {name}"
    for reading in readings:
        for raw in reading.get("items") or []:
            if not isinstance(raw, dict):
                continue
            number = _number(raw.get("line_number"))
            values = {key: _number(raw.get(key)) for key in ITEM_NUMBERS}
            values["quantity"] = fix_quantity(values["quantity"], values["rate"], values["discount_percent"], values["taxable_value"])
            line = int(number) if number is not None and number == number.to_integral_value() else len(items) + 1
            items.append(LineItem(
                line_number=Extracted.check(line, fallback_note(name), source),
                **{key: Extracted.found(value, source) for key, value in values.items() if value is not None},
                **{key: Extracted.found(str(raw[key]).strip(), source) for key in ITEM_TEXTS if raw.get(key) not in (None, "")},
            ))
    return items


AVERAGE_RATE = "the line prints several rates (e.g. one per size): gross / quantity"


def _line_ok(item: LineItem) -> bool:
    q, r, d, a = item.quantity.value, item.rate.value, item.discount_percent.value, item.taxable_value.value
    if q is None or r is None or a is None:
        return False
    return abs(_line_amount(q, r, d) - a) <= max(Decimal(1), abs(a) * Decimal("0.005"))


def repair_gross(items: list[LineItem], subtotal: Decimal | None, name: str) -> list[LineItem]:
    """Size-matrix lines read as "first size's rate, gross amount before discount": when the
    lines add up to the printed subtotal only after each line's discount, they are gross. Their
    amount becomes net and their rate the average (gross / quantity), marked ``check``."""
    if subtotal is None or not items or any(i.taxable_value.value is None for i in items):
        return items
    gross = [i.taxable_value.value for i in items]
    net = [g * (100 - (i.discount_percent.value or Decimal(0))) / 100 for g, i in zip(gross, items, strict=True)]  # type: ignore[operator]
    if abs(sum(gross) - subtotal) <= 1 or abs(sum(net) - subtotal) > 1:  # type: ignore[arg-type]
        return items
    repaired = []
    for item, g, n in zip(items, gross, net, strict=True):
        update: dict[str, Any] = {"taxable_value": Extracted.found(n.quantize(Decimal("0.01")), f"{g} less {item.discount_percent.value}% (read by {name})")}
        if item.quantity.value:
            update["rate"] = Extracted.check((g / item.quantity.value).quantize(Decimal("0.01")), AVERAGE_RATE, f"{g} / {item.quantity.value}")  # type: ignore[operator]
        repaired.append(item.model_copy(update=update))
    return repaired


def _line_wrong(item: LineItem) -> bool:
    """Quantity, rate and amount all read, and they do not agree (a line without a rate, e.g. a
    freight bill's consignment, cannot be judged and is never replaced)."""
    values = (item.quantity.value, item.rate.value, item.taxable_value.value)
    return all(v is not None for v in values) and not _line_ok(item)


def _by_line(first: list[LineItem], model: list[LineItem]) -> list[LineItem] | None:
    """The rules' lines, with each line that fails its arithmetic (or is missing) taken from the
    model's reading when the model's line adds up. None: nothing to take."""
    theirs = {i.line_number.value: i for i in model if _line_ok(i)}
    out, taken = [], False
    ours = {i.line_number.value for i in first}
    for item in first:
        replacement = theirs.get(item.line_number.value)
        if replacement is not None and _line_wrong(item):
            out.append(replacement)
            taken = True
        else:
            out.append(item)
    for number, item in theirs.items():
        if number not in ours:
            out.append(item)
            taken = True
    return sorted(out, key=lambda i: i.line_number.value or 0) if taken else None


def _checks(invoice: Invoice, document: str) -> dict[str, str]:
    """Status of each check the fallback can fix: pass, fail, or skip (a value it needs is missing)."""
    _, report = validate_invoice(invoice, document)
    statuses = {check.id: check.status for check in report.checks if check.id in FALLBACK_CHECKS}
    if not invoice.items:
        statuses["items"] = "fail"
    return statuses


def _cost(statuses: dict[str, str]) -> int:
    """A failed check costs 2, one that could not run (missing subtotal, total ...) 1."""
    return sum({"fail": 2, "skip": 1}.get(status, 0) for status in statuses.values())


ITEM_CHECKS = ("line_arithmetic", "items_sum", "line_numbers")


def fallback_scope(invoice: Invoice, document: str) -> str | None:
    """What the fallback model is asked for: "items" (every page, items and totals) when an item
    check fails or no items were read; "totals" (the last page's totals block only, a short answer)
    when only the totals cannot be reconciled; None when nothing calls for it. A line check
    skipped for want of a rate (freight consignments) is not a fault."""
    statuses = _checks(invoice, document)
    if not invoice.items or any(statuses.get(check) == "fail" for check in ITEM_CHECKS):
        return "items"
    if statuses.get("grand_total") != "pass" or statuses.get("items_sum") == "skip":
        return "totals"
    return None


def totals_show_item_fault(invoice: Invoice, reading: dict[str, Any], document: str) -> bool:
    """With the model's totals in place, do the items fail to add up (lines missed or misread)?
    Then a totals-only reading is not enough: escalate to the items."""
    updates = {name: Extracted.found(value, "probe") for name in TOTAL_FIELDS if (value := _number(reading.get(name))) is not None}
    statuses = _checks(invoice.model_copy(update=updates), document)
    return any(statuses.get(check) == "fail" for check in ITEM_CHECKS)


def needs_fallback(invoice: Invoice, document: str) -> list[str]:
    """The checks that fail or cannot run (empty: the rules' reading holds up)."""
    return [f"{name} ({status})" for name, status in _checks(invoice, document).items() if status != "pass"]


def apply_fallback(invoice: Invoice, readings: list[dict[str, Any]], document: str, name: str) -> tuple[Invoice, list[str], bool]:
    """(invoice, warnings, adopted): the model's items and/or its totals replace the rules' only
    where that makes more of the checks pass; the combination that passes most wins (ties keep
    the rules' reading)."""
    before = _checks(invoice, document)
    totals = next((r for r in reversed(readings) if r.get("total_amount") is not None), readings[-1] if readings else {})
    items = repair_gross(to_items(readings, name), _number(totals.get("subtotal")) or invoice.subtotal.value, name)
    total_updates: dict[str, Any] = {}
    for field in TOTAL_FIELDS:
        value = _number(totals.get(field))
        if value is not None and (getattr(invoice, field).value is None or before.get("grand_total") != "pass"):
            total_updates[field] = Extracted.found(value, f"read by {name}")
    options: list[tuple[str, Invoice]] = [("first", invoice)]
    if total_updates:
        options.append(("totals", invoice.model_copy(update=total_updates)))
    if items and (lines := _by_line(invoice.items, items)):
        options.append(("lines", invoice.model_copy(update={"items": lines})))
        if total_updates:
            options.append(("lines and totals", invoice.model_copy(update={"items": lines, **total_updates})))
    # The whole list only replaces one that is actually broken: lines that merely cannot be
    # checked (no rate, e.g. freight consignments) are not wrong.
    broken = not invoice.items or any(before.get(check) == "fail" for check in ("line_arithmetic", "items_sum", "line_numbers"))
    if items and broken:
        options.append(("items", invoice.model_copy(update={"items": items})))
        if total_updates:
            options.append(("items and totals", invoice.model_copy(update={"items": items, **total_updates})))
    scored = [(_cost(_checks(candidate, document)), index, label, candidate) for index, (label, candidate) in enumerate(options)]
    cost, _, label, chosen = min(scored, key=lambda s: (s[0], s[1]))
    if label == "first":
        return invoice, [f"{name} read the page too, but its reading does not pass more checks; the first reading is kept"], False
    after = _checks(chosen, document)
    fixed = [check for check, status in before.items() if status != "pass" and after.get(check) == "pass"]
    note = f"{label} read by {name}: {', '.join(fixed) or 'more checks'} now pass" + (
        "; the lines it read are marked for review" if label != "totals" else "")
    return chosen, [note], True
