"""Schemas for the customer dashboard and admin endpoints (Pydantic V2)."""

import datetime as dt
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import UUID_PATTERN, UTCDateTime

# ---------------------------------------------------------------------------
# Examples (shared by schemas and route docs)
# ---------------------------------------------------------------------------

EXAMPLE_KEY = {
    "id": "6c2f0b8e-4a1d-4f5e-9c3b-2a7d8e6f1b0c",
    "name": "production server",
    "prefix": "sk_live_Xy3q",
    "is_active": True,
    "expires_at": None,
    "last_used_at": "2026-09-24T10:12:00Z",
    "created_at": "2026-09-01T08:00:00Z",
}
EXAMPLE_TOTALS = {
    "requests": 1284,
    "successful": 1261,
    "failed": 23,
    "input_tokens": 412_300,
    "output_tokens": 98_410,
    "images": 1502,
    "avg_processing_ms": 842.5,
    "cost_usd": "3.357765",
}
EXAMPLE_DAYS = [
    {
        "date": "2026-09-23",
        "requests": 51,
        "failed": 1,
        "input_tokens": 16_200,
        "output_tokens": 3_900,
        "images": 60,
        "cost_usd": "0.134950",
    },
    {"date": "2026-09-24", "requests": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0, "images": 0, "cost_usd": "0.000000"},
]
EXAMPLE_BY_MODEL = [{"model": "mock-vision-1", "requests": 1284, "input_tokens": 412_300, "output_tokens": 98_410}]


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, json_schema_extra={"examples": [EXAMPLE_KEY]})

    id: str = Field(description="Key id (UUID). Not secret.")
    name: str = Field(description="Label chosen by the owner.")
    prefix: str = Field(description="First characters of the key, to recognise it. The full key is never shown again.")
    is_active: bool = Field(description="`false` once revoked. Revoked keys are kept for usage history.")
    expires_at: UTCDateTime | None = Field(default=None, description="Expiry time, or `null` for never.")
    last_used_at: UTCDateTime | None = Field(
        default=None, description="Last successful authentication (approximate, refreshed every few minutes)."
    )
    created_at: UTCDateTime = Field(description="Creation time.")


class ApiKeyList(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"items": [EXAMPLE_KEY]}]})

    items: list[ApiKeyOut] = Field(description="Keys, newest first; active keys before revoked ones.")


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={"examples": [{"name": "production server", "expires_in_days": 90}]},
    )

    name: str = Field(min_length=1, max_length=100, description="Label to tell keys apart, 1-100 characters.")
    expires_in_days: int | None = Field(
        default=None, ge=1, le=3650, description="Expire the key after this many days; omit for a key that never expires."
    )


class ApiKeyCreated(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"key": EXAMPLE_KEY, "secret": "sk_live_EXAMPLE_not_a_real_key_000001"}]
        }
    )

    key: ApiKeyOut
    secret: str = Field(description="The full API key. Shown **only in this response**; store it securely now.")


class ApiKeyUpdate(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, json_schema_extra={"examples": [{"name": "staging server"}]}
    )

    name: str = Field(min_length=1, max_length=100, description="New label, 1-100 characters.")


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


class UsageTotals(BaseModel):
    requests: int = Field(description="Requests that reached processing (authenticated and within rate limits).")
    successful: int = Field(description="Requests that returned a result.")
    failed: int = Field(description="Requests that failed (invalid image, model error, ...).")
    input_tokens: int = Field(description="Prompt and image tokens consumed.")
    output_tokens: int = Field(description="Tokens generated.")
    images: int = Field(description="Images submitted.")
    avg_processing_ms: float | None = Field(description="Mean processing time; `null` when there were no requests.")
    cost_usd: Decimal = Field(description="Charges for the window (failed requests are free), priced when each request ran.")


class UsageDay(BaseModel):
    date: dt.date = Field(description="UTC calendar day. Days without traffic are included with zeros.")
    requests: int
    failed: int
    input_tokens: int
    output_tokens: int
    images: int
    cost_usd: Decimal


class ModelUsage(BaseModel):
    model: str = Field(description="Model name as recorded at request time.")
    requests: int
    input_tokens: int
    output_tokens: int


class KeyUsage(BaseModel):
    api_key_id: str
    name: str = Field(description="Current key label.")
    prefix: str
    requests: int
    input_tokens: int
    output_tokens: int


class UsageReport(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "start": "2026-08-26",
                    "end": "2026-09-24",
                    "days": 30,
                    "totals": EXAMPLE_TOTALS,
                    "daily": EXAMPLE_DAYS,
                    "by_model": EXAMPLE_BY_MODEL,
                    "by_key": [
                        {
                            "api_key_id": EXAMPLE_KEY["id"],
                            "name": EXAMPLE_KEY["name"],
                            "prefix": EXAMPLE_KEY["prefix"],
                            "requests": 1284,
                            "input_tokens": 412_300,
                            "output_tokens": 98_410,
                        }
                    ],
                }
            ]
        }
    )

    start: dt.date = Field(description="First day of the window (UTC, inclusive).")
    end: dt.date = Field(description="Last day of the window: today, UTC (inclusive).")
    days: int = Field(description="Window length in days.")
    totals: UsageTotals
    daily: list[UsageDay] = Field(description="One entry per day, oldest first.")
    by_model: list[ModelUsage] = Field(description="Per model, busiest first.")
    by_key: list[KeyUsage] = Field(description="Per API key, busiest first.")


