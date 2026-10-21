"""Zoomed readings: blocks and flagged fields read again, enlarged, as a second opinion.

PaddleOCR-VL shrinks every image to about one megapixel, so on a whole page (or the wide
header strip) small print gets only a few pixels per character. Two extra readings cut the
text out first and enlarge it (``invoice_bands.zoom_crop``):

* **Blocks** (``region OCR``): the reasoning model boxes the seller, buyer, invoice-details
  and totals blocks; each is read on its own.
* **Flagged fields** (``re-read``): after the blocks, fields that are still ``check``, fail a
  validation, or are required but missing are boxed one by one and read at up to 2-3x.

``apply_zoom`` merges those readings into the invoice. A zoomed reading never wins just by
being zoomed:

* it agrees with the value                -> note "matches the zoomed ... crop"; unchanged
* the field is empty                      -> the zoomed value, ``check`` (if it passes the
                                             field's validation, where there is one)
* it disagrees, the field has a validation (GSTIN checksum, date range, totals adding up):
    - the current value fails, the zoomed one passes -> replaced, ``check`` (verify)
    - the current value passes                       -> kept; the other reading in the note
    - neither passes                                 -> kept, ``check``, both in the note
* it disagrees, no validation (invoice number, names):
    - the block crop and the re-read agree with each other -> replaced, ``check``
    - the value was found only by the reasoning model (no OCR rule backs it) -> replaced, ``check``
    - otherwise: invoice number ``check`` with both in the note; names only get the note
      (the signature line and a clipped block legitimately differ)
* a field flagged only because the block crop disagreed is cleared again when the re-read
  agrees with the original value (two readings out of three)

Replaced values stay ``check`` so that a reviewer sees them; the notes never say "confirmed by"
or "corrected by", so the confidence heuristic does not treat a zoomed reading as independent
confirmation. Everything here is pure (text in, invoice out), so saved results can be
re-mapped by the benchmark without running OCR again.
"""

import datetime as dt
import re
from decimal import Decimal
from typing import Any

from app.schemas.invoice import Extracted, Invoice
from app.services.invoice_extraction import (
    _WHOLE_DATE,
    _captioned_buyer,
    _invoice_number_in_text,
    _labelled_amounts,
    _similar,
    amounts_in,
    date_in_text,
    find_gstin,
    is_label,
    parse_date,
    parse_otsl,
    parse_parties,
)
from app.services.invoice_normalize import normalize_invoice_number
from app.services.invoice_validation import REQUIRED, validate_invoice
from app.services.purchase_order import PO_REQUIRED, is_purchase_order

REGION = "region OCR"
REREAD = "re-read"

# Which fields each block holds.
REGION_FIELDS: dict[str, tuple[str, ...]] = {
    "seller": ("vendor_name", "vendor_gstin"),
    "buyer": ("buyer_name", "buyer_gstin"),
    "invoice_details": ("invoice_number", "invoice_date"),
    "totals": ("subtotal", "cgst", "sgst", "igst", "total_amount"),
}
# Fields worth re-reading, most important first, and how to describe them to the locator.
REREAD_FIELDS: dict[str, str] = {
    "vendor_gstin": "the seller's GSTIN (a 15-character code such as 33ABCDE1234F1Z5) in the seller block",
    "buyer_gstin": "the buyer's GSTIN (a 15-character code) in the buyer / bill-to block",
    "invoice_number": "the invoice number value next to the 'Invoice No.' caption",
    "invoice_date": "the invoice date value next to the 'Dated' or 'Invoice Date' caption",
    "total_amount": "the grand total amount of the invoice",
    "subtotal": "the total taxable value amount",
    "vendor_name": "the seller's company name at the top of the invoice",
    "buyer_name": "the buyer's company name under 'Buyer' or 'Bill to'",
}
AMOUNT_FIELDS = ("subtotal", "cgst", "sgst", "igst", "total_amount")
NAME_FIELDS = ("vendor_name", "buyer_name")
LAST_PAGE_FIELDS = AMOUNT_FIELDS  # read from the last page; the rest from the first
# Validation checks that decide each field (see invoice_validation).
FIELD_CHECKS: dict[str, tuple[str, ...]] = {
    "vendor_gstin": ("vendor_gstin",),
    "buyer_gstin": ("buyer_gstin",),
    "invoice_date": ("invoice_date",),
    "subtotal": ("grand_total",),
    "total_amount": ("grand_total",),
    "cgst": ("grand_total", "cgst_sgst"),
    "sgst": ("grand_total", "cgst_sgst"),
    "igst": ("grand_total",),
}
_OTHER_PARTY = {"vendor_gstin": "buyer_gstin", "buyer_gstin": "vendor_gstin", "vendor_name": "buyer_name", "buyer_name": "vendor_name"}
_SOURCE = {REGION: "zoomed {label} crop", REREAD: "zoomed re-read"}


