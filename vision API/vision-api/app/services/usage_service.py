"""Usage accounting. Recording is best-effort: a failed write is logged, never raised."""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import APIKey, UsageLog, User
from app.services.billing_service import PlanPricing, micros_to_usd

logger = logging.getLogger(__name__)

UsageStatus = Literal["success", "error"]
_MAX_ERROR_MESSAGE = 1000


@dataclass(frozen=True, slots=True)
class UsageContext:
    """Per-request attribution captured at request time (valid after the request ends)."""

    user_id: str
    api_key_id: str
    request_id: str
    model: str
    image_count: int
    # Prices in force when the request started; None = not billed (e.g. tooling, tests).
    pricing: PlanPricing | None = None


async def log_usage(
    db: AsyncSession,
    user_id: str,
    api_key_id: str,
    request_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    image_count: int,
    status: str,
    error_message: str | None,
    processing_time_ms: int,
    cost_micros: int = 0,
) -> bool:
    """Persist one ``UsageLog`` row. Returns ``False`` (after logging) if the write fails."""
    if status not in ("success", "error"):
        raise ValueError(f"invalid usage status {status!r}")
    entry = UsageLog(
        user_id=user_id,
        api_key_id=api_key_id,
        request_id=request_id[:64],
        model=model[:200],
        input_tokens=max(0, input_tokens),
        output_tokens=max(0, output_tokens),
        image_count=max(0, image_count),
        status=status,
        error_message=error_message[:_MAX_ERROR_MESSAGE] if error_message else None,
        processing_time_ms=max(0, processing_time_ms),
        cost_micros=max(0, cost_micros),
    )
    try:
        db.add(entry)
        await db.commit()
        return True
    # Deliberately broad: drivers raise their own exception types (e.g. asyncpg errors on
    # connect), and usage accounting must never turn a served request into a 500.
    except Exception:
        logger.exception("failed to record usage", extra={"usage_request_id": request_id})
        try:
            await db.rollback()
        except Exception:
            logger.exception("rollback after failed usage write also failed")
        return False


# ---------------------------------------------------------------------------
# Aggregation for the dashboard
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UsageWindow:
    start: date
    end: date
    days: int

    @property
    def since(self) -> datetime:
        return datetime.combine(self.start, time.min, tzinfo=UTC)

    @classmethod
    def last_days(cls, days: int, today: date | None = None) -> "UsageWindow":
        end = today or datetime.now(UTC).date()
        return cls(start=end - timedelta(days=days - 1), end=end, days=days)


def _utc_day(db: AsyncSession, column: Any) -> Any:
    """Calendar day (UTC) of a timestamp column, per dialect."""
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        # timezone('UTC', timestamptz) -> UTC wall time, independent of the session TimeZone.
        return func.date(func.timezone("UTC", column))
    return func.date(column)  # SQLite stores naive UTC


def _scope(since: datetime, user_id: str | None) -> list[Any]:
    conditions: list[Any] = [UsageLog.created_at >= since]
    if user_id is not None:
        conditions.append(UsageLog.user_id == user_id)
    return conditions


def _failed() -> Any:
    return func.coalesce(func.sum(case((UsageLog.status == "error", 1), else_=0)), 0)


def _sum(column: Any) -> Any:
    return func.coalesce(func.sum(column), 0)


async def usage_totals(db: AsyncSession, window: UsageWindow, user_id: str | None) -> dict[str, Any]:
    row = (
        await db.execute(
            select(
                func.count(UsageLog.id),
                _failed(),
                _sum(UsageLog.input_tokens),
                _sum(UsageLog.output_tokens),
                _sum(UsageLog.image_count),
                func.avg(UsageLog.processing_time_ms),
                _sum(UsageLog.cost_micros),
            ).where(*_scope(window.since, user_id))
        )
    ).one()
    requests, failed, input_tokens, output_tokens, images, avg_ms, cost = row
    return {
        "requests": int(requests),
        "successful": int(requests) - int(failed),
        "failed": int(failed),
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "images": int(images),
        "avg_processing_ms": round(float(avg_ms), 1) if avg_ms is not None else None,
        "cost_usd": micros_to_usd(int(cost)),
    }


