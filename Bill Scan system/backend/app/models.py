# backend/app/models.py
"""SQLAlchemy models plus the engine/session wiring."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON, Date, DateTime, Float, Integer, String, Text, create_engine,
    inspect, text,
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, sessionmaker,
)
from sqlalchemy.schema import CreateColumn

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Bill(Base):
    """
    One scanned bill.

    Header fields are columns because they are filtered and summed on; line
    items ride along as JSON because they are only ever read with their bill.
    `raw_extraction` keeps what the model actually returned, before any human
    edit, so extraction quality can be measured later.
    """

    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Provenance
    image_path: Mapped[str] = mapped_column(String(512), default="")
    image_mime: Mapped[str] = mapped_column(String(64), default="image/jpeg")
    # "ai" when a vision call produced the numbers, "manual" when a person
    # typed them in. Confidence means nothing for a hand-entered bill.
    source: Mapped[str] = mapped_column(
        String(16), default="ai", server_default="ai"
    )
    status: Mapped[str] = mapped_column(String(32), default="pending_review")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    # Header
    vendor_name: Mapped[str | None] = mapped_column(String(255))
    vendor_gstin: Mapped[str | None] = mapped_column(String(32))
    bill_date: Mapped[date | None] = mapped_column(Date)
    bill_date_raw: Mapped[str | None] = mapped_column(String(64))
    bill_number: Mapped[str | None] = mapped_column(String(64))
    payment_method: Mapped[str] = mapped_column(String(16), default="unknown")
    category: Mapped[str] = mapped_column(String(32), default="miscellaneous")

    # Money
    subtotal: Mapped[float] = mapped_column(Float, default=0.0)
    cgst: Mapped[float | None] = mapped_column(Float)
    sgst: Mapped[float | None] = mapped_column(Float)
    igst: Mapped[float | None] = mapped_column(Float)
    tax_amount: Mapped[float] = mapped_column(Float, default=0.0)
    round_off: Mapped[float] = mapped_column(Float, default=0.0)
    grand_total: Mapped[float] = mapped_column(Float, default=0.0)

    # Extraction bookkeeping
    line_items: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[str] = mapped_column(String(8), default="medium")
    effective_confidence: Mapped[str] = mapped_column(String(8), default="medium")
    validation_ok: Mapped[bool] = mapped_column(default=True)
    validation_flags: Mapped[list] = mapped_column(JSON, default=list)
    raw_extraction: Mapped[dict] = mapped_column(JSON, default=dict)
    extraction_usage: Mapped[dict] = mapped_column(JSON, default=dict)
    edited_fields: Mapped[list] = mapped_column(JSON, default=list)

    def to_dict(self) -> dict:
        """The wire shape the review screen edits."""
        return {
            "id": self.id,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "_source": self.source,
            "_has_image": bool(self.image_path),
            "vendor_name": self.vendor_name,
            "vendor_gstin": self.vendor_gstin,
            "bill_date": self.bill_date.isoformat() if self.bill_date else None,
            "bill_date_raw": self.bill_date_raw,
            "bill_number": self.bill_number,
            "payment_method": self.payment_method,
            "category": self.category,
            "line_items": self.line_items or [],
            "subtotal": self.subtotal,
            "cgst": self.cgst,
            "sgst": self.sgst,
            "igst": self.igst,
            "tax_amount": self.tax_amount,
            "round_off": self.round_off,
            "grand_total": self.grand_total,
            "confidence": self.confidence,
            "_effective_confidence": self.effective_confidence,
            "_validation_ok": self.validation_ok,
            "_validation_flags": self.validation_flags or [],
        }


class VendorProfile(Base):
    """
    What "Save & Train Vendor" writes.

    One row per supplier, holding the details a human has confirmed. It is fed
    back into the prompt the next time a bill from that supplier is scanned.
    """

    __tablename__ = "vendor_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    vendor_name: Mapped[str] = mapped_column(String(255))
    vendor_gstin: Mapped[str | None] = mapped_column(String(32))
    category: Mapped[str | None] = mapped_column(String(32))
    payment_method: Mapped[str | None] = mapped_column(String(16))
    common_items: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(Text)
    confirmed_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    def to_dict(self) -> dict:
        return {
            "vendor_key": self.vendor_key,
            "vendor_name": self.vendor_name,
            "vendor_gstin": self.vendor_gstin,
            "category": self.category,
            "payment_method": self.payment_method,
            "common_items": self.common_items or [],
            "notes": self.notes,
            "confirmed_count": self.confirmed_count,
        }


engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """
    Add columns that exist on the model but not yet in the database file.

    create_all() only creates whole tables, so a database written by an
    earlier version keeps its old shape and every query against a new column
    fails. This is not a substitute for real migrations (it cannot drop, rename,
    or backfill) but it covers adding a column, which is the only schema change
    this project has needed. Any new column must carry a server_default, or
    SQLite refuses to add it to a table that already has rows.
    """
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = CreateColumn(column).compile(engine).string
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}"))


def get_db():
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
