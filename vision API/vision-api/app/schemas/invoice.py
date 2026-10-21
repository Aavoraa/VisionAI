"""Structured invoice output (Indian GST invoices first, but nothing here is GST-only).

Every extracted value is wrapped in ``Extracted``: the value itself (``None`` when it was not
found; never invented), a ``status``, the text it was read from, and a ``confidence`` slot
that Phase 4 fills in. Amounts are exact ``Decimal`` values; dates are ISO ``YYYY-MM-DD``.
"""

import datetime as dt
from decimal import Decimal
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict
from pydantic import Field

from app.schemas.common import UTCDateTime

T = TypeVar("T")

FieldStatus = Literal["ok", "check", "missing"]


class Extracted(BaseModel, Generic[T]):
    """One extracted value and how much to trust it."""

    model_config = ConfigDict(frozen=True)

    value: T | None = None
    status: FieldStatus = Field(
        default="missing",
        description="`ok`: found and consistent. `check`: found but failed a check or looks unusual. "
        "`missing`: not found (the value is `null`, never guessed).",
    )
    source_text: str | None = Field(default=None, description="Text the value was read from.")
    confidence: float | None = Field(default=None, ge=0, le=1, description="0-1; set from Phase 4 on.")
    note: str | None = Field(default=None, description="Why the field needs checking, if it does.")

    @classmethod
    def found(cls, value: T, source_text: str | None = None) -> "Extracted[T]":
        return cls(value=value, status="ok", source_text=source_text)

    @classmethod
    def check(cls, value: T | None, note: str, source_text: str | None = None) -> "Extracted[T]":
        return cls(value=value, status="check", note=note, source_text=source_text)


def _missing() -> Extracted:  # type: ignore[type-arg]
    return Extracted()


class LineItem(BaseModel):
    line_number: Extracted[int] = Field(default_factory=_missing)
    description: Extracted[str] = Field(default_factory=_missing)
    # Garment invoices with a size matrix: one line per style (see size_matrix_items), with the
    # style code as design, its sizes as "16,18,20" and the average MRP.
    design: Extracted[str] = Field(default_factory=_missing)
    size: Extracted[str] = Field(default_factory=_missing)
    hsn_sac: Extracted[str] = Field(default_factory=_missing)
    quantity: Extracted[Decimal] = Field(default_factory=_missing)
    unit: Extracted[str] = Field(default_factory=_missing)
    rate: Extracted[Decimal] = Field(default_factory=_missing)
    mrp: Extracted[Decimal] = Field(default_factory=_missing)
    discount_percent: Extracted[Decimal] = Field(default_factory=_missing)
    taxable_value: Extracted[Decimal] = Field(default_factory=_missing)
    tax_rate: Extracted[Decimal] = Field(default_factory=_missing)
    tax_amount: Extracted[Decimal] = Field(default_factory=_missing)
    total: Extracted[Decimal] = Field(default_factory=_missing)


class Invoice(BaseModel):
    invoice_number: Extracted[str] = Field(default_factory=_missing)
    invoice_date: Extracted[dt.date] = Field(default_factory=_missing)
    vendor_name: Extracted[str] = Field(default_factory=_missing)
    vendor_address: Extracted[str] = Field(default_factory=_missing)
    vendor_gstin: Extracted[str] = Field(default_factory=_missing)
    buyer_name: Extracted[str] = Field(default_factory=_missing)
    buyer_address: Extracted[str] = Field(default_factory=_missing)
    buyer_gstin: Extracted[str] = Field(default_factory=_missing)
    place_of_supply: Extracted[str] = Field(default_factory=_missing)
    delivery_note: Extracted[str] = Field(default_factory=_missing)
    payment_terms: Extracted[str] = Field(default_factory=_missing)
    currency: Extracted[str] = Field(default_factory=_missing)
    items: list[LineItem] = Field(default_factory=list)
    total_quantity: Extracted[Decimal] = Field(default_factory=_missing)
    subtotal: Extracted[Decimal] = Field(default_factory=_missing)
    cgst: Extracted[Decimal] = Field(default_factory=_missing)
    sgst: Extracted[Decimal] = Field(default_factory=_missing)
    igst: Extracted[Decimal] = Field(default_factory=_missing)
    discount: Extracted[Decimal] = Field(default_factory=_missing)
    round_off: Extracted[Decimal] = Field(default_factory=_missing)
    total_amount: Extracted[Decimal] = Field(default_factory=_missing)