async def usage_daily(db: AsyncSession, window: UsageWindow, user_id: str | None) -> list[dict[str, Any]]:
    day = _utc_day(db, UsageLog.created_at).label("day")
    rows = (
        await db.execute(
            select(
                day,
                func.count(UsageLog.id),
                _failed(),
                _sum(UsageLog.input_tokens),
                _sum(UsageLog.output_tokens),
                _sum(UsageLog.image_count),
                _sum(UsageLog.cost_micros),
            )
            .where(*_scope(window.since, user_id))
            .group_by(day)
        )
    ).all()
    # SQLite yields 'YYYY-MM-DD' strings, PostgreSQL yields date objects.
    by_day = {date.fromisoformat(str(r[0])): r for r in rows}
    series: list[dict[str, Any]] = []
    for offset in range(window.days):
        current = window.start + timedelta(days=offset)
        r = by_day.get(current)
        series.append(
            {
                "date": current,
                "requests": int(r[1]) if r else 0,
                "failed": int(r[2]) if r else 0,
                "input_tokens": int(r[3]) if r else 0,
                "output_tokens": int(r[4]) if r else 0,
                "images": int(r[5]) if r else 0,
                "cost_usd": micros_to_usd(int(r[6]) if r else 0),
            }
        )
    return series


async def usage_by_model(db: AsyncSession, window: UsageWindow, user_id: str | None) -> list[dict[str, Any]]:
    requests = func.count(UsageLog.id).label("requests")
    rows = (
        await db.execute(
            select(UsageLog.model, requests, _sum(UsageLog.input_tokens), _sum(UsageLog.output_tokens))
            .where(*_scope(window.since, user_id))
            .group_by(UsageLog.model)
            .order_by(requests.desc(), UsageLog.model)
        )
    ).all()
    return [
        {"model": model, "requests": int(n), "input_tokens": int(i), "output_tokens": int(o)} for model, n, i, o in rows
    ]


async def usage_by_key(db: AsyncSession, window: UsageWindow, user_id: str) -> list[dict[str, Any]]:
    requests = func.count(UsageLog.id).label("requests")
    rows = (
        await db.execute(
            select(
                APIKey.id,
                APIKey.name,
                APIKey.prefix,
                requests,
                _sum(UsageLog.input_tokens),
                _sum(UsageLog.output_tokens),
            )
            .join(APIKey, UsageLog.api_key_id == APIKey.id)
            .where(*_scope(window.since, user_id))
            .group_by(APIKey.id, APIKey.name, APIKey.prefix)
            .order_by(requests.desc(), APIKey.name)
        )
    ).all()
    return [
        {"api_key_id": key_id, "name": name, "prefix": prefix, "requests": int(n), "input_tokens": int(i), "output_tokens": int(o)}
        for key_id, name, prefix, n, i, o in rows
    ]


async def usage_top_users(db: AsyncSession, window: UsageWindow, limit: int = 10) -> list[dict[str, Any]]:
    requests = func.count(UsageLog.id).label("requests")
    rows = (
        await db.execute(
            select(User.id, User.email, requests, _sum(UsageLog.input_tokens), _sum(UsageLog.output_tokens))
            .join(User, UsageLog.user_id == User.id)
            .where(*_scope(window.since, None))
            .group_by(User.id, User.email)
            .order_by(requests.desc(), User.email)
            .limit(limit)
        )
    ).all()
    return [
        {"user_id": uid, "email": email, "requests": int(n), "input_tokens": int(i), "output_tokens": int(o)}
        for uid, email, n, i, o in rows
    ]


async def active_user_count(db: AsyncSession, window: UsageWindow) -> int:
    return int(
        (
            await db.execute(select(func.count(func.distinct(UsageLog.user_id))).where(*_scope(window.since, None)))
        ).scalar_one()
    )


async def record_usage(
    session_factory: async_sessionmaker[AsyncSession],
    context: UsageContext,
    *,
    status: UsageStatus,
    processing_time_ms: int,
    input_tokens: int = 0,
    output_tokens: int = 0,
    error_message: str | None = None,
) -> bool:
    """Open a dedicated session (safe to use after the request's session is closed) and log usage.

    Only successful requests are charged, at the prices captured in ``context.pricing``.
    """
    cost = 0
    if status == "success" and context.pricing is not None:
        cost = context.pricing.cost_micros(input_tokens, output_tokens, context.image_count)
    try:
        async with session_factory() as session:
            return await log_usage(
                session,
                user_id=context.user_id,
                api_key_id=context.api_key_id,
                request_id=context.request_id,
                model=context.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                image_count=context.image_count,
                status=status,
                error_message=error_message,
                processing_time_ms=processing_time_ms,
                cost_micros=cost,
            )
    except Exception:
        logger.exception("failed to open session for usage logging", extra={"usage_request_id": context.request_id})
        return False
