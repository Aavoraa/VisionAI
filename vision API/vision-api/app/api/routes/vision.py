"""POST /v1/vision: accepts application/json (image_url) or multipart/form-data (file uploads).

A single endpoint serves both content types, so the body is parsed manually after
authentication. The raw body is read with a hard byte cap *before* parsing, so neither a
missing/false Content-Length nor chunked transfer encoding can bypass the size limit.
"""

import json
import logging
import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import UploadFile
from starlette.types import Message

from app.api.dependencies import enforce_quota, get_current_api_key, get_vision_service
from app.api.docs import (
    EXAMPLE_JOB_ID,
    FORBIDDEN,
    RATE_LIMIT_HEADERS,
    REQUEST_ID_HEADER,
    UNAUTHORIZED,
    error_response,
)
from app.core.config import Settings
from app.core.logging import request_id_ctx
from app.core.rate_limit import get_rate_limiter
from app.database.database import get_db, get_session_factory
from app.database.models import APIKey, Job, User
from app.models.base import ModelError, ModelTimeoutError
from app.schemas.vision import JobResponse, VisionRequest, VisionResponse
from app.services.billing_service import PlanPricing
from app.services.usage_service import UsageContext, record_usage
from app.services.vision_service import VisionService, public_error_message

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["vision"])

UPLOAD_FIELD = "images"
_JSON_BODY_LIMIT = 256 * 1024
_MULTIPART_OVERHEAD = 1024 * 1024

# ---------------------------------------------------------------------------
# OpenAPI documentation
# ---------------------------------------------------------------------------

_DESCRIPTION = """
Analyse one or more images with a vision-language model.

**Authentication**: `Authorization: Bearer sk_live_...` (use **Authorize** above).
**Rate limit**: per user; see the `X-RateLimit-*` response headers. A `429` includes `Retry-After`.

### Two ways to send images

**1. `multipart/form-data`** to upload files. Repeat the `images` field for several files,
optionally together with an `image_url`:

```bash
curl http://localhost:8000/v1/vision \\
  -H "Authorization: Bearer sk_live_..." \\
  -F prompt="Describe this image" -F images=@photo.jpg
```

**2. `application/json`** to analyse an image by URL (fetched server-side):

```bash
curl http://localhost:8000/v1/vision \\
  -H "Authorization: Bearer sk_live_..." -H "Content-Type: application/json" \\
  -d '{"prompt": "Describe this image", "image_url": "https://httpbin.org/image/png"}'
```

### Limits
- 1 to 4 images per request (uploads + URL); JPEG, PNG, WebP or GIF (first frame only).
- Each image at most 10 MB and 40 megapixels. Type is detected from content, never from the file name.
- Images are stripped of all metadata (EXIF/GPS) and downscaled before inference.

### Sync vs async
- `mode=sync` (default): **200** with the result.
- `mode=async`: **202** with a `job_id` (and a `Location` header); poll `GET /v1/jobs/{job_id}`.
  Requests that are invalid up front (no image, unknown model, bad parameters) are rejected
  immediately without creating a job.
"""


def _multipart_schema() -> dict[str, Any]:
    """Form-field schema derived from ``VisionRequest`` so both content types stay in sync."""
    properties: dict[str, Any] = {}
    for name, schema in VisionRequest.model_json_schema()["properties"].items():
        field = dict(schema)
        if "anyOf" in field:  # Optional[X]: a form field is simply omitted when unset
            field.update(next(s for s in field.pop("anyOf") if s.get("type") != "null"))
        properties[name] = field
    properties["response_format"] = {
        "type": "string",
        "title": "Response Format",
        "description": properties["response_format"]["description"] + " In a form, send it as a JSON-encoded string.",
    }
    properties[UPLOAD_FIELD] = {
        "type": "array",
        "items": {"type": "string", "format": "binary"},
        "title": "Images",
        "description": (
            "One or more image files (JPEG, PNG, WebP, GIF), each at most 10 MB. "
            "Repeat the field for several images."
        ),
    }
    return {"type": "object", "required": ["prompt"], "properties": properties}