class InvoiceDetails(BaseModel):
    """Further details for purchase entry (e-invoice, transport, order, bank, contacts, derived
    states and rates). Not validated or scored; a value derived rather than read says so in its note."""

    # e-invoice
    irn: Extracted[str] = Field(default_factory=_missing)
    ack_no: Extracted[str] = Field(default_factory=_missing)
    ack_date: Extracted[dt.date] = Field(default_factory=_missing)
    eway_bill: Extracted[str] = Field(default_factory=_missing)
    # order and dispatch
    due_date: Extracted[dt.date] = Field(default_factory=_missing)
    order_no: Extracted[str] = Field(default_factory=_missing)
    order_date: Extracted[dt.date] = Field(default_factory=_missing)
    reference_no: Extracted[str] = Field(default_factory=_missing)
    reference_date: Extracted[dt.date] = Field(default_factory=_missing)
    challan_no: Extracted[str] = Field(default_factory=_missing)
    agent: Extracted[str] = Field(default_factory=_missing)
    broker: Extracted[str] = Field(default_factory=_missing)
    lr_no: Extracted[str] = Field(default_factory=_missing)
    lr_date: Extracted[dt.date] = Field(default_factory=_missing)
    transporter: Extracted[str] = Field(default_factory=_missing)
    destination: Extracted[str] = Field(default_factory=_missing)
    # seller contacts and bank
    vendor_pan: Extracted[str] = Field(default_factory=_missing)
    vendor_phone: Extracted[str] = Field(default_factory=_missing)
    vendor_email: Extracted[str] = Field(default_factory=_missing)
    vendor_state: Extracted[str] = Field(default_factory=_missing)
    vendor_state_code: Extracted[str] = Field(default_factory=_missing)
    buyer_state: Extracted[str] = Field(default_factory=_missing)
    buyer_state_code: Extracted[str] = Field(default_factory=_missing)
    bank_name: Extracted[str] = Field(default_factory=_missing)
    bank_account: Extracted[str] = Field(default_factory=_missing)
    bank_ifsc: Extracted[str] = Field(default_factory=_missing)
    bank_branch: Extracted[str] = Field(default_factory=_missing)
    # amounts
    cgst_rate: Extracted[Decimal] = Field(default_factory=_missing)
    sgst_rate: Extracted[Decimal] = Field(default_factory=_missing)
    igst_rate: Extracted[Decimal] = Field(default_factory=_missing)
    tax_total: Extracted[Decimal] = Field(default_factory=_missing)
    gross_amount: Extracted[Decimal] = Field(default_factory=_missing)
    amount_in_words: Extracted[str] = Field(default_factory=_missing)


class ProgressEvent(BaseModel):
    t: float = Field(description="Seconds since the extraction started.")
    stage: str = Field(description="Pipeline step: start, deskew, locate, header, table, bands, footer, rules, items, crosscheck, zoom, details, validate, result.")
    text: str
    level: Literal["info", "ok", "warn"] = "info"
    page: int | None = None
    region: list[float] | None = Field(default=None, description="Where on the page: x1, y1, x2, y2 as fractions.")


class InvoiceProgress(BaseModel):
    events: list[ProgressEvent]
    count: int = Field(description="Comments so far (pass as `after` next time).")
    done: bool = Field(description="The extraction has finished (or stopped).")


class ValidationCheck(BaseModel):
    id: str = Field(description="Stable check id, e.g. `grand_total`.")
    name: str = Field(description="Short human-readable name.")
    status: Literal["pass", "fail", "skip"] = Field(description="`skip`: the check could not run (see message).")
    message: str
    fields: list[str] = Field(default_factory=list, description="Invoice fields a failure concerns (marked `check`).")


class ValidationReport(BaseModel):
    status: Literal["passed", "failed"] = Field(description="`failed` if any check failed.")
    checks: list[ValidationCheck]


class ExtractionUsage(BaseModel):
    input_tokens: int = Field(default=0, ge=0, description="Tokens read by the models (pages count as image tokens).")
    output_tokens: int = Field(default=0, ge=0, description="Tokens generated by the models.")


ReviewStatus = Literal["processing", "needs_review", "approved", "reprocess", "failed"]


