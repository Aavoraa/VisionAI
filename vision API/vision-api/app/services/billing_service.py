"""Plans, per-request pricing, monthly quotas and the billing overview.

Money is integer micro-USD (1e-6 USD) end to end; it becomes ``Decimal`` only at the API
boundary. Each request's cost is fixed when it is recorded (``UsageLog.cost_micros``), so a
later price change never rewrites past bills. Billing periods are UTC calendar months.
"""

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Job, Plan, UsageLog, User

MICROS_PER_USD = 1_000_000
_SIX_PLACES = Decimal("0.000001")

DEFAULT_PLAN_NAME = "Default"
# Example prices for the bootstrap plan; administrators change them via /v1/admin/plans.
DEFAULT_PLAN_VALUES: dict[str, Any] = {
    "name": DEFAULT_PLAN_NAME,
    "is_default": True,
    "price_input_per_1k_micros": 500,  # $0.0005 per 1K input tokens
    "price_output_per_1k_micros": 1_500,  # $0.0015 per 1K output tokens
    "price_per_image_micros": 2_000,  # $0.002 per image
    "monthly_request_limit": 5_000,
    "monthly_spend_limit_micros": 25 * MICROS_PER_USD,  # $25
}


class QuotaExceeded(Exception):
    """The user's plan allowance for the current month is used up. Message is client-safe."""


def micros_to_usd(micros: int) -> Decimal:
    return (Decimal(micros) / MICROS_PER_USD).quantize(_SIX_PLACES)


def usd_to_micros(amount: Decimal) -> int:
    return int((amount * MICROS_PER_USD).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True, slots=True)
class PlanPricing:
    """Snapshot of a plan's prices, captured when a request starts."""

    plan_id: str
    input_per_1k_micros: int
    output_per_1k_micros: int
    per_image_micros: int

    @classmethod
    def from_plan(cls, plan: Plan) -> "PlanPricing":
        return cls(
            plan_id=plan.id,
            input_per_1k_micros=plan.price_input_per_1k_micros,
            output_per_1k_micros=plan.price_output_per_1k_micros,
            per_image_micros=plan.price_per_image_micros,
        )

    def cost_micros(self, input_tokens: int, output_tokens: int, images: int) -> int:
        token_cost = input_tokens * self.input_per_1k_micros + output_tokens * self.output_per_1k_micros
        return (token_cost + 500) // 1000 + images * self.per_image_micros  # per-1K prices, rounded half-up


@dataclass(frozen=True, slots=True)
class BillingPeriod:
    start: date
    end: date
    resets_at: datetime

    @property
    def since(self) -> datetime:
        return datetime.combine(self.start, time.min, tzinfo=UTC)

    @classmethod
    def containing(cls, moment: datetime) -> "BillingPeriod":
        year, month = moment.year, moment.month
        last_day = calendar.monthrange(year, month)[1]
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        return cls(
            start=date(year, month, 1),
            end=date(year, month, last_day),
            resets_at=datetime(next_year, next_month, 1, tzinfo=UTC),
        )

    def previous(self) -> "BillingPeriod":
        return BillingPeriod.containing(self.since - timedelta(microseconds=1))


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


async def ensure_default_plan(db: AsyncSession) -> Plan:
    """Return the default plan, creating the bootstrap one if no plan is marked default."""
    plan = (await db.execute(select(Plan).where(Plan.is_default.is_(True)))).scalar_one_or_none()
    if plan is not None:
        return plan
    existing = (await db.execute(select(Plan).where(Plan.name == DEFAULT_PLAN_NAME))).scalar_one_or_none()
    if existing is not None:  # a plan called "Default" exists but lost its flag: restore it
        existing.is_default = True
        await db.commit()
        return existing
    plan = Plan(**DEFAULT_PLAN_VALUES)
    db.add(plan)
    try:
        await db.commit()
    except IntegrityError:  # created concurrently by another request
        await db.rollback()
        return (await db.execute(select(Plan).where(Plan.is_default.is_(True)))).scalar_one()
    return plan


async def plan_for_user(db: AsyncSession, user: User) -> Plan:
    if user.plan_id is not None:
        plan = await db.get(Plan, user.plan_id)
        if plan is not None:
            return plan
    return await ensure_default_plan(db)


async def make_default(db: AsyncSession, plan: Plan) -> None:
    """Mark ``plan`` as the only default (unset the previous one first: unique partial index)."""
    await db.execute(update(Plan).where(Plan.id != plan.id, Plan.is_default.is_(True)).values(is_default=False))
    await db.flush()
    plan.is_default = True


# ---------------------------------------------------------------------------
# Usage in a period
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PeriodUsage:
    requests: int
    input_tokens: int
    output_tokens: int
    images: int
    cost_micros: int