_REQUEST_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": VisionRequest.model_json_schema(),
                "examples": {
                    "image_url": {
                        "summary": "Analyse an image by URL",
                        "value": {"prompt": "Describe this image in one sentence.", "image_url": "https://httpbin.org/image/png"},
                    },
                    "tuned": {
                        "summary": "Deterministic, short answer",
                        "value": {
                            "prompt": "What colour is the main object? Answer in one word.",
                            "image_url": "https://httpbin.org/image/jpeg",
                            "model": "vision-default",
                            "temperature": 0,
                            "max_tokens": 20,
                        },
                    },
                    "async": {
                        "summary": "Async job (returns 202 + job_id)",
                        "value": {
                            "prompt": "List every UI element visible in this screenshot.",
                            "image_url": "https://httpbin.org/image/png",
                            "mode": "async",
                        },
                    },
                },
            },
            "multipart/form-data": {
                "schema": _multipart_schema(),
                "encoding": {UPLOAD_FIELD: {"contentType": "image/jpeg, image/png, image/webp, image/gif"}},
                "examples": {
                    "upload": {
                        "summary": "Upload one image",
                        "description": "`images` carries the binary file content.",
                        "value": {"prompt": "Describe this image", "images": ["<binary JPEG data>"]},
                    },
                    "compare": {
                        "summary": "Two uploads plus a URL, async",
                        "value": {
                            "prompt": "Compare these images",
                            "images": ["<binary PNG data>", "<binary PNG data>"],
                            "image_url": "https://httpbin.org/image/jpeg",
                            "mode": "async",
                            "temperature": "0",
                        },
                    },
                },
            },
        },
    }
}

_EXAMPLE_RESPONSE = VisionResponse.model_config["json_schema_extra"]["examples"][0]  # type: ignore[index]