class UserUsage(BaseModel):
    user_id: str
    email: str
    requests: int
    input_tokens: int
    output_tokens: int


class PlatformUsageReport(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "start": "2026-08-26",
                    "end": "2026-09-24",
                    "days": 30,
                    "active_users": 17,
                    "totals": EXAMPLE_TOTALS,
                    "daily": EXAMPLE_DAYS,
                    "by_model": EXAMPLE_BY_MODEL,
                    "top_users": [
                        {
                            "user_id": "5f1d2c3b-4a59-4e6f-8a7b-9c0d1e2f3a4b",
                            "email": "ada@example.com",
                            "requests": 604,
                            "input_tokens": 190_100,
                            "output_tokens": 45_020,
                        }
                    ],
                }
            ]
        }
    )

    start: dt.date
    end: dt.date
    days: int
    active_users: int = Field(description="Users with at least one request in the window.")
    totals: UsageTotals
    daily: list[UsageDay]
    by_model: list[ModelUsage]
    top_users: list[UserUsage] = Field(description="Up to 10 busiest users.")


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


class JobSummary(BaseModel):
    job_id: str
    status: Literal["pending", "processing", "completed", "failed"]
    error: str | None = Field(default=None, description="Client-safe failure reason when `failed`.")
    created_at: UTCDateTime
    updated_at: UTCDateTime


class JobList(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "items": [
                        {
                            "job_id": "8d0c6f4e-2b1a-4c3d-9e8f-7a6b5c4d3e2f",
                            "status": "completed",
                            "error": None,
                            "created_at": "2026-09-24T10:00:00Z",
                            "updated_at": "2026-09-24T10:00:02Z",
                        }
                    ],
                    "total": 1,
                    "limit": 20,
                    "offset": 0,
                }
            ]
        }
    )

    items: list[JobSummary] = Field(description="Newest first. Fetch results with `GET /v1/jobs/{job_id}`.")
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Admin: users
# ---------------------------------------------------------------------------

EXAMPLE_ADMIN_USER = {
    "id": "5f1d2c3b-4a59-4e6f-8a7b-9c0d1e2f3a4b",
    "email": "ada@example.com",
    "role": "user",
    "is_active": True,
    "has_password": True,
    "created_at": "2026-09-01T08:00:00Z",
    "last_login_at": "2026-09-24T09:30:00Z",
    "plan_id": None,
    "plan_name": "Default",
    "active_keys": 2,
    "requests_30d": 604,
}


class AdminUserOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [EXAMPLE_ADMIN_USER]})

    id: str
    email: str
    role: Literal["user", "admin"]
    is_active: bool
    has_password: bool = Field(description="`false` for users created only to own API keys (cannot sign in).")
    created_at: UTCDateTime
    last_login_at: UTCDateTime | None = None
    plan_id: str | None = Field(default=None, description="Explicitly assigned plan, or `null` for the default plan.")
    plan_name: str = Field(description="Name of the plan in effect.")
    active_keys: int = Field(description="Non-revoked API keys.")
    requests_30d: int = Field(description="Requests in the last 30 days.")


class AdminUserList(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"items": [EXAMPLE_ADMIN_USER], "total": 1, "limit": 50, "offset": 0}]}
    )

    items: list[AdminUserOut] = Field(description="Newest accounts first.")
    total: int = Field(description="Total matching users (for pagination).")
    limit: int
    offset: int


class AdminUserUpdate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"is_active": False}, {"role": "admin"}, {"plan_id": None}]},
    )

    is_active: bool | None = Field(
        default=None, description="`false` signs the user out everywhere and disables their API keys."
    )
    role: Literal["user", "admin"] | None = Field(default=None, description="Grant or remove administrator rights.")
    plan_id: str | None = Field(
        default=None,
        pattern=UUID_PATTERN,
        description="Assign a plan by id; send `null` to put the user back on the default plan.",
    )

    @model_validator(mode="after")
    def _at_least_one(self) -> "AdminUserUpdate":
        if self.is_active is None and self.role is None and "plan_id" not in self.model_fields_set:
            raise ValueError("provide is_active, role and/or plan_id")
        return self


class AdminKeyList(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"user_id": EXAMPLE_ADMIN_USER["id"], "items": [EXAMPLE_KEY]}]})

    user_id: str
    items: list[ApiKeyOut]



# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class ModelTask(BaseModel):
    task: str = Field(description="Value to send as `prompt`, e.g. `<OCR>`.")
    description: str = Field(description="What the task returns.")


class ModelInfo(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "PaddleOCR-VL-1.6-GGUF",
                    "invoice_extraction": True,
                    "tasks": [
                        {"task": "OCR:", "description": "Read all text (document order, tables as text)"},
                        {"task": "Table Recognition:", "description": "Read the main table (structured)"},
                    ],
                }
            ]
        }
    )

    name: str = Field(description="Model serving `/v1/vision` (also accepted as its `model` field).")
    invoice_extraction: bool = Field(
        default=False, description="Whether `POST /v1/invoices/extract` works with this model (PaddleOCR-VL)."
    )
    tasks: list[ModelTask] | None = Field(
        description=(
            "`null` for chat models, whose `prompt` is a free-form question. Otherwise the model only "
            "accepts one of these tasks as `prompt` (e.g. Florence-2)."
        )
    )