class InvoiceExtraction(BaseModel):
    """Result of one extraction run."""

    id: str | None = Field(default=None, description="Stored invoice id (open it in the dashboard); null when not stored.")
    review_status: ReviewStatus | None = Field(
        default=None,
        description="`approved` (automatically, when confident), `needs_review` or `reprocess`; null when not stored.",
    )
    confidence: float | None = Field(
        default=None, ge=0, le=1, description="Lowest confidence among the key fields (heuristic, 0-1)."
    )
    invoice: Invoice
    details: InvoiceDetails = Field(
        default_factory=InvoiceDetails,
        description="Further purchase-entry details (e-invoice, transport, bank, ...); not validated or scored.",
    )
    model: str = Field(description="OCR model that read the document.")
    reasoning_model: str | None = Field(
        default=None, description="Model that cross-checked the fields; null when only the rules were used."
    )
    pages: int
    raw_text: str = Field(description="OCR text, page by page, for review and debugging.")
    warnings: list[str] = Field(default_factory=list)
    validation: ValidationReport | None = Field(
        default=None, description="Arithmetic, GST and format checks (Phase 3); null for results made before them."
    )
    usage: ExtractionUsage = Field(default_factory=ExtractionUsage)
    processing_ms: int


def _ok(value: object, source: str | None = None, note: str | None = None) -> dict[str, object]:
    return {"value": value, "status": "ok", "source_text": source, "confidence": None, "note": note}


# Documentation example (made-up company data, GSTINs with valid checksums).
EXAMPLE_EXTRACTION: dict[str, object] = {
    "id": "5b0e7c1a-9d2f-4e3b-8a6c-1f2e3d4c5b6a",
    "review_status": "needs_review",
    "confidence": 0.75,
    "invoice": {
        "invoice_number": _ok("AB/7/26-27", "Invoice No.: AB/7/26-27", "confirmed by Qwen3-VL-2B-Instruct"),
        "invoice_date": _ok("2026-08-03", "Dated: 03-Aug-26", "confirmed by Qwen3-VL-2B-Instruct"),
        "vendor_name": _ok("ACME TEXTILES PRIVATE LIMITED", "for ACME TEXTILES PRIVATE LIMITED"),
        "vendor_address": _ok("12 MILL STREET, BENGALURU-560001", "12 MILL STREET\nBENGALURU-560001"),
        "vendor_gstin": _ok("29ABCDE1234F1ZW", "GSTIN/UIN: 29ABCDE1234F1ZW", "confirmed by Qwen3-VL-2B-Instruct"),
        "buyer_name": _ok("NOVA FASHIONS PRIVATE LIMITED", "Buyer (Bill to)"),
        "buyer_address": _ok("4 MARKET ROAD, PUNE-411001", "4 MARKET ROAD\nPUNE-411001"),
        "buyer_gstin": _ok("27PQRST6789K1ZW", "GSTIN/UIN : 27PQRST6789K1ZW"),
        "place_of_supply": {
            "value": "Maharashtra (27)", "status": "check", "source_text": "Maharashtra, Code : 27", "confidence": None,
            "note": "no 'Place of Supply' printed; taken from the buyer's state",
        },
        "currency": _ok("INR", "INR/₹ on the invoice"),
        "items": [
            {
                "line_number": _ok(1, "1 COTTON SHIRT"), "description": _ok("COTTON SHIRT", "1 COTTON SHIRT"),
                "hsn_sac": _ok("6205", "6205"), "quantity": _ok("2", "2 pcs"), "unit": _ok("pcs", "2 pcs"),
                "rate": _ok("500.00", "500.00"), "discount_percent": _ok("10", "10 %"),
                "taxable_value": _ok("900.00", "900.00"),
            }
        ],
        "total_quantity": _ok("2", "2 pcs"),
        "subtotal": _ok("900.00", "900.00"),
        "igst": _ok("45.00", "45.00"),
        "total_amount": _ok("945.00", "₹ 945.00 E & OE", "confirmed by Qwen3-VL-2B-Instruct"),
    },
    "model": "PaddleOCR-VL-1.6-GGUF",
    "reasoning_model": "Qwen3-VL-2B-Instruct",
    "pages": 1,
    "raw_text": "--- header OCR (page 1, top 35%) ---\nTax Invoice\n...",
    "warnings": [],
    "validation": {
        "status": "passed",
        "checks": [
            {"id": "line_arithmetic", "name": "Line amounts", "status": "pass",
             "message": "Quantity × rate − discount = amount on all 1 lines", "fields": []},
            {"id": "grand_total", "name": "Grand total", "status": "pass",
             "message": "900.00 + taxes 45.00 = 945.00", "fields": []},
        ],
    },
    "usage": {"input_tokens": 4210, "output_tokens": 1980},
    "processing_ms": 142000,
}


# ---------------------------------------------------------------------------
# Stored invoices (dashboard)
# ---------------------------------------------------------------------------