_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "model": VisionResponse,
        "description": "Synchronous result (`mode=sync`).",
        "headers": {**RATE_LIMIT_HEADERS, **REQUEST_ID_HEADER},
        "content": {
            "application/json": {
                "examples": {
                    "result": {"summary": "Model answer", "value": _EXAMPLE_RESPONSE},
                    "mock": {
                        "summary": "Answer from the default mock backend",
                        "value": {
                            **_EXAMPLE_RESPONSE,
                            "result": {
                                "text": "Mock analysis of 1 image(s) [4be1f02c9a31] for prompt: Describe this image"
                            },
                            "usage": {"input_tokens": 259, "output_tokens": 11},
                        },
                    },
                }
            }
        },
    },
    202: {
        "model": JobResponse,
        "description": "Job accepted (`mode=async`). Poll `GET /v1/jobs/{job_id}`.",
        "headers": {
            "Location": {"description": "URL of the job: `/v1/jobs/{job_id}`.", "schema": {"type": "string"}},
            **RATE_LIMIT_HEADERS,
        },
        "content": {
            "application/json": {
                "example": {"job_id": EXAMPLE_JOB_ID, "status": "pending", "result": None, "error": None}
            }
        },
    },
    400: error_response(
        "The request is well-formed but cannot be processed.",
        {
            "invalid_image": (
                "Upload is not a supported image",
                "invalid_request_error",
                "Unsupported image type. Allowed types: image/gif, image/jpeg, image/png, image/webp",
                None,
            ),
            "blocked_url": (
                "image_url points to a private or internal address",
                "invalid_request_error",
                "Image URL rejected: URL resolves to a non-public address",
                None,
            ),
            "no_image": (
                "Neither an upload nor an image_url",
                "invalid_request_error",
                "Provide at least one image (file upload or image_url)",
                None,
            ),
            "unknown_model": (
                "Unknown model name",
                "invalid_request_error",
                "Unknown model 'gpt-99'. Available: 'vision-default', 'mock-vision-1'",
                None,
            ),
            "invalid_json": ("Malformed JSON body", "invalid_request_error", "Request body is not valid JSON", None),
        },
    ),
    401: UNAUTHORIZED,
    403: FORBIDDEN,
    413: error_response(
        "An image or the whole request body is too large.",
        {
            "image_too_large": ("Image over 10 MB", "request_too_large", "Image exceeds the 10 MB limit", None),
            "body_too_large": ("Request body over the limit", "request_too_large", "Request body is too large", None),
        },
    ),
    415: error_response(
        "Content-Type is neither `application/json` nor `multipart/form-data`.",
        {
            "unsupported": (
                "Wrong Content-Type",
                "invalid_request_error",
                "Content-Type must be application/json or multipart/form-data",
                None,
            )
        },
    ),
    422: error_response(
        "A field failed validation; `param` names it.",
        {
            "missing_prompt": ("prompt missing", "invalid_request_error", "prompt: Field required", "prompt"),
            "out_of_range": (
                "temperature out of range",
                "invalid_request_error",
                "temperature: Input should be less than or equal to 2",
                "temperature",
            ),
            "unknown_field": (
                "Unknown field",
                "invalid_request_error",
                "is_admin: Extra inputs are not permitted",
                "is_admin",
            ),
        },
    ),
    402: error_response(
        "The monthly allowance of the caller's plan is used up (requests or spend). Resets on the 1st (UTC).",
        {
            "requests": (
                "Request quota used up",
                "quota_exceeded_error",
                "Monthly request quota of 5000 reached; it resets on 2026-10-01",
                None,
            ),
            "spend": (
                "Spend limit reached",
                "quota_exceeded_error",
                "Monthly spend limit of $25 reached; it resets on 2026-10-01",
                None,
            ),
        },
    ),
    429: error_response(
        "Rate limit exceeded. Retry after `Retry-After` seconds.",
        {"rate_limited": ("Too many requests", "rate_limit_error", "Rate limit exceeded", None)},
        headers={"Retry-After": {"description": "Seconds to wait.", "schema": {"type": "integer"}}, **RATE_LIMIT_HEADERS},
    ),
    502: error_response(
        "The model backend failed. Safe to retry.",
        {"model_failed": ("Inference error", "model_error", "Model inference failed", None)},
    ),
    503: error_response(
        "Temporarily unavailable. Retry later.",
        {
            "model_not_loaded": ("Model still loading", "service_unavailable", "Vision model is not loaded", None),
            "rate_limiter_down": (
                "Redis unreachable (fail-closed mode)",
                "service_unavailable",
                "Rate limiting is temporarily unavailable",
                None,
            ),
        },
    ),
    504: error_response(
        "Inference exceeded the configured timeout.",
        {"timeout": ("Inference timeout", "timeout_error", "Model inference timed out", None)},
    ),
}


def _body_limit(settings: Settings, multipart: bool) -> int:
    if not multipart:
        return _JSON_BODY_LIMIT
    return settings.max_images_per_request * settings.max_image_bytes + _MULTIPART_OVERHEAD


async def _read_body_limited(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Request body is too large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Request body is too large")
    return bytes(body)


def _validate_request(data: dict[str, Any]) -> VisionRequest:
    try:
        return VisionRequest.model_validate(data)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False, include_input=False)) from exc


def _parse_json(body: bytes) -> VisionRequest:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Request body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Request body must be a JSON object")
    return _validate_request(payload)


