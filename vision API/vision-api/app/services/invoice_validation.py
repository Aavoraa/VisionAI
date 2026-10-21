"""Phase 3: validation of an extracted invoice (formats, arithmetic, GST rules, amounts in words).

``validate_invoice`` runs every check, returns a ``ValidationReport`` and marks the fields a
failed check involves as ``check`` (with the reason), so nothing inconsistent looks ``ok``.
Checks that cannot run (a value is missing, or the document holds only some pages of the
invoice) are reported as ``skip`` with the reason, never as passed.

All money comparisons allow 1 paisa (0.01) for rounding.
"""

import datetime as dt
import re
from decimal import Decimal
from typing import Any

from app.schemas.invoice import Extracted, Invoice, LineItem, ValidationCheck, ValidationReport
from app.services.purchase_order import PO_REQUIRED, is_purchase_order, size_counts

TOLERANCE = Decimal("0.01")
GST_START = dt.date(2017, 7, 1)
# Total GST rates in use (CGST + SGST, or IGST), in percent.
GST_RATES = (Decimal("0"), Decimal("0.1"), Decimal("0.25"), Decimal("1"), Decimal("1.5"), Decimal("3"), Decimal("5"),
             Decimal("6"), Decimal("12"), Decimal("18"), Decimal("28"), Decimal("40"))
REQUIRED = ("invoice_number", "invoice_date", "vendor_name", "vendor_gstin", "buyer_name", "total_amount")

# ---------------------------------------------------------------------------
# GSTIN: 2-digit state, PAN (5 letters, 4 digits, 1 letter), entity number, "Z", check character
# ---------------------------------------------------------------------------

_B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin_check_char(first14: str) -> str:
    total = 0
    for index, char in enumerate(first14):
        product = _B36.index(char) * (1 if index % 2 == 0 else 2)
        total += product // 36 + product % 36
    return _B36[(36 - total % 36) % 36]


def is_valid_gstin(value: str) -> bool:
    return bool(re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]", value)) and gstin_check_char(value[:14]) == value[14]

# ---------------------------------------------------------------------------
# Amounts in words (Indian numbering: thousand, lakh, crore; rupees and paise)
# ---------------------------------------------------------------------------

_ONES = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if i >= 2}
_SCALES = {"thousand": 1_000, "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000,
           "crore": 10_000_000, "crores": 10_000_000}


def _words_to_int(words: list[str]) -> int | None:
    total = current = 0
    seen = False
    for word in words:
        if word == "and":
            continue
        if word in _ONES:
            current += _ONES[word]
        elif word in _TENS:
            current += _TENS[word]
        elif word == "hundred":
            current = (current or 1) * 100
        elif word in _SCALES:
            total += (current or 1) * _SCALES[word]
            current = 0
        else:
            return None  # an unknown word: better no value than a wrong one
        seen = True
    return total + current if seen else None


def words_to_amount(text: str) -> Decimal | None:
    """ "INR Forty Nine Thousand Nine Hundred Sixty Nine Only" -> 49969; "... and Sixty Six paise" -> .66"""
    cleaned = re.sub(r"\b(inr|rs|rupees?|only)\b|[^a-z ]", " ", text.lower().replace("-", " "))
    words = cleaned.split()
    paise = 0
    if "paise" in words or "paisa" in words:
        end = words.index("paise") if "paise" in words else words.index("paisa")
        head = words[:end]
        if "and" not in head:
            return None
        split = len(head) - 1 - head[::-1].index("and")
        paise_value = _words_to_int(head[split + 1 :])
        if paise_value is None or paise_value >= 100:
            return None
        paise, words = paise_value, head[:split]
    rupees = _words_to_int(words)
    if rupees is None:
        return None
    return Decimal(rupees) + Decimal(paise) / 100


_AMOUNT_WORDS = re.compile(r"amount\s+chargeable\s*\(in\s+words\)\s*[:|]?\s*(.+?\bonly\b)", re.I | re.S)
_TAX_WORDS = re.compile(r"tax\s+amount\s*\(in\s+words\)\s*[:|]?\s*(.+?\bonly\b)", re.I | re.S)


def _plain(document: str) -> str:
    """OCR text with table markup turned into spaces."""
    return " ".join(re.sub(r"<(?:fcel|ecel|lcel|ucel|xcel|nl)>", " ", document).split())


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


class _Report:
    def __init__(self) -> None:
        self.checks: list[ValidationCheck] = []
        self.flag: dict[str, str] = {}  # field -> reason, for failed checks
        self.item_flags: dict[int, dict[str, str]] = {}  # item index -> field -> reason

    def add(self, check_id: str, name: str, status: str, message: str, fields: tuple[str, ...] = ()) -> None:
        self.checks.append(ValidationCheck(id=check_id, name=name, status=status, message=message, fields=list(fields)))  # type: ignore[arg-type]
        if status == "fail":
            for name_ in fields:
                self.flag.setdefault(name_, message)