class InvoiceSummary(BaseModel):
    id: str
    filename: str
    status: ReviewStatus
    confidence: float | None = Field(default=None, description="Lowest confidence among the key fields; null while processing.")
    invoice_number: str | None = None
    vendor_name: str | None = None
    total_amount: Decimal | None = None
    pages: int
    error: str | None = Field(default=None, description="Why processing failed (status `failed`).")
    created_at: UTCDateTime
    updated_at: UTCDateTime
    approved_at: UTCDateTime | None = None
    approved_automatically: bool = Field(default=False, description="Approved by the confidence rules, not a person.")


class InvoiceList(BaseModel):
    items: list[InvoiceSummary]
    total: int = Field(description="Invoices matching the filter.")
    counts: dict[str, int] = Field(description="Invoices per status (all of the user's invoices).")
    limit: int
    offset: int


class InvoiceCorrectionOut(BaseModel):
    field: str = Field(description='Field path: "vendor_name", or "items.3.rate" (0-based item index).')
    old_value: str | None = None
    new_value: str | None = None
    old_status: str | None = None
    corrected_by: str | None = Field(default=None, description="Reviewer's e-mail; null if the account was deleted.")
    created_at: UTCDateTime


class InvoiceDetail(InvoiceSummary):
    content_type: str
    files_available: bool = Field(
        default=True, description="False once the retention policy has deleted the upload and page images."
    )
    extraction: InvoiceExtraction | None = Field(default=None, description="Current values, with corrections; null while processing.")
    corrections: list[InvoiceCorrectionOut]


class InvoiceCorrections(BaseModel):
    """Values to change. Empty string or null clears a value."""

    model_config = ConfigDict(extra="forbid")

    fields: dict[str, str | None] = Field(
        min_length=1,
        max_length=500,
        description='Field path -> new value as text, e.g. {"vendor_name": "ACME LTD", "items.0.rate": "500.00"}.',
    )


# ---------------------------------------------------------------------------
# Integration (API key): clean values for ERP / accounting systems
# ---------------------------------------------------------------------------

_EXAMPLE_ID = "5b0e7c1a-9d2f-4e3b-8a6c-1f2e3d4c5b6a"
EXAMPLE_API_INVOICE: dict[str, object] = {
    "id": _EXAMPLE_ID,
    "status": "approved",
    "confidence": 0.93,
    "filename": "invoice-aug.pdf",
    "pages": 1,
    "created_at": "2026-09-26T10:15:00+00:00",
    "updated_at": "2026-09-26T10:17:22+00:00",
    "approved_at": "2026-09-26T10:17:22+00:00",
    "approved_automatically": True,
    "validation": "passed",
    "data": {
        "invoice_number": "AB/7/26-27", "invoice_date": "2026-08-03", "vendor_name": "ACME TEXTILES PRIVATE LIMITED",
        "vendor_gstin": "29ABCDE1234F1ZW", "buyer_name": "NOVA FASHIONS PRIVATE LIMITED", "buyer_gstin": "27PQRST6789K1ZW",
        "currency": "INR", "subtotal": "900.00", "igst": "45.00", "total_amount": "945.00",
        "items": [{"line_number": 1, "description": "COTTON SHIRT", "hsn_sac": "6205", "quantity": "2", "unit": "pcs",
                   "rate": "500.00", "discount_percent": "10", "taxable_value": "900.00"}],
    },
    "flags": [],
}


class InvoiceAccepted(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"id": _EXAMPLE_ID, "review_status": "processing", "status_url": f"/v1/invoices/{_EXAMPLE_ID}"}]}
    )

    id: str
    review_status: Literal["processing"] = "processing"
    status_url: str = Field(description="Poll this (with the same API key) until `status` is no longer `processing`.")


class ApiInvoice(BaseModel):
    """A stored invoice as other systems see it: review state, plain values, and what to check."""

    model_config = ConfigDict(json_schema_extra={"examples": [EXAMPLE_API_INVOICE]})

    id: str
    status: ReviewStatus
    confidence: float | None = None
    filename: str
    pages: int
    created_at: str
    updated_at: str
    approved_at: str | None = None
    approved_automatically: bool = False
    validation: Literal["passed", "failed"] | None = None
    data: dict[str, object] | None = Field(
        default=None,
        description="Field values only: amounts as decimal strings, dates as YYYY-MM-DD, null when absent, "
        "`items` as a list. Null while processing or after a failure.",
    )
    flags: list[str] = Field(default_factory=list, description='Values marked for checking: "vendor_name", "items.3.rate".')


class ApiInvoiceList(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"items": [EXAMPLE_API_INVOICE], "total": 1, "limit": 50, "offset": 0}]}
    )

    items: list[ApiInvoice]
    total: int
    limit: int
    offset: int
