"""Customer dashboard (session auth): own API keys, own usage, own async jobs."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AuthenticatedSession, get_current_session, get_vision_service
from app.api.docs import NOT_SIGNED_IN, SESSION_FORBIDDEN, error_response, invalid_id_response, not_found_response
from app.core.security import api_key_display_prefix, generate_api_key
from app.database.database import get_db
from app.database.models import APIKey, Job
from app.schemas.common import UUID_PATTERN
from app.schemas.dashboard import (
    EXAMPLE_KEY,
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyList,
    ApiKeyOut,
    ApiKeyUpdate,
    JobList,
    JobSummary,
    ModelInfo,
    ModelTask,
    UsageReport,
)
from app.schemas.usage import BillingOverview
from app.services.billing_service import billing_overview
from app.services.usage_service import UsageWindow, usage_by_key, usage_by_model, usage_daily, usage_totals
from app.services.invoice_extraction import OCR_TASK, TABLE_TASK
from app.services.vision_service import VisionService

router = APIRouter(prefix="/v1/dashboard", tags=["dashboard"])

Session = Annotated[AuthenticatedSession, Depends(get_current_session)]
Db = Annotated[AsyncSession, Depends(get_db)]
KeyId = Annotated[
    str, Path(pattern=UUID_PATTERN, description="API key id (from the key list).", examples=[EXAMPLE_KEY["id"]])
]


def _example(model: type[Any]) -> dict[str, Any]:
    return {"content": {"application/json": {"example": model.model_config["json_schema_extra"]["examples"][0]}}}


def _range_error(param: str, message: str) -> dict[str, Any]:
    return error_response(
        f"`{param}` is out of range.", {"out_of_range": (f"{param} out of range", "invalid_request_error", message, param)}
    )


async def _own_key(db: AsyncSession, user_id: str, key_id: str) -> APIKey:
    # Ownership is part of the query: another user's key is indistinguishable from a missing one.
    key = (
        await db.execute(select(APIKey).where(APIKey.id == key_id.lower(), APIKey.user_id == user_id))
    ).scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    return key


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


@router.get(
    "/keys",
    response_model=ApiKeyList,
    summary="List my API keys",
    description=(
        "All API keys of the signed-in user: active keys first, then revoked ones, newest first. "
        "Only a short prefix of each key is ever shown again after creation."
    ),
    response_description="The user's keys.",
    responses={200: _example(ApiKeyList), 401: NOT_SIGNED_IN, 403: SESSION_FORBIDDEN},
)
async def list_keys(auth: Session, db: Db) -> ApiKeyList:
    keys = (
        await db.execute(
            select(APIKey)
            .where(APIKey.user_id == auth.user.id)
            .order_by(APIKey.is_active.desc(), APIKey.created_at.desc())
        )
    ).scalars()
    return ApiKeyList(items=[ApiKeyOut.model_validate(key) for key in keys])


@router.post(
    "/keys",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiKeyCreated,
    summary="Create an API key",
    description=(
        "Issues a new `sk_live_` key for the signed-in user. The full key (`secret`) is returned "
        "**only in this response**: only its SHA-256 hash is stored, so it cannot be shown again. "
        "The number of active keys per user is capped (`VISION_MAX_API_KEYS_PER_USER`, default 25). "
        "Requires `X-CSRF-Token`."
    ),
    response_description="Key created. Copy `secret` now.",
    responses={
        201: _example(ApiKeyCreated),
        401: NOT_SIGNED_IN,
        403: SESSION_FORBIDDEN,
        409: error_response(
            "Too many active keys; revoke one first.",
            {"limit": ("Key limit reached", "conflict_error", "Active API key limit (25) reached; revoke a key first", None)},
        ),
        422: error_response(
            "A field failed validation; `param` names it.",
            {
                "missing_name": ("Name missing", "invalid_request_error", "name: Field required", "name"),
                "bad_expiry": (
                    "Expiry out of range",
                    "invalid_request_error",
                    "expires_in_days: Input should be greater than or equal to 1",
                    "expires_in_days",
                ),
            },
        ),
    },
)
async def create_key(payload: ApiKeyCreate, request: Request, auth: Session, db: Db) -> ApiKeyCreated:
    limit = request.app.state.settings.max_api_keys_per_user
    active = (
        await db.execute(
            select(func.count(APIKey.id)).where(APIKey.user_id == auth.user.id, APIKey.is_active.is_(True))
        )
    ).scalar_one()
    if active >= limit:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Active API key limit ({limit}) reached; revoke a key first",
        )

    raw_key, key_hash = generate_api_key()
    key = APIKey(
        user_id=auth.user.id,
        name=payload.name,
        key_hash=key_hash,
        prefix=api_key_display_prefix(raw_key),
        expires_at=datetime.now(UTC) + timedelta(days=payload.expires_in_days) if payload.expires_in_days else None,
    )
    db.add(key)
    await db.commit()
    return ApiKeyCreated(key=ApiKeyOut.model_validate(key), secret=raw_key)


@router.patch(
    "/keys/{key_id}",
    response_model=ApiKeyOut,
    summary="Rename an API key",
    description=(
        "Changes the label of one of the signed-in user's keys (active or revoked). The key "
        "itself does not change. Requires `X-CSRF-Token`."
    ),
    response_description="The updated key.",
    responses={
        200: _example(ApiKeyOut),
        401: NOT_SIGNED_IN,
        403: SESSION_FORBIDDEN,
        404: not_found_response("API key"),
        422: invalid_id_response("key_id"),
    },
)
async def rename_key(key_id: KeyId, payload: ApiKeyUpdate, auth: Session, db: Db) -> ApiKeyOut:
    key = await _own_key(db, auth.user.id, key_id)
    key.name = payload.name
    await db.commit()
    return ApiKeyOut.model_validate(key)


@router.delete(
    "/keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key",
    description=(
        "Revokes one of the signed-in user's keys: it stops authenticating immediately. The key "
        "record is kept (as `is_active=false`) so its usage history stays attributable. Revoking "
        "an already revoked key succeeds. Requires `X-CSRF-Token`."
    ),
    response_description="Key revoked.",
    responses={
        401: NOT_SIGNED_IN,
        403: SESSION_FORBIDDEN,
        404: not_found_response("API key"),
        422: invalid_id_response("key_id"),
    },
)
async def revoke_key(key_id: KeyId, auth: Session, db: Db) -> None:
    key = await _own_key(db, auth.user.id, key_id)
    if key.is_active:
        key.is_active = False
        await db.commit()


# ---------------------------------------------------------------------------
# Usage and jobs
# ---------------------------------------------------------------------------


@router.get(
    "/usage",
    response_model=UsageReport,
    summary="My usage",
    description=(
        "Usage of the signed-in user over the last `days` days (UTC, including today): totals, "
        "a daily series with every day present (zeros for quiet days, ready to chart), and "
        "breakdowns by model and by API key. Counts every request that reached processing, "
        "successful or not; rate-limited and unauthenticated requests are not counted."
    ),
    response_description="Usage report.",
    responses={
        200: _example(UsageReport),
        401: NOT_SIGNED_IN,
        403: SESSION_FORBIDDEN,
        422: _range_error("days", "days: Input should be less than or equal to 90"),
    },
)
async def my_usage(
    auth: Session,
    db: Db,
    days: Annotated[int, Query(ge=1, le=90, description="Window length in days, 1-90.")] = 30,
) -> UsageReport:
    window = UsageWindow.last_days(days)
    user_id = auth.user.id
    return UsageReport(
        start=window.start,
        end=window.end,
        days=window.days,
        totals=await usage_totals(db, window, user_id),
        daily=await usage_daily(db, window, user_id),
        by_model=await usage_by_model(db, window, user_id),
        by_key=await usage_by_key(db, window, user_id),
    )


@router.get(
    "/billing",
    response_model=BillingOverview,
    summary="My plan, charges and quota",
    description=(
        "Billing overview for the current calendar month (UTC): the plan in effect with its "
        "prices and limits, usage and charges so far, a month-end projection, remaining quota, "
        "and the last 6 months of charges. Each request is priced when it runs, so plan changes "
        "never rewrite past charges; failed requests are free. Amounts are USD decimal strings."
    ),
    response_description="Billing overview.",
    responses={200: _example(BillingOverview), 401: NOT_SIGNED_IN, 403: SESSION_FORBIDDEN},
)
async def my_billing(auth: Session, db: Db) -> BillingOverview:
    return BillingOverview.model_validate(await billing_overview(db, auth.user))


@router.get(
    "/jobs",
    response_model=JobList,
    summary="My async jobs",
    description=(
        "Recent async jobs (`mode=async`) of the signed-in user, newest first, with offset "
        "pagination. Results are not included; fetch one with `GET /v1/jobs/{job_id}` using an API key."
    ),
    response_description="A page of jobs.",
    responses={
        200: _example(JobList),
        401: NOT_SIGNED_IN,
        403: SESSION_FORBIDDEN,
        422: _range_error("limit", "limit: Input should be less than or equal to 100"),
    },
)
async def my_jobs(
    auth: Session,
    db: Db,
    limit: Annotated[int, Query(ge=1, le=100, description="Page size, 1-100.")] = 20,
    offset: Annotated[int, Query(ge=0, le=100_000, description="Items to skip.")] = 0,
) -> JobList:
    owner = Job.user_id == auth.user.id
    total = (await db.execute(select(func.count(Job.id)).where(owner))).scalar_one()
    jobs = (
        await db.execute(select(Job).where(owner).order_by(Job.created_at.desc(), Job.id).limit(limit).offset(offset))
    ).scalars()
    items = [
        JobSummary(
            job_id=job.id,
            status=job.status,  # type: ignore[arg-type]  # constrained by ck_jobs_status
            error=job.error_message,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )
        for job in jobs
    ]
    return JobList(items=items, total=total, limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@router.get(
    "/model",
    response_model=ModelInfo,
    summary="Describe the serving model",
    description=(
        "Which model answers `/v1/vision`, and whether it takes free-form questions or a fixed set "
        "of tasks (Florence-2). The Playground uses this to show a question box or a task picker."
    ),
    response_description="The serving model.",
    responses={200: _example(ModelInfo), 401: NOT_SIGNED_IN, 403: SESSION_FORBIDDEN},
)
async def serving_model(auth: Session, service: Annotated[VisionService, Depends(get_vision_service)]) -> ModelInfo:
    model = service.model
    tasks = None if model.tasks is None else [ModelTask(task=t, description=d) for t, d in model.tasks.items()]
    extraction = bool(model.tasks) and OCR_TASK in model.tasks and TABLE_TASK in model.tasks  # type: ignore[operator]
    return ModelInfo(name=model.name, tasks=tasks, invoice_extraction=extraction)
