"""Phase 5: invoices in plain, integration-friendly shapes (API, exports, webhook).

``clean_invoice`` drops the per-field metadata (status, confidence, source text, note) and keeps
only values, so an ERP can map it directly: amounts as decimal strings, dates as ISO strings,
``null`` for absent fields. Fields a person should still look at are listed in ``flags``.
"""

import csv
import io
from decimal import Decimal
from typing import Any

from app.database.models import InvoiceRecord
from app.schemas.invoice import Invoice, InvoiceExtraction, LineItem

HEADER_COLUMNS = (
    "invoice_number", "invoice_date", "vendor_name", "vendor_gstin", "vendor_address", "buyer_name", "buyer_gstin",
    "buyer_address", "place_of_supply", "currency", "total_quantity", "subtotal", "cgst", "sgst", "igst", "discount",
    "round_off", "total_amount",
)
ITEM_COLUMNS = ("line_number", "description", "design", "size", "hsn_sac", "quantity", "unit", "rate", "mrp", "discount_percent", "taxable_value")


def _plain(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def invoice_values(invoice: Invoice) -> dict[str, Any]:
    """{"invoice_number": "...", ..., "items": [{...}]} with plain JSON values."""
    values: dict[str, Any] = {
        name: _plain(getattr(invoice, name).value) for name in Invoice.model_fields if name != "items"
    }
    values["items"] = [
        {name: _plain(getattr(item, name).value) for name in LineItem.model_fields} for item in invoice.items
    ]
    return values


def flagged_fields(invoice: Invoice) -> list[str]:
    """Paths of values marked ``check``: "vendor_name", "items.3.rate"."""
    flags = [name for name in Invoice.model_fields if name != "items" and getattr(invoice, name).status == "check"]
    for index, item in enumerate(invoice.items):
        flags += [f"items.{index}.{name}" for name in LineItem.model_fields if getattr(item, name).status == "check"]
    return flags


def clean_record(record: InvoiceRecord) -> dict[str, Any]:
    """An invoice as other systems see it: identity, review state, values and flags."""
    extraction = InvoiceExtraction.model_validate(record.extraction) if record.extraction else None
    return {
        "id": record.id,
        "status": record.status,
        "confidence": record.confidence,
        "filename": record.filename,
        "pages": record.pages,
        "created_at": _plain(record.created_at),
        "updated_at": _plain(record.updated_at),
        "approved_at": _plain(record.approved_at),
        "approved_automatically": record.status == "approved" and record.approved_by is None,
        "validation": extraction.validation.status if extraction and extraction.validation else None,
        "data": invoice_values(extraction.invoice) if extraction else None,
        "flags": flagged_fields(extraction.invoice) if extraction else [],
    }


def invoices_csv(records: list[InvoiceRecord]) -> str:
    """One row per invoice: id, status, confidence, then every header field."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "status", "confidence", "filename", "created_at", "approved_at", *HEADER_COLUMNS, "flags"])
    for record in records:
        clean = clean_record(record)
        data = clean["data"] or {}
        writer.writerow([
            clean["id"], clean["status"], clean["confidence"], clean["filename"], clean["created_at"], clean["approved_at"],
            *(_cell(data.get(column)) for column in HEADER_COLUMNS), " ".join(clean["flags"]),
        ])
    return buffer.getvalue()


def items_csv(records: list[InvoiceRecord]) -> str:
    """One row per line item, with the invoice it belongs to."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["invoice_id", "invoice_number", "vendor_gstin", "invoice_date", *ITEM_COLUMNS])
    for record in records:
        data = clean_record(record)["data"] or {}
        for item in data.get("items", []):
            writer.writerow([
                record.id, _cell(data.get("invoice_number")), _cell(data.get("vendor_gstin")), _cell(data.get("invoice_date")),
                *(_cell(item.get(column)) for column in ITEM_COLUMNS),
            ])
    return buffer.getvalue()


def _cell(value: Any) -> str:
    """A CSV cell; text that a spreadsheet would run as a formula is prefixed with '."""
    text = "" if value is None else str(value)
    return f"'{text}" if text[:1] in ("=", "+", "-", "@", "\t", "\r") and not _is_number(text) else text


def _is_number(text: str) -> bool:
    try:
        Decimal(text)
    except ArithmeticError:  # decimal.InvalidOperation
        return False
    return True