# ---------------------------------------------------------------------------
# Reading values out of zoomed text
# ---------------------------------------------------------------------------


def region_values(label: str, text: str) -> dict[str, Any]:
    """Field values found in one block's OCR text (``totals``: table markup)."""
    if label == "invoice_details":
        out: dict[str, Any] = {}
        if found := _invoice_number_in_text(text):
            out["invoice_number"] = normalize_invoice_number(found[0], text)[0]
        if dated := date_in_text(text):
            out["invoice_date"] = dated[0]
        return out
    if label == "totals":
        amounts = _labelled_amounts([parse_otsl(text)])
        return {name: amounts[name].value for name in AMOUNT_FIELDS if name in amounts}
    if label not in ("seller", "buyer"):
        return {}
    parties = parse_parties(text)
    if label == "seller":
        party = next((p for p in parties if p.role is None), None)
    else:
        party = next((p for p in parties if p.role == "buyer"), None) or next(
            (p for p in parties if p.role is None), None
        ) or _captioned_buyer(text, Extracted())
    prefix = "vendor" if label == "seller" else "buyer"
    out = {}
    if party is not None:
        if party.name:
            out[f"{prefix}_name"] = party.name
        if party.gstin.value and party.gstin.status == "ok":  # a zoomed GSTIN is only useful if it checks out
            out[f"{prefix}_gstin"] = party.gstin.value
    return out


