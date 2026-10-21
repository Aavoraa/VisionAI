"""Plans and billing schemas (Pydantic V2). Money is exact: ``Decimal`` USD, sent as strings."""

import datetime as dt
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import UTCDateTime

# Up to 6 decimal places (micro-USD precision), 0-1,000,000 USD.
Usd = Annotated[Decimal, Field(ge=0, le=1_000_000, decimal_places=6)]

EXAMPLE_PLAN = {
    "id": "0e8f5b1c-7d2a-4c9e-b3f6-1a2b3c4d5e6f",
    "name": "Default",
    "is_default": True,
    "price_per_1k_input_tokens_usd": "0.000500",
    "price_per_1k_output_tokens_usd": "0.001500",
    "price_per_image_usd": "0.002000",
    "monthly_request_limit": 5000,
    "monthly_spend_limit_usd": "25.000000",
}


class PlanOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [EXAMPLE_PLAN]})

    id: str
    name: str
    is_default: bool = Field(description="Applies to every user without an explicitly assigned plan.")
    price_per_1k_input_tokens_usd: Decimal = Field(description="USD per 1,000 prompt and image tokens.")
    price_per_1k_output_tokens_usd: Decimal = Field(description="USD per 1,000 generated tokens.")
    price_per_image_usd: Decimal = Field(description="USD per submitted image.")
    monthly_request_limit: int | None = Field(description="Requests per calendar month (UTC); `null` = unlimited.")
    monthly_spend_limit_usd: Decimal | None = Field(description="Spend cap per calendar month; `null` = unlimited.")


class PlanWithUsers(PlanOut):
    model_config = ConfigDict(json_schema_extra={"examples": [{**EXAMPLE_PLAN, "users": 42}]})

    users: int = Field(description="Users on this plan (for the default plan: including users with no explicit plan).")


class PlanList(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"items": [{**EXAMPLE_PLAN, "users": 42}]}]})

    items: list[PlanWithUsers] = Field(description="Default plan first, then by name.")


class PlanCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {
                    "name": "Pro",
                    "price_per_1k_input_tokens_usd": "0.0004",
                    "price_per_1k_output_tokens_usd": "0.0012",
                    "price_per_image_usd": "0.0015",
                    "monthly_request_limit": 100000,
                    "monthly_spend_limit_usd": None,
                }
            ]
        },
    )

    name: str = Field(min_length=1, max_length=50, description="Unique plan name.")
    price_per_1k_input_tokens_usd: Usd
    price_per_1k_output_tokens_usd: Usd
    price_per_image_usd: Usd
    monthly_request_limit: int | None = Field(default=None, ge=0, le=1_000_000_000, description="`null` = unlimited.")
    monthly_spend_limit_usd: Usd | None = Field(default=None, description="`null` = unlimited.")
    is_default: bool = Field(default=False, description="Make this the default plan (replaces the current default).")


class PlanUpdate(BaseModel):
    """Partial update: only fields present in the request body change (send `null` to lift a limit)."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={"examples": [{"price_per_image_usd": "0.0025"}, {"monthly_request_limit": None}]},
    )

    name: str | None = Field(default=None, min_length=1, max_length=50)
    price_per_1k_input_tokens_usd: Usd | None = None
    price_per_1k_output_tokens_usd: Usd | None = None
    price_per_image_usd: Usd | None = None
    monthly_request_limit: int | None = Field(default=None, ge=0, le=1_000_000_000)
    monthly_spend_limit_usd: Usd | None = None
    is_default: bool | None = Field(
        default=None, description="`true` makes this the default plan. To change the default, promote another plan."
    )

    @model_validator(mode="after")
    def _check(self) -> "PlanUpdate":
        if not self.model_fields_set:
            raise ValueError("provide at least one field to change")
        for required in ("name", "price_per_1k_input_tokens_usd", "price_per_1k_output_tokens_usd", "price_per_image_usd"):
            if required in self.model_fields_set and getattr(self, required) is None:
                raise ValueError(f"{required} cannot be null")
        return self


# ---------------------------------------------------------------------------
# Billing overview
# ---------------------------------------------------------------------------


class BillingPeriodOut(BaseModel):
    start: dt.date = Field(description="First day of the current billing month (UTC).")
    end: dt.date = Field(description="Last day of the current billing month (UTC).")
    resets_at: UTCDateTime = Field(description="When quotas reset (start of next month, UTC).")


class BillingUsage(BaseModel):
    requests: int = Field(description="Requests this month (successful and failed).")
    input_tokens: int
    output_tokens: int
    images: int


class BillingCost(BaseModel):
    total_usd: Decimal = Field(description="Charges so far this month. Failed requests are not charged.")
    projected_usd: Decimal = Field(description="Month-end estimate at the current daily rate.")


class QuotaStatus(BaseModel):
    request_limit: int | None = Field(description="`null` = unlimited.")
    requests_used: int
    requests_remaining: int | None
    spend_limit_usd: Decimal | None = Field(description="`null` = unlimited.")
    spend_used_usd: Decimal
    spend_remaining_usd: Decimal | None


class MonthCost(BaseModel):
    month: str = Field(description="`YYYY-MM` (UTC).", pattern=r"^\d{4}-\d{2}$")
    requests: int
    cost_usd: Decimal


class BillingOverview(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "plan": EXAMPLE_PLAN,
                    "period": {"start": "2026-09-01", "end": "2026-09-30", "resets_at": "2026-10-01T00:00:00Z"},
                    "usage": {"requests": 1284, "input_tokens": 412300, "output_tokens": 98410, "images": 1502},
                    "cost": {"total_usd": "3.357765", "projected_usd": "4.197206"},
                    "quota": {
                        "request_limit": 5000,
                        "requests_used": 1284,
                        "requests_remaining": 3716,
                        "spend_limit_usd": "25.000000",
                        "spend_used_usd": "3.357765",
                        "spend_remaining_usd": "21.642235",
                    },
                    "history": [
                        {"month": "2026-08", "requests": 3120, "cost_usd": "8.101200"},
                        {"month": "2026-09", "requests": 1284, "cost_usd": "3.357765"},
                    ],
                }
            ]
        }
    )

    plan: PlanOut
    period: BillingPeriodOut
    usage: BillingUsage
    cost: BillingCost
    quota: QuotaStatus
    history: list[MonthCost] = Field(description="Last 6 months including the current one, oldest first.")

