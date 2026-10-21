"""Phase 4: confidence per field and routing (approve / review / reprocess).

The scores are **heuristics, not calibrated probabilities**: they combine the signals the
pipeline already has, and the thresholds are settings to be tuned on the benchmark (Phase 6).

Per field (items: per cell):

* rule reading, status ``ok``                          0.80
* + confirmed by the reasoning model                   +0.10
* + vouched for by passed validation checks            +0.05 per check, at most +0.10
* status ``check`` (disagreement, derived value, ...)  0.75
* involved in a failed validation check                0.50
* corrected by a reviewer (and consistent)             1.00
* no value                                             null (nothing to trust)

Invoice confidence is the lowest score among the key fields; a missing required field counts
as 0.75 (a reviewer can fill it in). Routing:

* confidence >= auto-approve threshold and validation passed -> ``approved``
* confidence >= review threshold                              -> ``needs_review``
* otherwise                                                   -> ``reprocess``

Any flagged line item keeps the invoice out of automatic approval. Because only independent
confirmation lifts a field above 0.90, nothing is auto-approved without a reasoning model.
"""

from typing import Any, Literal

from app.schemas.invoice import Extracted, Invoice, LineItem, ValidationReport

Route = Literal["approved", "needs_review", "reprocess"]

KEY_FIELDS = (
    "invoice_number", "invoice_date", "vendor_name", "vendor_gstin", "buyer_name",
    "buyer_gstin", "subtotal", "total_amount",
)
REQUIRED_FIELDS = ("invoice_number", "invoice_date", "vendor_name", "vendor_gstin", "buyer_name", "total_amount")

# Fields each validation check vouches for when it passes.
CHECK_FIELDS: dict[str, tuple[str, ...]] = {
    "invoice_date": ("invoice_date",),
    "vendor_gstin": ("vendor_gstin",),
    "buyer_gstin": ("buyer_gstin",),
    "items_sum": ("subtotal",),
    "quantity_sum": ("total_quantity",),
    "tax_type": ("cgst", "sgst", "igst"),
    "cgst_sgst": ("cgst", "sgst"),
    "tax_rate": ("cgst", "sgst", "igst", "subtotal"),
    "round_off": ("round_off",),
    "grand_total": ("subtotal", "cgst", "sgst", "igst", "round_off", "total_amount"),
    "amount_in_words": ("total_amount",),
    "tax_in_words": ("cgst", "sgst", "igst"),
}
ITEM_CHECK_FIELDS: dict[str, tuple[str, ...]] = {
    "line_numbers": ("line_number",),
    "line_arithmetic": ("quantity", "rate", "discount_percent", "taxable_value"),
}

READ = 0.80
CONFIRMED_BONUS = 0.10
CHECK_BONUS = 0.05
MAX_CHECK_BONUS = 0.10
FLAGGED = 0.75
FAILED = 0.50


def _score(field: Extracted[Any], passed: int, failed: bool) -> float | None:
    if field.value is None:
        return None
    if failed:
        return FAILED
    if field.note and "corrected by" in field.note:
        return 1.0  # entered by a reviewer
    if field.status == "check":
        return FLAGGED
    score = READ
    if field.note and "confirmed by" in field.note:
        score += CONFIRMED_BONUS
    score += min(MAX_CHECK_BONUS, CHECK_BONUS * passed)
    return round(min(score, 0.99), 2)


def score_invoice(
    invoice: Invoice, validation: ValidationReport | None, *, auto_approve: float, review: float
) -> tuple[Invoice, float, Route]:
    """Invoice with ``confidence`` on every field, the invoice confidence and its route."""
    passed: dict[str, int] = {}
    failed: set[str] = set()
    item_passed: dict[str, int] = {}
    for check in validation.checks if validation else []:
        if check.status == "pass":
            for name in CHECK_FIELDS.get(check.id, ()):
                passed[name] = passed.get(name, 0) + 1
            for name in ITEM_CHECK_FIELDS.get(check.id, ()):
                item_passed[name] = item_passed.get(name, 0) + 1
        elif check.status == "fail":
            failed.update(check.fields)

    updates: dict[str, Any] = {}
    for name in Invoice.model_fields:
        if name == "items":
            continue
        field: Extracted[Any] = getattr(invoice, name)
        updates[name] = field.model_copy(update={"confidence": _score(field, passed.get(name, 0), name in failed)})
    items: list[LineItem] = []
    for item in invoice.items:
        cells = {}
        for name in LineItem.model_fields:
            cell: Extracted[Any] = getattr(item, name)
            cells[name] = cell.model_copy(update={"confidence": _score(cell, item_passed.get(name, 0), False)})
        items.append(item.model_copy(update=cells))
    updates["items"] = items
    scored = invoice.model_copy(update=updates)

    key_scores = []
    for name in KEY_FIELDS:
        value = getattr(scored, name).confidence
        if value is not None:
            key_scores.append(value)
        elif name in REQUIRED_FIELDS:
            key_scores.append(FLAGGED)  # missing: a reviewer can add it
    confidence = min(key_scores) if key_scores else 0.0
    any_item_flagged = any(getattr(item, n).status == "check" for item in scored.items for n in LineItem.model_fields)
    if any_item_flagged:
        confidence = min(confidence, FLAGGED)
    if validation is not None and validation.status == "failed":
        confidence = min(confidence, FLAGGED)
    # Arithmetic that does not add up after OCR usually means a misread: read the document again.
    if failed & {"total_amount", "subtotal"}:
        confidence = min(confidence, FAILED)

    passed_all = validation is not None and validation.status == "passed"
    route: Route
    if confidence >= auto_approve and passed_all and not any_item_flagged:
        route = "approved"
    elif confidence >= review:
        route = "needs_review"
    else:
        route = "reprocess"
    return scored, round(confidence, 2), route
