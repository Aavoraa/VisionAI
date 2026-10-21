from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"status": "ok", "version": "0.1.0"}]})

    status: Literal["ok"] = Field(description="Always `ok` when the process is serving requests.")
    version: str = Field(description="Deployed API version.")


@router.get(
    "",
    response_model=HealthResponse,
    summary="Liveness probe",
    description=(
        "Returns `200` while the process is up and serving requests. Needs no authentication "
        "and touches no dependencies (database, Redis, model), so it is safe for frequent "
        "container and load-balancer health checks."
    ),
    response_description="The service is alive.",
    responses={200: {"content": {"application/json": {"example": {"status": "ok", "version": "0.1.0"}}}}},
)
async def liveness(request: Request) -> HealthResponse:
    return HealthResponse(status="ok", version=request.app.state.settings.app_version)
