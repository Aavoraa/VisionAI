"""Administrator endpoints (session auth + admin role): users, keys, platform usage."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import and_, delete, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AuthenticatedSession, require_admin
from app.api.docs import ADMIN_FORBIDDEN, NOT_SIGNED_IN, error_response, invalid_id_response, not_found_response
from app.database.database import get_db
from app.database.models import APIKey, Plan, UsageLog, User, UserSession
from app.schemas.common import UUID_PATTERN
from app.schemas.dashboard import (
    EXAMPLE_ADMIN_USER,
    EXAMPLE_KEY,
    AdminKeyList,
    AdminUserList,
    AdminUserOut,
    AdminUserUpdate,
    ApiKeyOut,
    PlatformUsageReport,
)
from app.schemas.usage import EXAMPLE_PLAN, PlanCreate, PlanList, PlanUpdate, PlanWithUsers
from app.services.billing_service import ensure_default_plan, make_default, plan_view, usd_to_micros
from app.services.usage_service import (
    UsageWindow,
    active_user_count,
    usage_by_model,
    usage_daily,
    usage_top_users,
    usage_totals,
)

router = APIRouter(prefix="/v1/admin", tags=["admin"])

Admin = Annotated[AuthenticatedSession, Depends(require_admin)]
Db = Annotated[AsyncSession, Depends(get_db)]
UserId = Annotated[
    str, Path(pattern=UUID_PATTERN, description="User id (UUID).", examples=[EXAMPLE_ADMIN_USER["id"]])
]
KeyId = Annotated[str, Path(pattern=UUID_PATTERN, description="API key id (UUID).", examples=[EXAMPLE_KEY["id"]])]


def _example(model: type[Any]) -> dict[str, Any]:
    return {"content": {"application/json": {"example": model.model_config["json_schema_extra"]["examples"][0]}}}


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _admin_user_rows(db: AsyncSession, condition: Any, *, limit: int, offset: int) -> list[AdminUserOut]:
    since = datetime.now(UTC) - timedelta(days=30)
    active_keys = (
        select(func.count(APIKey.id))
        .where(and_(APIKey.user_id == User.id, APIKey.is_active.is_(True)))
        .correlate(User)
        .scalar_subquery()
    )
    recent_requests = (
        select(func.count(UsageLog.id))
        .where(and_(UsageLog.user_id == User.id, UsageLog.created_at >= since))
        .correlate(User)
        .scalar_subquery()
    )
    default_plan = await ensure_default_plan(db)
    rows = (
        await db.execute(
            select(User, active_keys, recent_requests, Plan.name)
            .outerjoin(Plan, User.plan_id == Plan.id)
            .where(condition)
            .order_by(User.created_at.desc(), User.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [
        AdminUserOut(
            id=user.id,
            email=user.email,
            role=user.role,  # type: ignore[arg-type]  # constrained by ck_users_role
            is_active=user.is_active,
            has_password=user.password_hash is not None,
            created_at=user.created_at,
            last_login_at=user.last_login_at,
            plan_id=user.plan_id,
            plan_name=plan_name or default_plan.name,
            active_keys=int(keys),
            requests_30d=int(requests),
        )
        for user, keys, requests, plan_name in rows
    ]


async def _get_user(db: AsyncSession, user_id: str) -> User:
    user = await db.get(User, user_id.lower())
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


@router.get(
    "/users",
    response_model=AdminUserList,
    summary="List users",
    description=(
        "All accounts, newest first, with offset pagination and an optional case-insensitive "
        "email search. Each row includes the number of active API keys and requests in the "
        "last 30 days. Administrators only."
    ),
    response_description="A page of users.",
    responses={
        200: _example(AdminUserList),
        401: NOT_SIGNED_IN,
        403: ADMIN_FORBIDDEN,
        422: error_response(
            "A query parameter is out of range.",
            {
                "limit": (
                    "limit out of range",
                    "invalid_request_error",
                    "limit: Input should be less than or equal to 100",
                    "limit",
                )
            },
        ),
    },
)
async def list_users(
    _admin: Admin,
    db: Db,
    search: Annotated[
        str | None, Query(max_length=320, description="Case-insensitive substring of the email.", examples=["ada"])
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Page size, 1-100.")] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000, description="Items to skip.")] = 0,
) -> AdminUserList:
    term = (search or "").strip().lower()
    condition = User.email.like(f"%{_escape_like(term)}%", escape="\\") if term else true()
    total = (await db.execute(select(func.count(User.id)).where(condition))).scalar_one()
    items = await _admin_user_rows(db, condition, limit=limit, offset=offset)
    return AdminUserList(items=items, total=total, limit=limit, offset=offset)


@router.patch(
    "/users/{user_id}",
    response_model=AdminUserOut,
    summary="Activate, deactivate, promote/demote a user or change their plan",
    description=(
        "Changes `is_active`, `role` and/or `plan_id` (`null` = back to the default plan; a new "
        "plan's prices apply to requests from now on). Deactivating signs the user out of every session "
        "immediately and makes all their API keys stop working (keys are kept and resume if the "
        "user is re-activated). Administrators cannot deactivate or demote themselves, so the "
        "platform always keeps at least the acting administrator. Requires `X-CSRF-Token`."
    ),
    response_description="The updated user.",
    responses={
        200: _example(AdminUserOut),
        401: NOT_SIGNED_IN,
        403: ADMIN_FORBIDDEN,
        404: error_response(
            "Unknown user, or the `plan_id` does not exist.",
            {
                "user": ("Unknown user", "not_found_error", "User not found", None),
                "plan": ("Unknown plan", "not_found_error", "Plan not found", None),
            },
        ),
        409: error_response(
            "An administrator tried to lock themselves out.",
            {
                "self": (
                    "Changing your own access",
                    "conflict_error",
                    "You cannot deactivate or demote your own account",
                    None,
                )
            },
        ),
        422: error_response(
            "Invalid id or body.",
            {
                "empty": (
                    "Nothing to change",
                    "invalid_request_error",
                    "Value error, provide is_active, role and/or plan_id",
                    None,
                ),
                "malformed_id": (
                    "Not a UUID",
                    "invalid_request_error",
                    f"user_id: String should match pattern '{UUID_PATTERN}'",
                    "user_id",
                ),
            },
        ),
    },
)
async def update_user(user_id: UserId, payload: AdminUserUpdate, admin: Admin, db: Db) -> AdminUserOut:
    user = await _get_user(db, user_id)
    if user.id == admin.user.id and (payload.is_active is False or payload.role == "user"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="You cannot deactivate or demote your own account"
        )

    if "plan_id" in payload.model_fields_set:
        if payload.plan_id is not None and await db.get(Plan, payload.plan_id.lower()) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
        user.plan_id = payload.plan_id.lower() if payload.plan_id else None
    if payload.role is not None:
        user.role = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active
        if not payload.is_active:
            await db.execute(delete(UserSession).where(UserSession.user_id == user.id))
    await db.commit()
    (row,) = await _admin_user_rows(db, User.id == user.id, limit=1, offset=0)
    return row


@router.get(
    "/users/{user_id}/keys",
    response_model=AdminKeyList,
    summary="List a user's API keys",
    description=(
        "Every API key of one user (active first, newest first), for support and incident "
        "response. Secrets are never available; revoke with `DELETE /v1/admin/keys/{key_id}`. "
        "Administrators only."
    ),
    response_description="The user's keys.",
    responses={
        200: _example(AdminKeyList),
        401: NOT_SIGNED_IN,
        403: ADMIN_FORBIDDEN,
        404: not_found_response("User"),
        422: invalid_id_response("user_id"),
    },
)
async def user_keys(user_id: UserId, _admin: Admin, db: Db) -> AdminKeyList:
    user = await _get_user(db, user_id)
    keys = (
        await db.execute(
            select(APIKey).where(APIKey.user_id == user.id).order_by(APIKey.is_active.desc(), APIKey.created_at.desc())
        )
    ).scalars()
    return AdminKeyList(user_id=user.id, items=[ApiKeyOut.model_validate(key) for key in keys])


@router.delete(
    "/keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke any API key",
    description=(
        "Revokes a key regardless of its owner, e.g. when it has leaked. It stops authenticating "
        "immediately; the record is kept for usage history. Revoking an already revoked key "
        "succeeds. Requires `X-CSRF-Token`. Administrators only."
    ),
    response_description="Key revoked.",
    responses={
        401: NOT_SIGNED_IN,
        403: ADMIN_FORBIDDEN,
        404: not_found_response("API key"),
        422: invalid_id_response("key_id"),
    },
)
async def revoke_any_key(key_id: KeyId, _admin: Admin, db: Db) -> None:
    key = await db.get(APIKey, key_id.lower())
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    if key.is_active:
        key.is_active = False
        await db.commit()


@router.get(
    "/usage",
    response_model=PlatformUsageReport,
    summary="Platform usage",
    description=(
        "Usage across all users over the last `days` days (UTC, including today): totals, a "
        "zero-filled daily series, per-model breakdown, the number of active users and the 10 "
        "busiest users. Administrators only."
    ),
    response_description="Platform usage report.",
    responses={
        200: _example(PlatformUsageReport),
        401: NOT_SIGNED_IN,
        403: ADMIN_FORBIDDEN,
        422: error_response(
            "`days` is out of range.",
            {
                "out_of_range": (
                    "days out of range",
                    "invalid_request_error",
                    "days: Input should be less than or equal to 90",
                    "days",
                )
            },
        ),
    },
)
async def platform_usage(
    _admin: Admin,
    db: Db,
    days: Annotated[int, Query(ge=1, le=90, description="Window length in days, 1-90.")] = 30,
) -> PlatformUsageReport:
    window = UsageWindow.last_days(days)
    return PlatformUsageReport(
        start=window.start,
        end=window.end,
        days=window.days,
        active_users=await active_user_count(db, window),
        totals=await usage_totals(db, window, None),
        daily=await usage_daily(db, window, None),
        by_model=await usage_by_model(db, window, None),
        top_users=await usage_top_users(db, window),
    )


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

PlanId = Annotated[str, Path(pattern=UUID_PATTERN, description="Plan id (UUID).", examples=[EXAMPLE_PLAN["id"]])]

_PLAN_NAME_TAKEN = "A plan with this name already exists"
_PLAN_422 = error_response(
    "A field failed validation; `param` names it.",
    {
        "negative_price": (
            "Negative price",
            "invalid_request_error",
            "price_per_image_usd: Input should be greater than or equal to 0",
            "price_per_image_usd",
        ),
        "too_precise": (
            "More than 6 decimal places",
            "invalid_request_error",
            "price_per_image_usd: Decimal input should have no more than 6 decimal places",
            "price_per_image_usd",
        ),
    },
)


async def _plans_with_users(db: AsyncSession, plan_ids: list[str] | None = None) -> list[PlanWithUsers]:
    default = await ensure_default_plan(db)
    counts = dict(
        (await db.execute(select(User.plan_id, func.count(User.id)).group_by(User.plan_id))).all()
    )
    query = select(Plan).order_by(Plan.is_default.desc(), Plan.name)
    if plan_ids is not None:
        query = query.where(Plan.id.in_(plan_ids))
    plans = (await db.execute(query)).scalars().all()
    return [
        PlanWithUsers(
            **plan_view(plan),
            users=int(counts.get(plan.id, 0)) + (int(counts.get(None, 0)) if plan.id == default.id else 0),
        )
        for plan in plans
    ]


def _apply_prices(plan: Plan, values: dict[str, Any]) -> None:
    mapping = {
        "price_per_1k_input_tokens_usd": "price_input_per_1k_micros",
        "price_per_1k_output_tokens_usd": "price_output_per_1k_micros",
        "price_per_image_usd": "price_per_image_micros",
        "monthly_spend_limit_usd": "monthly_spend_limit_micros",
    }
    for field, value in values.items():
        if field in mapping:
            setattr(plan, mapping[field], usd_to_micros(value) if value is not None else None)
        elif field in ("name", "monthly_request_limit"):
            setattr(plan, field, value)


async def _name_taken(db: AsyncSession, name: str, exclude_id: str | None = None) -> bool:
    query = select(Plan.id).where(func.lower(Plan.name) == name.lower())
    if exclude_id is not None:
        query = query.where(Plan.id != exclude_id)
    return (await db.execute(query)).first() is not None


@router.get(
    "/plans",
    response_model=PlanList,
    summary="List plans",
    description=(
        "All pricing plans with their prices, monthly limits and number of users. The default "
        "plan (listed first) applies to every user without an explicitly assigned plan. It is "
        "created automatically with example prices if none exists. Administrators only."
    ),
    response_description="All plans.",
    responses={200: _example(PlanList), 401: NOT_SIGNED_IN, 403: ADMIN_FORBIDDEN},
)
async def list_plans(_admin: Admin, db: Db) -> PlanList:
    return PlanList(items=await _plans_with_users(db))


@router.post(
    "/plans",
    status_code=status.HTTP_201_CREATED,
    response_model=PlanWithUsers,
    summary="Create a plan",
    description=(
        "Adds a pricing plan. Prices are USD with up to 6 decimal places (send them as strings to "
        "avoid floating-point rounding); limits are per UTC calendar month, `null` = unlimited. "
        "With `is_default=true` it replaces the current default plan. Requires `X-CSRF-Token`."
    ),
    response_description="The new plan.",
    responses={
        201: _example(PlanWithUsers),
        401: NOT_SIGNED_IN,
        403: ADMIN_FORBIDDEN,
        409: error_response(
            "Plan names are unique (case-insensitive).",
            {"name_taken": ("Duplicate name", "conflict_error", _PLAN_NAME_TAKEN, None)},
        ),
        422: _PLAN_422,
    },
)
async def create_plan(payload: PlanCreate, _admin: Admin, db: Db) -> PlanWithUsers:
    if await _name_taken(db, payload.name):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_PLAN_NAME_TAKEN)
    await ensure_default_plan(db)
    plan = Plan(is_default=False)
    _apply_prices(plan, payload.model_dump(exclude={"is_default"}))
    db.add(plan)
    await db.flush()
    if payload.is_default:
        await make_default(db, plan)
    await db.commit()
    (row,) = await _plans_with_users(db, [plan.id])
    return row


@router.patch(
    "/plans/{plan_id}",
    response_model=PlanWithUsers,
    summary="Update a plan",
    description=(
        "Changes only the fields present in the body; send `null` to lift a limit. New prices "
        "apply to requests from now on (past charges keep the price they were recorded at). "
        "`is_default=true` makes this the default plan; the default cannot be unset directly "
        "(promote another plan instead). Requires `X-CSRF-Token`."
    ),
    response_description="The updated plan.",
    responses={
        200: _example(PlanWithUsers),
        401: NOT_SIGNED_IN,
        403: ADMIN_FORBIDDEN,
        404: not_found_response("Plan"),
        409: error_response(
            "Name already used, or an attempt to leave the platform without a default plan.",
            {
                "name_taken": ("Duplicate name", "conflict_error", _PLAN_NAME_TAKEN, None),
                "default_required": (
                    "Unsetting the default",
                    "conflict_error",
                    "The default plan cannot be unset; make another plan the default instead",
                    None,
                ),
            },
        ),
        422: _PLAN_422,
    },
)
async def update_plan(plan_id: PlanId, payload: PlanUpdate, _admin: Admin, db: Db) -> PlanWithUsers:
    plan = await db.get(Plan, plan_id.lower())
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    changes = payload.model_dump(include=payload.model_fields_set)
    if "name" in changes and await _name_taken(db, changes["name"], exclude_id=plan.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_PLAN_NAME_TAKEN)
    if changes.get("is_default") is False and plan.is_default:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The default plan cannot be unset; make another plan the default instead",
        )

    _apply_prices(plan, changes)
    if changes.get("is_default"):
        await make_default(db, plan)
    await db.commit()
    (row,) = await _plans_with_users(db, [plan.id])
    return row
