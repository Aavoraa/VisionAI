"""FastAPI application entrypoint."""

import asyncio
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.services.invoice_progress import ProgressHub
from app.api.routes import admin, auth, dashboard, dashboard_invoices, health, invoices, jobs, usage, vision
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, request_id_ctx
from app.core.rate_limit import create_redis_client
from app.database.database import engine, get_session_factory
from app.models.base import VisionModel
from app.models.llama_cpp import LlamaCppVisionModel
from app.models.local_transformers import MockVisionModel, load_local_model
from app.services.document_store import DocumentStore
from app.services.invoice_integration import recover_interrupted, retention_loop
from app.services.invoice_llm import LlmFieldMapper
from app.services.invoice_fallback import FallbackReader
from app.schemas.vision import ErrorDetail, VisionErrorResponse
from app.services.vision_service import VisionService

logger = logging.getLogger("app")
access_logger = logging.getLogger("app.access")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}
# Swagger UI needs to load its own assets, so the docs pages get a looser policy.
_DOCS_PATHS = frozenset({"/docs", "/redoc", "/docs/oauth2-redirect"})

API_DESCRIPTION = """
Analyse images with vision-language models.

**Authentication**: every `/v1` endpoint requires `Authorization: Bearer sk_live_...`
(click **Authorize** above). Keys are issued with
`python scripts/init_db.py create-key --email you@example.com`.

**Sending images**: `POST /v1/vision` accepts either
* `application/json` with an `image_url` (fetched server-side with SSRF protection), or
* `multipart/form-data` with one or more `images` files (JPEG, PNG, WebP, GIF).

All images are validated by content, stripped of metadata (EXIF/GPS) and downscaled.

**Sync vs async**: `mode=sync` (default) returns the result directly; `mode=async` returns
`202` with a `job_id` to poll at `GET /v1/jobs/{job_id}`.

**Rate limits**: per user, fixed window. Responses carry `X-RateLimit-Limit`,
`X-RateLimit-Remaining` and `X-RateLimit-Reset`; a `429` includes `Retry-After`.

**Errors** always use one shape:
`{"error": {"type": "...", "message": "...", "param": null}, "request_id": "..."}`.
Quote `request_id` (also in the `X-Request-ID` header) when reporting problems.
"""

OPENAPI_TAGS = [
    {"name": "vision", "description": "Submit images for analysis (sync or async)."},
    {"name": "invoices", "description": "Invoice image/PDF -> structured fields (needs PaddleOCR-VL)."},
    {"name": "jobs", "description": "Poll async jobs created with `mode=async`."},
    {"name": "usage", "description": "Plan, charges and remaining monthly quota for the calling API key."},
    {"name": "auth", "description": "Dashboard accounts: cookie sessions with CSRF protection (not for API clients)."},
    {"name": "dashboard", "description": "The signed-in user's API keys, usage and jobs (session cookie)."},
    {"name": "admin", "description": "Administrator-only user, key and platform-usage management (session cookie)."},
    {"name": "health", "description": "Liveness probe (no authentication)."},
]

_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "quota_exceeded_error",
    403: "permission_error",
    404: "not_found_error",
    409: "conflict_error",
    405: "invalid_request_error",
    413: "request_too_large",
    415: "invalid_request_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
    502: "model_error",
    503: "service_unavailable",
    504: "timeout_error",
}


def build_vision_model(settings: Settings) -> VisionModel:
    """Blocking (may load multi-GB weights): run once at startup, off the event loop."""
    if settings.use_mock_model:
        if settings.is_production:
            raise RuntimeError("Refusing to serve MockVisionModel in production; set VISION_USE_MOCK_MODEL=false")
        return MockVisionModel()
    if settings.model_backend == "llama_cpp":
        return LlamaCppVisionModel(
            settings.model_name, settings.llama_cpp_url, timeout_seconds=settings.model_timeout_seconds,
            concurrency=settings.llama_cpp_concurrency,
        )
    return load_local_model(settings.model_name, device=settings.model_device)