async def usage_between(db: AsyncSession, user_id: str, start: datetime, end: datetime | None = None) -> PeriodUsage:
    conditions = [UsageLog.user_id == user_id, UsageLog.created_at >= start]
    if end is not None:
        conditions.append(UsageLog.created_at < end)
    requests, input_tokens, output_tokens, images, cost = (
        await db.execute(
            select(
                func.count(UsageLog.id),
                func.coalesce(func.sum(UsageLog.input_tokens), 0),
                func.coalesce(func.sum(UsageLog.output_tokens), 0),
                func.coalesce(func.sum(UsageLog.image_count), 0),
                func.coalesce(func.sum(UsageLog.cost_micros), 0),
            ).where(*conditions)
        )
    ).one()
    return PeriodUsage(int(requests), int(input_tokens), int(output_tokens), int(images), int(cost))


async def jobs_in_flight(db: AsyncSession, user_id: str) -> int:
    """Accepted async jobs not yet recorded in usage (they will count once they finish)."""
    return int(
        (
            await db.execute(
                select(func.count(Job.id)).where(Job.user_id == user_id, Job.status.in_(("pending", "processing")))
            )
        ).scalar_one()
    )


# ---------------------------------------------------------------------------
# Quota enforcement
# ---------------------------------------------------------------------------


async def check_quota(db: AsyncSession, user: User, now: datetime | None = None) -> Plan:
    """Raise ``QuotaExceeded`` if the user's plan allowance for this month is used up.

    Soft limit: requests already in progress can take usage slightly past it.
    """
    plan = await plan_for_user(db, user)
    if plan.monthly_request_limit is None and plan.monthly_spend_limit_micros is None:
        return plan

    period = BillingPeriod.containing(now or datetime.now(UTC))
    used = await usage_between(db, user.id, period.since)
    resets = period.resets_at.date().isoformat()

    if plan.monthly_request_limit is not None:
        requests = used.requests + await jobs_in_flight(db, user.id)
        if requests >= plan.monthly_request_limit:
            raise QuotaExceeded(
                f"Monthly request quota of {plan.monthly_request_limit} reached; it resets on {resets}"
            )
    if plan.monthly_spend_limit_micros is not None and used.cost_micros >= plan.monthly_spend_limit_micros:
        raise QuotaExceeded(
            f"Monthly spend limit of ${micros_to_usd(plan.monthly_spend_limit_micros).normalize():f} reached; "
            f"it resets on {resets}"
        )
    return plan


# ---------------------------------------------------------------------------
# Billing overview
# ---------------------------------------------------------------------------


def plan_view(plan: Plan) -> dict[str, Any]:
    return {
        "id": plan.id,
        "name": plan.name,
        "is_default": plan.is_default,
        "price_per_1k_input_tokens_usd": micros_to_usd(plan.price_input_per_1k_micros),
        "price_per_1k_output_tokens_usd": micros_to_usd(plan.price_output_per_1k_micros),
        "price_per_image_usd": micros_to_usd(plan.price_per_image_micros),
        "monthly_request_limit": plan.monthly_request_limit,
        "monthly_spend_limit_usd": (
            micros_to_usd(plan.monthly_spend_limit_micros) if plan.monthly_spend_limit_micros is not None else None
        ),
    }


async def billing_overview(db: AsyncSession, user: User, now: datetime | None = None, months: int = 6) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    plan = await plan_for_user(db, user)
    period = BillingPeriod.containing(now)
    used = await usage_between(db, user.id, period.since)

    elapsed = (now - period.since).total_seconds()
    length = (period.resets_at - period.since).total_seconds()
    projected = round(used.cost_micros * length / elapsed) if elapsed > 0 else used.cost_micros

    request_limit = plan.monthly_request_limit
    spend_limit = plan.monthly_spend_limit_micros
    history: list[dict[str, Any]] = []
    cursor = period
    for _ in range(months):
        month_usage = used if cursor is period else await usage_between(db, user.id, cursor.since, cursor.resets_at)
        history.append(
            {
                "month": cursor.start.strftime("%Y-%m"),
                "requests": month_usage.requests,
                "cost_usd": micros_to_usd(month_usage.cost_micros),
            }
        )
        cursor = cursor.previous()
    history.reverse()

    return {
        "plan": plan_view(plan),
        "period": {"start": period.start, "end": period.end, "resets_at": period.resets_at},
        "usage": {
            "requests": used.requests,
            "input_tokens": used.input_tokens,
            "output_tokens": used.output_tokens,
            "images": used.images,
        },
        "cost": {"total_usd": micros_to_usd(used.cost_micros), "projected_usd": micros_to_usd(projected)},
        "quota": {
            "request_limit": request_limit,
            "requests_used": used.requests,
            "requests_remaining": max(0, request_limit - used.requests) if request_limit is not None else None,
            "spend_limit_usd": micros_to_usd(spend_limit) if spend_limit is not None else None,
            "spend_used_usd": micros_to_usd(used.cost_micros),
            "spend_remaining_usd": micros_to_usd(max(0, spend_limit - used.cost_micros)) if spend_limit is not None else None,
        },
        "history": history,
    }
