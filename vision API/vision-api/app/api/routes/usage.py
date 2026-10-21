"""GET /v1/usage: plan, charges and remaining quota for API clients (API-key auth)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_api_key
from app.api.docs import FORBIDDEN, UNAUTHORIZED
from app.database.database import get_db
from app.database.models import APIKey, User
from app.schemas.usage import BillingOverview
from app.services.billing_service import billing_overview

router = APIRouter(prefix="/v1", tags=["usage"])

_EXAMPLE = BillingOverview.model_config["json_schema_extra"]["examples"][0]  # type: ignore[index]


@router.get(
    "/usage",
    response_model=BillingOverview,
    summary="Usage, charges and remaining quota",
    description=(
        "The same billing overview the dashboard shows, for the owner of the calling API key: "
        "plan, this month's usage and charges, a month-end projection, remaining quota and the "
        "last 6 months. Useful for clients that want to stop before hitting `402` quota errors. "
        "Not rate limited and not billed."
    ),
    response_description="Billing overview for the key's owner.",
    responses={
        200: {"content": {"application/json": {"example": _EXAMPLE}}},
        401: UNAUTHORIZED,
        403: FORBIDDEN,
    },
)
async def get_usage(
    auth: Annotated[tuple[APIKey, User], Depends(get_current_api_key)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BillingOverview:
    _, user = auth
    return BillingOverview.model_validate(await billing_overview(db, user))