async def _parse_multipart(request: Request, body: bytes, settings: Settings) -> tuple[VisionRequest, list[bytes]]:
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    # Starlette converts multipart parse errors (incl. the limits below) into HTTP 400.
    form = await Request(request.scope, replay).form(
        max_files=settings.max_images_per_request,
        max_fields=len(VisionRequest.model_fields),
        max_part_size=64 * 1024,
    )
    try:
        fields: dict[str, Any] = {}
        files: list[bytes] = []
        for key, value in form.multi_items():
            if isinstance(value, UploadFile):
                if key != UPLOAD_FIELD:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Unexpected file field {key!r}; upload images as {UPLOAD_FIELD!r}",
                    )
                data = await value.read(settings.max_image_bytes + 1)
                if len(data) > settings.max_image_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail=f"Image exceeds the {settings.max_image_bytes // (1024 * 1024)} MB limit",
                    )
                files.append(data)
            elif key == UPLOAD_FIELD:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"{UPLOAD_FIELD!r} must be uploaded as files",
                )
            elif key in fields:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Duplicate form field {key!r}")
            elif key == "response_format":
                try:
                    fields[key] = json.loads(value)
                except json.JSONDecodeError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST, detail="response_format must be a JSON object"
                    ) from exc
            else:
                # Unknown keys are passed through so the schema (extra="forbid") reports them.
                fields[key] = value
    finally:
        await form.close()
    return _validate_request(fields), files


@router.post(
    "/vision",
    response_model=None,
    summary="Analyse images with a vision-language model",
    description=_DESCRIPTION,
    response_description="Synchronous result (`mode=sync`).",
    responses=_RESPONSES,
    openapi_extra=_REQUEST_BODY,
)
async def create_vision(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    auth: Annotated[tuple[APIKey, User], Depends(get_current_api_key)],
    _rate_limit: Annotated[None, Depends(get_rate_limiter)],
    pricing: Annotated[PlanPricing, Depends(enforce_quota)],
    service: Annotated[VisionService, Depends(get_vision_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> VisionResponse | JobResponse:
    settings: Settings = request.app.state.settings
    api_key, user = auth
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    if content_type == "application/json":
        vision_request = _parse_json(await _read_body_limited(request, _body_limit(settings, multipart=False)))
        uploads: list[bytes] = []
    elif content_type == "multipart/form-data":
        body = await _read_body_limited(request, _body_limit(settings, multipart=True))
        vision_request, uploads = await _parse_multipart(request, body, settings)
    else:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Content-Type must be application/json or multipart/form-data",
        )

    usage = UsageContext(
        user_id=user.id,
        api_key_id=api_key.id,
        request_id=request_id_ctx.get() or uuid.uuid4().hex,
        model=service.model.name,
        image_count=len(uploads) + (1 if vision_request.image_url else 0),
        pricing=pricing,
    )

    if vision_request.mode == "async":
        # Reject obviously invalid requests now instead of creating a job that is doomed to fail.
        service.validate(vision_request, len(uploads))
        job = Job(user_id=user.id, api_key_id=api_key.id, status="pending")
        db.add(job)
        await db.commit()
        background_tasks.add_task(service.run_job, session_factory, job.id, vision_request, uploads, usage)
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["Location"] = f"/v1/jobs/{job.id}"
        return JobResponse(job_id=job.id, status="pending")

    started = time.perf_counter()
    try:
        result = await service.process_request(vision_request, uploads or None)
    except Exception as exc:
        message = public_error_message(exc)
        # Inline (not a background task): FastAPI drops background tasks when the route raises.
        await record_usage(
            session_factory,
            usage,
            status="error",
            error_message=message,
            processing_time_ms=int((time.perf_counter() - started) * 1000),
        )
        if isinstance(exc, ModelTimeoutError):
            logger.warning("inference timed out", extra={"model": service.model.name})
            raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=message) from exc
        if isinstance(exc, ModelError):
            logger.error("inference failed", extra={"model": service.model.name, "error": str(exc)}, exc_info=exc)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=message) from exc
        raise

    # Success: write the usage row after the response is sent, adding no latency.
    background_tasks.add_task(
        record_usage,
        session_factory,
        usage,
        status="success",
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        processing_time_ms=int((time.perf_counter() - started) * 1000),
    )
    return result
