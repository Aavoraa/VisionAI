# backend/app/schemas.py
"""Pydantic schemas for the HTTP surface."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .extraction.prompt import CATEGORIES, PAYMENT_METHODS

Category = Literal[tuple(CATEGORIES)]  # type: ignore[valid-type]
PaymentMethod = Literal[tuple(PAYMENT_METHODS)]  # type: ignore[valid-type]


class LineItemIn(BaseModel):
    description: str = ""
    quantity: float = 1
    unit: str | None = None
    rate: float = 0
    amount: float = 0
    tax_percent: float | None = None


class BillIn(BaseModel):
    """A bill as edited on the review screen."""

    vendor_name: str | None = None
    vendor_gstin: str | None = None
    bill_date: date | None = None
    bill_date_raw: str | None = None
    bill_number: str | None = None
    payment_method: PaymentMethod = "unknown"
    category: Category = "miscellaneous"
    line_items: list[LineItemIn] = Field(default_factory=list)
    subtotal: float = 0
    cgst: float | None = None
    sgst: float | None = None
    igst: float | None = None
    tax_amount: float = 0
    round_off: float = 0
    grand_total: float = 0
    confidence: Literal["high", "medium", "low"] = "medium"

    @field_validator("vendor_name", "bill_number", "vendor_gstin")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


class ValidationFlagOut(BaseModel):
    field_path: str
    severity: Literal["error", "warning"]
    message: str
    expected: float | str | None = None
    actual: float | str | None = None


class ValidationOut(BaseModel):
    ok: bool
    flags: list[ValidationFlagOut]
    corrections: dict[str, float] = Field(default_factory=dict)
    effective_confidence: str


class ConfirmIn(BaseModel):
    """Body of POST /api/bills/{id}/confirm."""

    bill: BillIn
    train: bool = False
    vendor_notes: str | None = None


class BillSummary(BaseModel):
    id: int
    status: str
    vendor_name: str | None
    bill_date: date | None
    category: str
    payment_method: str
    grand_total: float
    effective_confidence: str
    validation_ok: bool
    created_at: str | None
