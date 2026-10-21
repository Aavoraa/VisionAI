# backend/app/extraction/validate.py
"""
Reconciliation layer for daily bills.

Checks the AI-extracted invoice against its own internal arithmetic. If any
check fails, the field is flagged for human review. This catches a single
misread digit without needing any ground truth.

validate_bill() is pure: it never mutates the bill it is given. Where the math
says a field should hold a different value, that value lands in
`result.corrections` and only apply_validation(autocorrect=True) writes it.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any


@dataclass
class FieldFlag:
    """A single validation finding."""
    field_path: str          # e.g. "line_items[2].amount" or "grand_total"
    severity: str            # "error" | "warning"
    message: str             # human-readable explanation
    expected: Any = None     # what the math says it should be
    actual: Any = None       # what the AI returned


@dataclass
class ValidationResult:
    """The full outcome of validating a bill."""
    is_valid: bool = True
    flags: list = field(default_factory=list)
    corrections: dict = field(default_factory=dict)
    confidence_override: str | None = None  # if math fails, cap confidence

    def error(self, path: str, message: str, expected=None, actual=None):
        self.is_valid = False
        self.flags.append(FieldFlag(path, "error", message, expected, actual))

    def warn(self, path: str, message: str, expected=None, actual=None):
        self.flags.append(FieldFlag(path, "warning", message, expected, actual))

    @property
    def errors(self) -> list:
        return [f for f in self.flags if f.severity == "error"]


# Tolerance for float comparison. Rs 0.50 is a common printed rounding.
TOLERANCE = 0.50

# A bill dated further ahead than this is a misread year, not a real date.
FUTURE_GRACE = timedelta(days=2)


def _n(value) -> float:
    """Coerce anything the model returned into a float. None/'' becomes 0."""
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _close(a, b) -> bool:
    return abs(_n(a) - _n(b)) <= TOLERANCE


def _round(value: float) -> float:
    """Keep flag payloads readable: 59.999999999 becomes 60.0."""
    return round(value + 0.0, 2)


def validate_bill(bill: dict, today: date | None = None) -> ValidationResult:
    """
    Run every reconciliation check against an extracted bill.

    Checks performed (in order):
      1. The extractor did not reject the image
      2. Required header fields present, date is sane
      3. At least one line item exists
      4. Each line: qty x rate == amount, and neither is negative
      5. Sum of line amounts == subtotal
      6. Tax breakdown: cgst + sgst + igst == tax_amount
      7. subtotal + tax_amount + round_off == grand_total
      8. Grand total is positive
    """
    result = ValidationResult()
    today = today or date.today()

    # --- 1. The extractor may have rejected the image outright ---
    if bill.get("error"):
        result.error(
            "error",
            bill.get("error_reason") or f"Extraction failed: {bill['error']}",
            actual=bill["error"],
        )
        result.confidence_override = "low"
        return result

    # --- 2. Header sanity ---
    if not bill.get("vendor_name"):
        result.warn("vendor_name", "Vendor name is missing or empty")

    if not bill.get("bill_date"):
        result.warn(
            "bill_date",
            "Bill date could not be parsed. "
            f"Raw value: {bill.get('bill_date_raw') or '(none)'}",
            actual=bill.get("bill_date_raw"),
        )
    else:
        try:
            parsed = date.fromisoformat(str(bill["bill_date"]))
        except ValueError:
            result.error(
                "bill_date",
                f"Bill date {bill['bill_date']!r} is not a valid YYYY-MM-DD date",
                actual=bill["bill_date"],
            )
        else:
            if parsed > today + FUTURE_GRACE:
                result.warn(
                    "bill_date",
                    f"Bill is dated {parsed.isoformat()}, in the future. "
                    "The year or the day/month order may have been misread.",
                    actual=bill["bill_date"],
                )

    gstin = bill.get("vendor_gstin")
    if gstin and len(str(gstin).strip()) != 15:
        result.warn(
            "vendor_gstin",
            f"GSTIN should be 15 characters, got {len(str(gstin).strip())}",
            expected=15,
            actual=gstin,
        )

    # --- 3. Line items must exist ---
    lines = bill.get("line_items") or []
    if not lines:
        result.error("line_items", "No line items were extracted")
        result.confidence_override = "low"
        return result  # nothing further can be reconciled

    # --- 4. Each line: qty x rate == amount ---
    for i, line in enumerate(lines):
        qty = _n(line.get("quantity"))
        rate = _n(line.get("rate"))
        amount = _n(line.get("amount"))
        expected = _round(qty * rate)

        if qty < 0 or rate < 0:
            result.warn(
                f"line_items[{i}].quantity",
                f"Line {i + 1}: negative quantity or rate "
                f"(qty {qty}, rate {rate})",
                actual=qty,
            )

        if not _close(expected, amount):
            result.error(
                f"line_items[{i}].amount",
                f"Line {i + 1}: {qty} x {rate} = {expected}, "
                f"but amount is {amount}",
                expected=expected,
                actual=amount,
            )

    # --- 5. Sum of line amounts == subtotal ---
    line_sum = _round(sum(_n(ln.get("amount")) for ln in lines))
    subtotal = _n(bill.get("subtotal"))
    subtotal_ok = _close(line_sum, subtotal)

    if not subtotal_ok:
        result.error(
            "subtotal",
            f"Line items sum to {line_sum}, but subtotal is {subtotal}",
            expected=line_sum,
            actual=subtotal,
        )
        # Trust the line sum: the subtotal is the number usually misread.
        result.corrections["subtotal"] = line_sum

    # --- 6. Tax breakdown ---
    cgst = _n(bill.get("cgst"))
    sgst = _n(bill.get("sgst"))
    igst = _n(bill.get("igst"))
    tax_amount = _n(bill.get("tax_amount"))
    tax_sum = _round(cgst + sgst + igst)

    if tax_sum > 0 and not _close(tax_sum, tax_amount):
        result.warn(
            "tax_amount",
            f"CGST({cgst}) + SGST({sgst}) + IGST({igst}) = {tax_sum}, "
            f"but tax_amount is {tax_amount}",
            expected=tax_sum,
            actual=tax_amount,
        )
        result.corrections["tax_amount"] = tax_sum
        tax_amount = tax_sum

    # --- 7. Grand total = subtotal + tax + round_off ---
    round_off = _n(bill.get("round_off"))
    grand_total = _n(bill.get("grand_total"))

    # Compare against both readings of the subtotal. If the grand total ties
    # out against either one, the discrepancy is isolated to the subtotal and
    # flagging grand_total too would just be noise.
    from_reported = _round(subtotal + tax_amount + round_off)
    from_lines = _round(line_sum + tax_amount + round_off)

    if not (_close(from_reported, grand_total) or _close(from_lines, grand_total)):
        detail = f"{from_lines}" if subtotal_ok else \
                 f"{from_reported} (or {from_lines} using the line sum)"
        result.error(
            "grand_total",
            f"Subtotal({subtotal}) + Tax({tax_amount}) + Round({round_off}) "
            f"= {detail}, but grand_total is {grand_total}",
            expected=from_lines if subtotal_ok else from_reported,
            actual=grand_total,
        )

    # --- 8. Grand total positive ---
    if grand_total <= 0:
        result.error(
            "grand_total",
            "Grand total must be greater than zero",
            actual=grand_total,
        )

    # --- Downgrade confidence if any hard check failed ---
    if not result.is_valid:
        result.confidence_override = "low"

    return result


def apply_validation(bill: dict, autocorrect: bool = False) -> dict:
    """
    Run validation and attach the results to a copy of the bill.

    Adds four keys:
      _validation_ok:         bool
      _validation_flags:      list of flag dicts
      _validation_corrections: {field: value the math suggests}
      _effective_confidence:  confidence after math overrides

    With autocorrect=True the suggested corrections are written into the
    returned bill. The flags stay either way, so a human still sees what moved.
    """
    result = validate_bill(bill)
    out = dict(bill)

    if autocorrect:
        out.update(result.corrections)

    out["_validation_ok"] = result.is_valid
    out["_validation_flags"] = [
        {
            "field_path": f.field_path,
            "severity": f.severity,
            "message": f.message,
            "expected": f.expected,
            "actual": f.actual,
        }
        for f in result.flags
    ]
    out["_validation_corrections"] = result.corrections
    out["_effective_confidence"] = (
        result.confidence_override or bill.get("confidence") or "medium"
    )
    return out