def error_response(
    status_code: int,
    message: str,
    *,
    param: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = VisionErrorResponse(
        error=ErrorDetail(type=_ERROR_TYPES.get(status_code, "api_error"), message=message, param=param),
        request_id=request_id_ctx.get(),
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(), headers=dict(headers or {}))


def _format_validation_errors(errors: Sequence[Any]) -> tuple[str, str | None]:
    parts: list[str] = []
    first_param: str | None = None
    for err in list(errors)[:5]:
        loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path", "header")]
        param = ".".join(loc) or None
        first_param = first_param or param
        parts.append(f"{param}: {err.get('msg')}" if param else str(err.get("msg")))
    return "; ".join(parts) or "Invalid request", first_param


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        redis = create_redis_client(settings)
        try:
            await redis.ping()
        except (RedisError, OSError) as exc:
            # Not fatal: the rate limiter applies settings.rate_limit_fail_open per request.
            logger.warning("redis unreachable at startup", extra={"error": str(exc)})
        app.state.redis = redis

        model = await asyncio.to_thread(build_vision_model, settings)
        app.state.vision_service = VisionService(model, settings)
        logger.info("vision model ready", extra={"model": model.name, "mock": settings.use_mock_model})
        # Optional reasoning model for invoice extraction. Checked per request, not here:
        # if it is down, extraction falls back to the rules with a warning.
        mapper = (
            LlmFieldMapper(
                settings.extraction_llm_name,
                settings.extraction_llm_url,
                timeout_seconds=settings.extraction_llm_timeout_seconds,
            )
            if settings.extraction_llm_url
            else None
        )
        app.state.invoice_mapper = mapper
        # Optional hybrid fallback: a larger vision model for invoices whose checks fail.
        fallback = (
            FallbackReader(
                settings.invoice_fallback_llm_name,
                settings.invoice_fallback_llm_url,
                timeout_seconds=settings.invoice_fallback_timeout_seconds,
            )
            if settings.invoice_fallback_llm_url
            else None
        )
        app.state.invoice_fallback = fallback

        # Stored invoices: recover from a restart, then purge expired files periodically. The
        # session factory is looked up like a dependency so tests' overrides apply here too.
        session_factory = app.dependency_overrides.get(get_session_factory, get_session_factory)()
        try:
            await recover_interrupted(session_factory)
        except Exception as exc:  # best effort: the database may be starting too; never block startup
            logger.warning("could not recover interrupted invoices", extra={"error": str(exc)})
        retention = (
            asyncio.create_task(
                retention_loop(session_factory, DocumentStore(settings.document_storage_dir), settings.invoice_file_retention_days)
            )
            if settings.invoice_file_retention_days
            else None
        )
        try:
            yield
        finally:
            if retention is not None:
                retention.cancel()
            app.state.vision_service = None
            app.state.invoice_mapper = None
            app.state.invoice_fallback = None
            app.state.redis = None
            await model.close()
            if mapper is not None:
                await mapper.close()
            if fallback is not None:
                await fallback.close()
            await redis.aclose()
            await engine.dispose()

    docs_enabled = not settings.is_production
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=API_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    app.state.settings = settings
    app.state.vision_service = None
    app.state.redis = None
    app.state.invoice_progress = ProgressHub()  # live extraction comments (in memory only)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return error_response(exc.status_code, str(exc.detail), headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        message, param = _format_validation_errors(exc.errors())
        return error_response(422, message, param=param)

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID_RE.fullmatch(incoming) else uuid.uuid4().hex
        token = request_id_ctx.set(request_id)
        started = time.perf_counter()
        status_code = 500
        try:
            try:
                response = await call_next(request)
                status_code = response.status_code
            except Exception:
                logger.exception("unhandled error")
                response = error_response(500, "Internal server error")
            finally:
                access_logger.info(
                    "request",
                    extra={
                        "method": request.method,
                        "path": request.url.path,  # query string deliberately omitted
                        "status": status_code,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    },
                )
        finally:
            request_id_ctx.reset(token)

        response.headers["X-Request-ID"] = request_id
        for header, value in _SECURITY_HEADERS.items():
            if header == "Content-Security-Policy" and request.url.path in _DOCS_PATHS:
                continue
            response.headers.setdefault(header, value)
        return response

    app.include_router(health.router)
    app.include_router(vision.router)
    app.include_router(invoices.router)
    app.include_router(jobs.router)
    app.include_router(usage.router)
    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(dashboard_invoices.router)
    app.include_router(admin.router)
    return app