def _v(field: Extracted[Any]) -> Any:
    return field.value


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _mixed_rate_tax(invoice: Invoice) -> Decimal | None:
    """Σ line amount x line GST rate, when every line has both and there are several rates."""
    lines = [(i.taxable_value.value, i.tax_rate.value) for i in invoice.items]
    if not lines or any(amount is None or rate is None for amount, rate in lines):
        return None
    if len({rate for _, rate in lines}) < 2:
        return None
    return sum((amount * rate / 100 for amount, rate in lines), Decimal(0))  # type: ignore[operator]


def _state_code(text: str | None) -> str | None:
    match = re.search(r"\((\d{2})\)", text or "")
    return match.group(1) if match else None


def validate_invoice(invoice: Invoice, document: str = "", *, today: dt.date | None = None) -> tuple[Invoice, ValidationReport]:
    """Run every check; returns the invoice with failing fields marked ``check``, and the report."""
    report = _Report()
    today = today or dt.date.today()
    plain = _plain(document)

    # --- Presence and formats ---------------------------------------------------------
    order = is_purchase_order(document)  # a purchase order has no GSTIN of the supplier, no taxes, no totals
    missing = [name for name in (PO_REQUIRED if order else REQUIRED) if _v(getattr(invoice, name)) is None]
    present = "Order number, date, supplier and buyer are present" if order else \
        "Invoice number, date, seller, seller GSTIN, buyer and total are present"
    report.add("required_fields", "Required fields", "fail" if missing else "pass",
               f"Not found: {', '.join(missing)}" if missing else present)
    if order:
        # Handwritten lines: the pieces per size must add up to the line's written total.
        wrong = [f"line {_v(item.line_number)}" for item in invoice.items
                 if (counts := size_counts(_v(item.size))) and _v(item.quantity) is not None and sum(counts) != _v(item.quantity)]
        unread = [f"line {_v(item.line_number)}" for item in invoice.items if item.size.status == "check"]
        if not invoice.items:
            report.add("size_counts", "Size counts add up", "skip", "No order lines")
        elif wrong or unread:
            report.add("size_counts", "Size counts add up", "fail",
                       f"Check the handwriting: {', '.join(dict.fromkeys(wrong + unread))}", ())
        else:
            report.add("size_counts", "Size counts add up", "pass", f"Pieces per size add up to the total on all {len(invoice.items)} lines")

    date = _v(invoice.invoice_date)
    if date is None:
        report.add("invoice_date", "Invoice date", "skip", "No invoice date")
    elif not GST_START <= date <= today + dt.timedelta(days=7):
        report.add("invoice_date", "Invoice date", "fail",
                   f"{date.isoformat()} is outside {GST_START.isoformat()} (GST start) to today", ("invoice_date",))
    else:
        report.add("invoice_date", "Invoice date", "pass", f"{date.isoformat()} is a plausible GST invoice date")

    for role in ("vendor", "buyer"):
        gstin: Extracted[str] = getattr(invoice, f"{role}_gstin")
        label = "Seller GSTIN" if role == "vendor" else "Buyer GSTIN"
        if gstin.value is None:
            report.add(f"{role}_gstin", label, "skip", "No GSTIN found")
        elif not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]", str(gstin.value)):
            # Judged by the value itself: a field's note may come from any reader (e.g. the
            # reasoning model), and an e-mail address or a hash is never a GSTIN.
            report.add(f"{role}_gstin", label, "fail",
                       f"{gstin.value}: not a GSTIN (15 characters: state code, PAN, entity, Z, check)", (f"{role}_gstin",))
        elif not is_valid_gstin(str(gstin.value)):
            report.add(f"{role}_gstin", label, "fail", f"{gstin.value}: checksum does not match", (f"{role}_gstin",))
        else:
            report.add(f"{role}_gstin", label, "pass", f"{gstin.value}: valid format and checksum")

    # --- Line items ----------------------------------------------------------------------
    items = invoice.items
    numbers = [_v(item.line_number) for item in items]
    if len(items) < 2 or any(n is None for n in numbers):
        report.add("line_numbers", "Line numbers", "skip", "Fewer than two numbered lines")
    else:
        gaps = [f"{a}->{b}" for a, b in zip(numbers, numbers[1:]) if b != a + 1]
        if gaps:
            report.add("line_numbers", "Line numbers", "fail",
                       f"Lines are not consecutive ({', '.join(gaps[:5])}): a row may have been dropped or read twice")
            for index, (a, b) in enumerate(zip(numbers, numbers[1:]), 1):
                if b != a + 1:
                    report.item_flags.setdefault(index, {})["line_number"] = "line number out of sequence"
        else:
            report.add("line_numbers", "Line numbers", "pass", f"Lines {numbers[0]}-{numbers[-1]} are consecutive")

    wrong_lines: list[str] = []
    checked_lines = 0
    for index, item in enumerate(items):
        qty, rate, amount = _v(item.quantity), _v(item.rate), _v(item.taxable_value)
        if None in (qty, rate, amount):
            continue
        discount = _v(item.discount_percent) or Decimal(0)
        expected = (qty * rate * (100 - discount) / 100).quantize(Decimal("0.01"))
        checked_lines += 1
        if abs(expected - amount) > TOLERANCE:
            line = _v(item.line_number)
            wrong_lines.append(f"line {line}: {qty} × {_money(rate)} − {discount}% = {_money(expected)}, read {_money(amount)}")
            reason = f"{qty} × {_money(rate)} − {discount}% = {_money(expected)}, but the amount reads {_money(amount)}"
            report.item_flags.setdefault(index, {}).update({"quantity": reason, "rate": reason, "taxable_value": reason})
    if checked_lines == 0:
        report.add("line_arithmetic", "Line amounts", "skip", "No line has quantity, rate and amount")
    elif wrong_lines:
        report.add("line_arithmetic", "Line amounts", "fail", "; ".join(wrong_lines[:5]))
    else:
        report.add("line_arithmetic", "Line amounts", "pass", f"Quantity × rate − discount = amount on all {checked_lines} lines")

    # Sums over items are only meaningful when the document holds every line of the invoice.
    complete = bool(items) and numbers[0] == 1
    partial_reason = (f"The document starts at line {numbers[0]}: earlier pages are not included"
                      if items and numbers[0] not in (None, 1) else "No numbered line items")
    subtotal = _v(invoice.subtotal)
    amounts = [_v(i.taxable_value) for i in items]
    if not complete or subtotal is None or None in amounts:
        report.add("items_sum", "Items add up", "skip", partial_reason if not complete else "Taxable value or an amount is missing")
    else:
        total_items = sum(amounts, Decimal(0))
        ok = abs(total_items - subtotal) <= TOLERANCE * len(items)
        report.add("items_sum", "Items add up", "pass" if ok else "fail",
                   f"Items total {_money(total_items)}" + ("" if ok else f", taxable value reads {_money(subtotal)}"),
                   () if ok else ("subtotal",))
    total_qty = _v(invoice.total_quantity)
    quantities = [_v(i.quantity) for i in items]
    if not complete or total_qty is None or None in quantities:
        report.add("quantity_sum", "Quantities add up", "skip", partial_reason if not complete else "A quantity is missing")
    else:
        sum_qty = sum(quantities, Decimal(0))
        report.add("quantity_sum", "Quantities add up", "pass" if sum_qty == total_qty else "fail",
                   f"Lines add up to {sum_qty}" + ("" if sum_qty == total_qty else f", total quantity reads {total_qty}"),
                   () if sum_qty == total_qty else ("total_quantity",))

    # --- Taxes ------------------------------------------------------------------------------
    cgst, sgst, igst = _v(invoice.cgst), _v(invoice.sgst), _v(invoice.igst)
    seller_code = (_v(invoice.vendor_gstin) or "")[:2] or None
    supply_code = _state_code(_v(invoice.place_of_supply)) or ((_v(invoice.buyer_gstin) or "")[:2] or None)
    if seller_code is None or supply_code is None:
        report.add("tax_type", "Tax type", "skip", "Seller or place-of-supply state unknown")
    else:
        intra = seller_code == supply_code
        has_split = bool(cgst or sgst)
        if intra and igst:
            report.add("tax_type", "Tax type", "fail", f"Same state ({seller_code}): expected CGST + SGST, found IGST", ("igst",))
        elif not intra and has_split:
            report.add("tax_type", "Tax type", "fail",
                       f"Different states ({seller_code} -> {supply_code}): expected IGST, found CGST/SGST", ("cgst", "sgst"))
        elif not (has_split or igst):
            report.add("tax_type", "Tax type", "skip", "No tax amounts found")
        else:
            report.add("tax_type", "Tax type", "pass",
                       f"{'Same state' if intra else 'Different states'} ({seller_code} -> {supply_code}): "
                       f"{'CGST + SGST' if intra else 'IGST'} as expected")
    if cgst is None and sgst is None:
        report.add("cgst_sgst", "CGST = SGST", "skip", "No CGST/SGST")
    elif cgst is None or sgst is None or abs(cgst - sgst) > TOLERANCE:
        report.add("cgst_sgst", "CGST = SGST", "fail", f"CGST {cgst} and SGST {sgst} must be equal", ("cgst", "sgst"))
    else:
        report.add("cgst_sgst", "CGST = SGST", "pass", f"Both {_money(cgst)}")

    tax_total = sum((t for t in (cgst, sgst, igst) if t is not None), Decimal(0))
    if subtotal is None or not subtotal or tax_total == 0:
        report.add("tax_rate", "Tax rate", "skip", "Taxable value or taxes missing")
    elif (mixed := _mixed_rate_tax(invoice)) is not None:
        # Lines taxed at different rates (e.g. 5% and 18%): the total rate is a blend, so check
        # the taxes against each line's amount x its own rate instead.
        ok = abs(mixed - tax_total) <= max(Decimal(1), tax_total * Decimal("0.002"))
        report.add("tax_rate", "Tax rate", "pass" if ok else "fail",
                   f"Mixed rates: the lines' amounts at their own rates give {_money(mixed)}, taxes read {_money(tax_total)}",
                   () if ok else ("cgst", "sgst", "igst"))
    else:
        rate = tax_total * 100 / subtotal
        nearest = min(GST_RATES, key=lambda r: abs(r - rate))
        ok = abs(nearest - rate) <= Decimal("0.05")
        report.add("tax_rate", "Tax rate", "pass" if ok else "fail",
                   f"Taxes are {rate:.2f}% of the taxable value" + (f" (GST rate {nearest}%)" if ok else ", not a GST rate"),
                   () if ok else ("cgst", "sgst", "igst", "subtotal"))

    # --- Grand total ----------------------------------------------------------------------------
    total = _v(invoice.total_amount)
    round_off = _v(invoice.round_off) or Decimal(0)
    discount_total = _v(invoice.discount) or Decimal(0)
    if not round_off:
        report.add("round_off", "Round off", "skip", "No round-off")
    elif abs(round_off) >= 1:
        report.add("round_off", "Round off", "fail", f"Round-off {round_off} should be under 1.00", ("round_off",))
    else:
        report.add("round_off", "Round off", "pass", f"{round_off} is under 1.00")
    if total is None or subtotal is None:
        report.add("grand_total", "Grand total", "skip", "Taxable value or total missing")
    else:
        expected_total = subtotal + tax_total - discount_total + round_off
        ok = abs(expected_total - total) <= TOLERANCE
        report.add("grand_total", "Grand total", "pass" if ok else "fail",
                   f"{_money(subtotal)} + taxes {_money(tax_total)}"
                   + (f" − discount {_money(discount_total)}" if discount_total else "")
                   + (f" + round-off {round_off}" if round_off else "")
                   + f" = {_money(expected_total)}" + ("" if ok else f", total reads {_money(total)}"),
                   () if ok else ("total_amount", "subtotal", "cgst", "sgst", "igst", "round_off"))

    # --- Amounts in words ------------------------------------------------------------------------
    for check_id, name, pattern, value, fields in (
        ("amount_in_words", "Total in words", _AMOUNT_WORDS, total, ("total_amount",)),
        ("tax_in_words", "Tax in words", _TAX_WORDS, tax_total or None, ("cgst", "sgst", "igst")),
    ):
        match = pattern.search(plain)
        words_value = words_to_amount(match.group(1)) if match else None
        if match is None or value is None:
            report.add(check_id, name, "skip", "Not printed" if match is None else "No amount to compare")
        elif words_value is None:
            report.add(check_id, name, "skip", f"Could not read {match.group(1).strip()!r} as a number")
        elif abs(words_value - value) <= TOLERANCE:
            report.add(check_id, name, "pass", f"{match.group(1).strip()!r} = {_money(words_value)}")
        else:
            report.add(check_id, name, "fail",
                       f"{match.group(1).strip()!r} = {_money(words_value)}, but the figure reads {_money(value)}", fields)

    return _apply(invoice, report), ValidationReport(
        status="failed" if any(c.status == "fail" for c in report.checks) else "passed", checks=report.checks
    )


def _flagged(field: Extracted[Any], reason: str) -> Extracted[Any]:
    if field.value is None:
        return field
    note = f"{field.note}; {reason}" if field.note and reason not in field.note else (field.note or reason)
    return field.model_copy(update={"status": "check", "note": note})


def _apply(invoice: Invoice, report: _Report) -> Invoice:
    updates: dict[str, Any] = {name: _flagged(getattr(invoice, name), reason) for name, reason in report.flag.items()}
    if report.item_flags:
        items: list[LineItem] = []
        for index, item in enumerate(invoice.items):
            flags: dict[str, str] = report.item_flags.get(index, {})
            items.append(item.model_copy(update={k: _flagged(getattr(item, k), r) for k, r in flags.items()}) if flags else item)
        updates["items"] = items
    return invoice.model_copy(update=updates)

