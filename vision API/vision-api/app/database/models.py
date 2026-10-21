"""SQLAlchemy 2.0 ORM models.

Collections use ``lazy="raise"`` so an accidental implicit load (which would perform
blocking I/O under AsyncSession) fails loudly; load relationships explicitly instead.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _new_id() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    # Dashboard login. NULL for users created only to own API keys (they cannot sign in).
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user", server_default="user")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # NULL = the default plan (whichever plan is currently marked is_default).
    plan_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("plans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    api_keys: Mapped[list["APIKey"]] = relationship(back_populates="user", lazy="raise")
    usage_logs: Mapped[list["UsageLog"]] = relationship(back_populates="user", lazy="raise")
    sessions: Mapped[list["UserSession"]] = relationship(
        back_populates="user", lazy="raise", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (CheckConstraint("role IN ('user', 'admin')", name="ck_users_role"),)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def __repr__(self) -> str:
        # password_hash intentionally omitted.
        return f"User(id={self.id!r}, role={self.role!r}, is_active={self.is_active!r})"


class UserSession(Base):
    """A dashboard login. Only SHA-256 hashes of the session and CSRF tokens are stored."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="sessions", lazy="raise")

    def __repr__(self) -> str:
        return f"UserSession(id={self.id!r}, user_id={self.user_id!r})"


class APIKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Approximate (refreshed at most every few minutes) so API traffic does not become DB writes.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="api_keys", lazy="raise")
    usage_logs: Mapped[list["UsageLog"]] = relationship(back_populates="api_key", lazy="raise")

    __table_args__ = (CheckConstraint("length(key_hash) = 64", name="ck_api_keys_key_hash_len"),)

    def __repr__(self) -> str:
        # key_hash intentionally omitted.
        return f"APIKey(id={self.id!r}, prefix={self.prefix!r}, is_active={self.is_active!r})"


class UsageLog(Base):
    __tablename__ = "usage_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # RESTRICT: keys are revoked (is_active=False), never deleted, so billing history is preserved.
    api_key_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("api_keys.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    image_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    processing_time_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Price of this request in micro-USD (1e-6 USD), fixed at request time from the user's plan.
    cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="usage_logs", lazy="raise")
    api_key: Mapped[APIKey] = relationship(back_populates="usage_logs", lazy="raise")

    __table_args__ = (
        CheckConstraint("status IN ('success', 'error')", name="ck_usage_logs_status"),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND image_count >= 0 AND processing_time_ms >= 0 "
            "AND cost_micros >= 0",
            name="ck_usage_logs_non_negative",
        ),
        Index("ix_usage_logs_user_id_created_at", "user_id", "created_at"),
    )


class Plan(Base):
    """Pricing and monthly quotas. Money is stored as integer micro-USD (1e-6 USD) so sums are exact."""

    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    price_input_per_1k_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    price_output_per_1k_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    price_per_image_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    monthly_request_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)  # NULL = unlimited
    monthly_spend_limit_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # NULL = unlimited
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "price_input_per_1k_micros >= 0 AND price_output_per_1k_micros >= 0 AND price_per_image_micros >= 0 "
            "AND (monthly_request_limit IS NULL OR monthly_request_limit >= 0) "
            "AND (monthly_spend_limit_micros IS NULL OR monthly_spend_limit_micros >= 0)",
            name="ck_plans_non_negative",
        ),
        # At most one default plan.
        Index(
            "uq_plans_single_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default"),
        ),
    )

    def __repr__(self) -> str:
        return f"Plan(id={self.id!r}, name={self.name!r}, is_default={self.is_default!r})"


JOB_STATUSES = ("pending", "processing", "completed", "failed")


class Job(Base):
    """An asynchronous vision request. Lifecycle: pending -> processing -> completed | failed."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    api_key_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("api_keys.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    result_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in JOB_STATUSES) + ")", name="ck_jobs_status"
        ),
        Index("ix_jobs_user_id_created_at", "user_id", "created_at"),
    )

    def __repr__(self) -> str:
        return f"Job(id={self.id!r}, status={self.status!r})"


INVOICE_STATUSES = ("processing", "needs_review", "approved", "reprocess", "failed")


class InvoiceRecord(Base):
    """An uploaded invoice and its extraction.

    Lifecycle: processing -> approved (automatically, when confident) | needs_review | reprocess;
    a reviewer can correct fields and approve, or send it back for reprocessing. ``failed``
    means reprocessing itself failed. The document's files live under
    ``Settings.document_storage_dir/<id>/`` (see ``document_store``).
    """

    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    api_key_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="processing")
    # Lowest confidence among the key fields, 0-1 (see invoice_confidence). NULL while processing.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Copies of a few fields for listing and searching without reading the JSON.
    invoice_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vendor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    total_amount: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The full InvoiceExtraction (current values, including reviewers' corrections).
    extraction: Mapped[dict[str, Any] | None] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), nullable=True)
    # The Invoice as read, before validation flags, with corrections applied: every change is
    # validated and scored afresh from this, so old flags never outlive a fix.
    base_invoice: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in INVOICE_STATUSES) + ")", name="ck_invoices_status"
        ),
        Index("ix_invoices_user_id_created_at", "user_id", "created_at"),
        Index("ix_invoices_user_id_status", "user_id", "status"),
    )

    def __repr__(self) -> str:
        return f"InvoiceRecord(id={self.id!r}, status={self.status!r})"


class InvoiceCorrection(Base):
    """One reviewer edit: kept to measure where extraction goes wrong (Phase 6)."""

    __tablename__ = "invoice_corrections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    invoice_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # "vendor_name", or "items.3.rate" for a line-item cell (0-based item index).
    field: Mapped[str] = mapped_column(String(64), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    old_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