def reread_candidates(name: str, text: str) -> list[Any]:
    """Possible values of ``name`` in a one-field crop's OCR text, most likely first."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if name in ("vendor_gstin", "buyer_gstin"):
        found = [g.value for line in [*lines, text.replace(" ", "")] if (g := find_gstin(line)) and g.status == "ok"]
        return list(dict.fromkeys(found))
    if name == "invoice_number":
        if found := _invoice_number_in_text(text):
            return [normalize_invoice_number(found[0], text)[0]]
        # A tight crop may hold only the value: one token with a digit that is not a date.
        tokens = [t for line in lines for t in line.split()]
        # "719" over its "No." caption (a handwritten or stamped serial number).
        if any(re.fullmatch(r"no\.?", line, re.I) for line in lines):
            serials = [line for line in lines if re.fullmatch(r"\d{2,8}", line)]
            if len(serials) == 1:
                return serials
        if len(tokens) == 1 and re.search(r"\d", tokens[0]) and not _WHOLE_DATE.fullmatch(tokens[0]) and not re.fullmatch(r"\d{10,}", tokens[0]):
            return [normalize_invoice_number(tokens[0], text)[0]]
        return []
    if name == "invoice_date":
        if dated := date_in_text(text):
            return [dated[0]]
        return list(dict.fromkeys(d for line in lines if (d := parse_date(line))))
    if name in AMOUNT_FIELDS:
        return list(dict.fromkeys(a for a in amounts_in(text.replace("\n", " ")) if abs(a) >= 1))
    if name in NAME_FIELDS:
        prefix = "seller" if name == "vendor_name" else "buyer"
        if value := region_values(prefix, text).get(name):
            return [value]
        return [line for line in lines if len(re.findall(r"[A-Za-z]", line)) >= 3 and not is_label(line) and not find_gstin(line)][:1]
    return []


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------


def _flat(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def _same(name: str, a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    if isinstance(a, Decimal | dt.date) or isinstance(b, Decimal | dt.date):
        return a == b
    if name in NAME_FIELDS:
        return _similar(str(a), str(b))
    return _flat(a) == _flat(b)


def _verdict(invoice: Invoice, name: str, document: str) -> bool | None:
    """Do the validation checks that decide ``name`` pass (None: none of them applies)?"""
    _, report = validate_invoice(invoice, document)
    applied = [c.status for c in report.checks if c.id in FIELD_CHECKS.get(name, ()) and c.status != "skip"]
    return all(s == "pass" for s in applied) if applied else None


def _reasoning_only(field: Extracted[Any]) -> bool:
    """Was the value supplied only by the reasoning model (invoice_llm.merge), with no OCR rule behind it?"""
    return field.status == "check" and bool(field.note) and "found only by" in str(field.note)


def _with(invoice: Invoice, name: str, field: Extracted[Any]) -> Invoice:
    return invoice.model_copy(update={name: field})


def _note(field: Extracted[Any], note: str) -> Extracted[Any]:
    return field.model_copy(update={"note": "; ".join(n for n in (field.note, note) if n)})


def _show(value: Any) -> str:
    return value.isoformat() if isinstance(value, dt.date) else str(value)


class _Merge:
    def __init__(self, invoice: Invoice, document: str) -> None:
        self.original = invoice
        self.invoice = invoice
        self.document = document
        self.region: dict[str, Any] = {}  # field -> the block crop's value
        self.region_flagged: set[str] = set()  # fields flagged only because the block crop disagreed
        self.replaced: list[str] = []
        self.disagreed: list[str] = []

    def consider(self, name: str, candidates: list[Any], source: str, kind: str) -> None:
        current: Extracted[Any] = getattr(self.invoice, name)
        other = _OTHER_PARTY.get(name)
        taken = getattr(self.invoice, other).value if other else None
        candidates = [c for c in candidates if not _same(name, c, taken)]  # the other party's value: wrong block
        if not candidates:
            return
        if any(_same(name, c, current.value) for c in candidates):
            if kind == REREAD and name in self.region_flagged:
                # Two readings out of three agree with the original: the block crop misread it.
                self.invoice = _with(self.invoice, name, _note(getattr(self.original, name), f"matches the {source}"))
                self.region_flagged.discard(name)
            else:
                self.invoice = _with(self.invoice, name, _note(current, f"matches the {source}"))
            return

        checked = name in FIELD_CHECKS
        now = _verdict(self.invoice, name, self.document) if checked and current.value is not None else None
        passing = [c for c in candidates if not checked or _verdict(_with(self.invoice, name, Extracted.found(c)), name, self.document) is not False]
        value = passing[0] if passing else candidates[0]
        seconded = kind == REREAD and _same(name, value, self.region.get(name))

        if current.value is None:
            if passing:
                self._replace(name, value, f"read only in the {source}; verify", source)
            return
        if checked and passing and now is False and _verdict(_with(self.invoice, name, Extracted.found(value)), name, self.document):
            self._replace(name, value, f"the first reading {_show(current.value)} did not pass validation; the {source} reads {_show(value)}; verify", source)
            return
        if not checked and seconded:
            self._replace(name, value, f"the zoomed block crop and re-read both read {_show(value)} (first reading {_show(current.value)}); verify", source)
            return
        if not checked and _reasoning_only(current):
            # A guess no OCR reading backs (e.g. a code from another slip in the photo) is weaker
            # evidence than OCR of the zoomed block itself.
            self._replace(name, value, f"the first reading {_show(current.value)} was found only by the reasoning model; the {source} reads {_show(value)}; verify", source)
            return
        self.disagreed.append(name)
        note = f"the {source} reads {_show(value)}"
        if name in NAME_FIELDS or now is True:
            self.invoice = _with(self.invoice, name, _note(current, note))
        else:
            if current.status != "check" and kind == REGION:
                self.region_flagged.add(name)
            self.invoice = _with(self.invoice, name, _note(current, note).model_copy(update={"status": "check"}))

    def _replace(self, name: str, value: Any, note: str, source: str) -> None:
        self.invoice = _with(self.invoice, name, Extracted.check(value, note, source))
        self.region_flagged.discard(name)
        self.replaced.append(name)


def apply_zoom(invoice: Invoice, readings: list[tuple[str, str, str]], document: str) -> tuple[Invoice, list[str]]:
    """Merge zoomed readings (``split_zoom`` of the raw text) into ``invoice``; see the module
    docstring. ``document`` is the OCR text the validation checks look at."""
    merge = _Merge(invoice, document)
    for kind, label, text in readings:
        if kind != REGION:
            continue
        values = region_values(label, text)
        for name in REGION_FIELDS.get(label, ()):
            if name in values:
                merge.region[name] = values[name]
                merge.consider(name, [values[name]], _SOURCE[REGION].format(label=label.replace("_", " ")), REGION)
    for kind, label, text in readings:
        if kind == REREAD and label in REREAD_FIELDS:
            merge.consider(label, reread_candidates(label, text), _SOURCE[REREAD], REREAD)
    warnings: list[str] = []
    if merge.replaced:
        warnings.append(f"zoomed crops changed {', '.join(dict.fromkeys(merge.replaced))}; marked 'check'")
    disagreed = [n for n in dict.fromkeys(merge.disagreed) if n not in merge.replaced]
    if disagreed:
        warnings.append(f"zoomed crops read {', '.join(disagreed)} differently; see the field notes")
    return merge.invoice, warnings


def blocks_needing_zoom(invoice: Invoice, document: str) -> set[str]:
    """Blocks worth a zoomed crop: one of their fields is missing, marked ``check`` or failing a
    validation check. A block whose fields all check out cannot be changed by its crop (a
    zoomed reading never wins just by being zoomed), so reading it only costs time."""
    _, report = validate_invoice(invoice, document)
    failed = {name for check in report.checks if check.status == "fail" for name in check.fields}
    taxes = ("cgst", "sgst", "igst")
    no_tax = all(getattr(invoice, name).value is None for name in taxes)

    def in_doubt(name: str) -> bool:
        field = getattr(invoice, name)
        if field.status == "check" or name in failed:
            return True
        # CGST + SGST or IGST: one of them is legitimately absent; only no tax at all is missing.
        return field.value is None and (name not in taxes or no_tax)

    needed = {label for label, names in REGION_FIELDS.items() if any(in_doubt(name) for name in names)}
    if is_purchase_order(document):
        needed.discard("totals")  # an order has no totals or taxes to read
        if invoice.vendor_name.value is not None:
            needed.discard("seller")  # its supplier has no GSTIN printed
    return needed


def reread_targets(invoice: Invoice, document: str, limit: int) -> list[str]:
    """Fields to read again, most important first: ``check``, failing a validation, or required
    and missing."""
    if limit <= 0:
        return []
    _, report = validate_invoice(invoice, document)
    failed = {name for check in report.checks if check.status == "fail" for name in check.fields}
    order = is_purchase_order(document)
    required = PO_REQUIRED if order else REQUIRED
    targets = [
        name for name in REREAD_FIELDS
        if not (order and name in (*AMOUNT_FIELDS, "vendor_gstin", "buyer_gstin"))
        and ((field := getattr(invoice, name)).status == "check" or name in failed or (field.value is None and name in required))
    ]
    return targets[:limit]
