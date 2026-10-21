"""GET /v1/jobs/{job_id}: poll the status of an async vision request."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_api_key
from app.api.docs import EXAMPLE_JOB_ID, FORBIDDEN, UNAUTHORIZED, error_response
from app.database.database import get_db
from app.database.models import APIKey, Job, User
from app.schemas.common import UUID_PATTERN
from app.schemas.vision import JobResponse, VisionResponse

router = APIRouter(prefix="/v1", tags=["jobs"])


_DESCRIPTION = """
Poll an async job created by `POST /v1/vision` with `mode=async`.

Status lifecycle: `pending` -> `processing` -> `completed` | `failed`.
- `completed`: `result` holds the same payload as a synchronous response.
- `failed`: `error` holds a client-safe reason; `result` is `null`.

Jobs are private to the user who created them. Anyone else gets `404`, exactly as for a
job that does not exist, so job ids cannot be probed. Polling is not rate limited; an
interval of 1-2 seconds is reasonable.
"""

_COMPLETED_RESULT = VisionResponse.model_config["json_schema_extra"]["examples"][0]  # type: ignore[index]

_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "Current job state.",
        "content": {
            "application/json": {
                "examples": {
                    "pending": {
                        "summary": "Queued",
                        "value": {"job_id": EXAMPLE_JOB_ID, "status": "pending", "result": None, "error": None},
                    },
                    "processing": {
                        "summary": "Running",
                        "value": {"job_id": EXAMPLE_JOB_ID, "status": "processing", "result": None, "error": None},
                    },
                    "completed": {
                        "summary": "Finished with a result",
                        "value": {
                            "job_id": EXAMPLE_JOB_ID,
                            "status": "completed",
                            "result": _COMPLETED_RESULT,
                            "error": None,
                        },
                    },
                    "failed": {
                        "summary": "Failed",
                        "value": {
                            "job_id": EXAMPLE_JOB_ID,
                            "status": "failed",
                            "result": None,
                            "error": "Model inference failed",
                        },
                    },
                }
            }
        },
    },
    401: UNAUTHORIZED,
    403: FORBIDDEN,
    404: error_response(
        "No job with this id belongs to the caller.",
        {"not_found": ("Unknown job, or another user's job", "not_found_error", "Job not found", None)},
    ),
    422: error_response(
        "`job_id` is not a UUID.",
        {
            "malformed_id": (
                "Not a UUID",
                "invalid_request_error",
                f"job_id: String should match pattern '{UUID_PATTERN}'",
                "job_id",
            )
        },
    ),
}


def job_to_response(job: Job) -> JobResponse:
    return JobResponse(
        job_id=job.id,
        status=job.status,  # type: ignore[arg-type]  # constrained by ck_jobs_status
        result=VisionResponse.model_validate(job.result_data) if job.result_data is not None else None,
        error=job.error_message,
    )


@router.get(
    "/jobs/{job_id}",
    response_model=JobResponse,
    summary="Get the status and result of an async job",
    description=_DESCRIPTION,
    response_description="Current job state.",
    responses=_RESPONSES,
)
async def get_job(
    job_id: Annotated[
        str,
        Path(
            pattern=UUID_PATTERN,
            description="The `job_id` returned by `POST /v1/vision` with `mode=async` (a UUID).",
            examples=[EXAMPLE_JOB_ID],
        ),
    ],
    auth: Annotated[tuple[APIKey, User], Depends(get_current_api_key)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobResponse:
    _, user = auth
    # Ownership is part of the query: another user's job is indistinguishable from a missing
    # one (404, not 403), so job IDs cannot be probed for existence.
    job = (
        await db.execute(select(Job).where(Job.id == job_id.lower(), Job.user_id == user.id))
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job_to_response(job)
