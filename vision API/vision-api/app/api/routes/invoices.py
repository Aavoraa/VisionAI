"""POST /v1/invoices/extract: invoice image or PDF -> structured invoice fields (API key auth)."""

import asyncio
import logging
import uuid
from pathlib import PurePath
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Path, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import UploadFile
from starlette.types import Message

from app.api.dependencies import enforce_quota, get_current_api_key, get_vision_service
from app.api.docs import FORBIDDEN, RATE_LIMIT_HEADERS, REQUEST_ID_HEADER, UNAUTHORIZED, error_response
from app.api.routes.vision import _RESPONSES as VISION_RESPONSES
from app.api.routes.vision import _read_body_limited
from app.core.config import Settings
from app.core.logging import request_id_ctx
from app.core.rate_limit import get_rate_limiter
from app.database.database import get_session_factory
from app.database.models import APIKey, User
from app.models.base import ModelError
from app.schemas.common import UTCDateTime, UUID_PATTERN
from app.schemas.invoice import (
    InvoiceProgress,
    EXAMPLE_API_INVOICE,
    EXAMPLE_EXTRACTION,
    ApiInvoice,
    ApiInvoiceList,
    InvoiceAccepted,
    InvoiceExtraction,
    ReviewStatus,
)
from app.services.billing_service import PlanPricing
from app.database.models import InvoiceRecord
from app.services.document_service import DocumentError, detect_document_type, load_pages
from app.services.document_store import DocumentStore
from app.services.invoice_extraction import ExtractionUnavailableError, InvoiceExtractor
from app.services.invoice_export import clean_record
from app.services.invoice_integration import notify_approved
from app.services.invoice_service import extract_and_store
from app.services.usage_service import UsageContext
from app.services.vision_service import VisionService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/invoices", tags=["invoices"])

UPLOAD_FIELD = "file"
_MULTIPART_OVERHEAD = 64 * 1024

_DESCRIPTION = """
Upload one invoice (JPEG, PNG, WebP, GIF or a PDF of up to 10 pages) as `multipart/form-data`
field `file`; get back its fields as structured JSON: invoice number and date, seller and
buyer (names, addresses, GSTINs), place of supply, line items and totals.

Every field has a `value` (`null` when it is not on the invoice; nothing is guessed), a
`status` (`ok`, `check` = verify before use, `missing`), a heuristic `confidence`, the
`source_text` it was read from and a `note` explaining any `check`. The OCR model reads the
document; when a reasoning model is configured, it cross-checks the fields. Arithmetic, GST
and amount-in-words checks run on the result (`validation`).

The invoice is stored for review: `id` opens it in the dashboard, and `review_status` says
whether it was approved automatically (confident and every check passed), needs review, or
should be reprocessed.

With `?mode=async` the call returns at once (`202`, `Location: /v1/invoices/{id}`) and the
invoice is read in the background; poll `GET /v1/invoices/{id}`. Use it for multi-page PDFs
and slow (CPU) servers.

Needs the server to run PaddleOCR-VL (see README, "GGUF models with llama.cpp"). Processing
takes seconds per page on a GPU and a few minutes per page on a CPU. Billed like one
`/v1/vision` request, by the tokens both models used.
""".strip()

_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "model": InvoiceExtraction,
        "description": "The extracted invoice (`mode=sync`).",
        "headers": {**RATE_LIMIT_HEADERS, **REQUEST_ID_HEADER},
        "content": {"application/json": {"example": EXAMPLE_EXTRACTION}},
    },
    202: {
        "model": InvoiceAccepted,
        "description": "Accepted (`mode=async`): being read in the background.",
        "headers": {"Location": {"description": "`/v1/invoices/{id}`.", "schema": {"type": "string"}}, **RATE_LIMIT_HEADERS},
        "content": {"application/json": {"example": InvoiceAccepted.model_config["json_schema_extra"]["examples"][0]}},  # type: ignore[index]
    },
    400: error_response(
        "The upload cannot be processed.",
        {
            "unsupported_file": (
                "Not a PDF or image",
                "invalid_request_error",
                "Unsupported file type. Upload a PDF or a JPEG, PNG, WebP or GIF image.",
                None,
            ),
            "too_many_pages": ("PDF too long", "invalid_request_error", "The PDF has 14 pages; at most 10 are allowed", None),
            "no_file": ("No file uploaded", "invalid_request_error", "Upload the invoice as the 'file' field", None),
        },
    ),
    401: UNAUTHORIZED,
    402: VISION_RESPONSES[402],
    403: FORBIDDEN,
    413: error_response(
        "The upload is too large.",
        {"too_large": ("Over the size limit", "request_too_large", "Request body is too large", None)},
    ),
    415: error_response(
        "Content-Type is not `multipart/form-data`.",
        {"unsupported": ("Wrong Content-Type", "invalid_request_error", "Content-Type must be multipart/form-data", None)},
    ),
    422: error_response(
        "A query parameter is invalid.",
        {"mode": ("Unknown mode", "invalid_request_error", "mode: Input should be 'sync' or 'async'", "mode")},
    ),
    429: VISION_RESPONSES[429],
    502: VISION_RESPONSES[502],
    503: error_response(
        "Invoice extraction is not available on this server, or the model is still loading.",
        {
            "no_document_model": (
                "Serving model cannot read documents",
                "service_unavailable",
                "Invoice extraction needs PaddleOCR-VL; the serving model is 'mock-vision-1'",
                None,
            ),
            "model_not_loaded": ("Model still loading", "service_unavailable", "Vision model is not loaded", None),
        },
    ),
    504: VISION_RESPONSES[504],
}

_REQUEST_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": [UPLOAD_FIELD],
                    "properties": {
                        UPLOAD_FIELD: {"type": "string", "format": "binary", "description": "The invoice: an image or a PDF."}
                    },
                },
                "encoding": {UPLOAD_FIELD: {"contentType": "application/pdf, image/jpeg, image/png, image/webp, image/gif"}},
            }
        },
    }
}


def get_invoice_mapper(request: Request) -> Any:
    return getattr(request.app.state, "invoice_mapper", None)


def get_invoice_fallback(request: Request) -> Any:
    """The hybrid fallback reader (``invoice_fallback``), or None when it is not configured."""
    return getattr(request.app.state, "invoice_fallback", None)


def get_document_store(request: Request) -> DocumentStore:
    return DocumentStore(request.app.state.settings.document_storage_dir)


async def _read_upload(request: Request, body: bytes, settings: Settings) -> tuple[bytes, str]:
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    # Exactly one file and no other fields (Starlette answers anything else with 400).
    form = await Request(request.scope, replay).form(max_files=1, max_fields=0, max_part_size=64 * 1024)
    try:
        upload = form.get(UPLOAD_FIELD)
        if not isinstance(upload, UploadFile):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Upload the invoice as the {UPLOAD_FIELD!r} field")
        data = await upload.read(settings.max_document_bytes + 1)
        if len(data) > settings.max_document_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"The file exceeds the {settings.max_document_bytes // (1024 * 1024)} MB limit",
            )
        # Only the base name, for display: paths are never built from it.
        name = PurePath((upload.filename or "invoice").replace("\\", "/")).name[:255] or "invoice"
        return data, name
    finally:
        await form.close()


async def _extract_in_background(
    session_factory: async_sessionmaker[AsyncSession],
    record_id: str,
    pages: list[bytes],
    model: Any,
    mapper: Any,
    settings: Settings,
    usage: UsageContext,
    progress: Any = None,
    fallback: Any = None,
) -> None:
    """``mode=async``: failures are stored on the invoice (status ``failed``), never raised."""
    try:
        result = await extract_and_store(session_factory, record_id, pages, model, mapper, settings, usage, progress, fallback)
    except Exception:  # background context: extract_and_store already recorded the outcome
        logger.warning("background invoice extraction failed", extra={"invoice_id": record_id})
        return
    if result.review_status == "approved":
        await notify_approved(session_factory, settings, record_id)


@router.post(
    "/extract",
    response_model=None,
    summary="Extract structured fields from an invoice",
    description=_DESCRIPTION,
    response_description="The extracted invoice.",
    responses=_RESPONSES,
    openapi_extra=_REQUEST_BODY,
)
async def extract_invoice(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    auth: Annotated[tuple[APIKey, User], Depends(get_current_api_key)],
    _rate_limit: Annotated[None, Depends(get_rate_limiter)],
    pricing: Annotated[PlanPricing, Depends(enforce_quota)],
    service: Annotated[VisionService, Depends(get_vision_service)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    mapper: Annotated[Any, Depends(get_invoice_mapper)],
    fallback: Annotated[Any, Depends(get_invoice_fallback)],
    store: Annotated[DocumentStore, Depends(get_document_store)],
    mode: Annotated[Literal["sync", "async"], Query(description="`async`: return at once and read in the background.")] = "sync",
    progress_id: Annotated[str | None, Header(
        alias="X-Progress-Id", max_length=64,
        description="Optional random id (8-64 letters, digits, - or _): poll `GET /v1/invoices/progress/{id}` for live comments.",
    )] = None,
) -> InvoiceExtraction | InvoiceAccepted:
    settings: Settings = request.app.state.settings
    api_key, user = auth
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "multipart/form-data":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Content-Type must be multipart/form-data"
        )
    try:
        extractor = InvoiceExtractor(service.model, mapper)
    except ExtractionUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    body = await _read_body_limited(request, settings.max_document_bytes + _MULTIPART_OVERHEAD)
    data, filename = await _read_upload(request, body, settings)
    try:
        pages = await asyncio.to_thread(load_pages, data, settings)
    except DocumentError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    content_type = detect_document_type(data) or "application/octet-stream"
    async with session_factory() as db:
        record = InvoiceRecord(
            user_id=user.id, api_key_id=api_key.id, filename=filename, content_type=content_type,
            size_bytes=len(data), pages=len(pages), status="processing",
        )
        db.add(record)
        await db.commit()
        record_id = record.id
    await asyncio.to_thread(store.save, record_id, data, content_type, pages)

    usage = UsageContext(
        user_id=user.id,
        api_key_id=api_key.id,
        request_id=request_id_ctx.get() or uuid.uuid4().hex,
        model=service.model.name,
        image_count=len(pages),
        pricing=pricing,
    )
    progress = request.app.state.invoice_progress.open(progress_id, user.id) if progress_id else None
    if progress_id and progress is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="X-Progress-Id is malformed or already in use")
    if mode == "async":
        background_tasks.add_task(
            _extract_in_background, session_factory, record_id, pages, service.model, mapper, settings, usage, progress, fallback
        )
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["Location"] = f"/v1/invoices/{record_id}"
        return InvoiceAccepted(id=record_id, status_url=f"/v1/invoices/{record_id}")

    try:
        result = await extract_and_store(
            session_factory, record_id, pages, service.model, mapper, settings, usage, progress, fallback
        )
    except TimeoutError as exc:
        logger.warning("invoice extraction timed out", extra={"model": service.model.name, "pages": len(pages)})
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Model inference timed out") from exc
    except ModelError as exc:
        logger.error("invoice extraction failed", extra={"model": service.model.name, "error": str(exc)}, exc_info=exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Model inference failed") from exc
    if result.review_status == "approved":
        background_tasks.add_task(notify_approved, session_factory, settings, record_id)
    return result


# ---------------------------------------------------------------------------
# Reading stored invoices (API key): for ERP / accounting integrations
# ---------------------------------------------------------------------------

_LIST_DESCRIPTION = """
Invoices uploaded with any API key of the caller's account, newest change first, as plain
values ready to import (`data`) with the review state. To sync approved invoices into another
system, poll with `status=approved` and `updated_since` set to the last sync time (or use the
approval webhook, see README).
""".strip()


@router.get(
    "",
    response_model=ApiInvoiceList,
    summary="List stored invoices",
    description=_LIST_DESCRIPTION,
    response_description="A page of invoices.",
    responses={
        200: {"description": "A page of invoices.", "content": {"application/json": {"example": ApiInvoiceList.model_config["json_schema_extra"]["examples"][0]}}},  # type: ignore[index]
        401: UNAUTHORIZED,
        403: FORBIDDEN,
        422: error_response(
            "A query parameter is invalid.",
            {"limit": ("limit too large", "invalid_request_error", "limit: Input should be less than or equal to 200", "limit")},
        ),
        429: VISION_RESPONSES[429],
    },
)
async def list_invoices(
    auth: Annotated[tuple[APIKey, User], Depends(get_current_api_key)],
    _rate_limit: Annotated[None, Depends(get_rate_limiter)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    status_filter: Annotated[ReviewStatus | None, Query(alias="status", description="Only this review status.")] = None,
    updated_since: Annotated[UTCDateTime | None, Query(description="Only invoices changed after this time (ISO 8601).")] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="Page size, 1-200.")] = 50,
    offset: Annotated[int, Query(ge=0, le=1_000_000, description="Items to skip.")] = 0,
) -> ApiInvoiceList:
    _, user = auth
    query = select(InvoiceRecord).where(InvoiceRecord.user_id == user.id)
    if status_filter:
        query = query.where(InvoiceRecord.status == status_filter)
    if updated_since:
        query = query.where(InvoiceRecord.updated_at > updated_since)
    async with session_factory() as db:
        total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
        records = (
            await db.execute(query.order_by(InvoiceRecord.updated_at.desc(), InvoiceRecord.id).limit(limit).offset(offset))
        ).scalars().all()
    return ApiInvoiceList(items=[ApiInvoice(**clean_record(r)) for r in records], total=total, limit=limit, offset=offset)


@router.get(
    "/progress/{progress_id}",
    response_model=InvoiceProgress,
    summary="Live comments of a running extraction",
    description=(
        "Comments the pipeline writes while it reads an invoice started with `X-Progress-Id` "
        "(same account): what it is reading, where on the page (`region`, fractions x1, y1, x2, y2) "
        "and what it found. Pass `after` = the number of comments already received. Kept in memory "
        "for an hour; not rate limited (poll every 1-2 s)."
    ),
    responses={
        200: {"description": "Comments so far.", "content": {"application/json": {"example": {
            "events": [{"t": 4.2, "stage": "bands", "text": "Reading item rows at full resolution: band 2 of 4",
                        "level": "info", "page": 1, "region": [0.0, 0.41, 1.0, 0.56]}],
            "count": 9, "done": False}}}},
        401: UNAUTHORIZED,
        404: error_response("No such progress id (or it belongs to another account).",
                            {"not_found": ("Unknown id", "not_found_error", "Progress not found", None)}),
        422: error_response("`progress_id` or `after` is malformed.",
                            {"malformed_id": ("Bad id", "invalid_request_error",
                                              "progress_id: String should match pattern '^[A-Za-z0-9_-]{8,64}$'", "progress_id")}),
    },
)
async def get_progress(
    request: Request,
    progress_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{8,64}$", description="The id sent as X-Progress-Id.")],
    auth: Annotated[tuple[APIKey, User], Depends(get_current_api_key)],
    after: Annotated[int, Query(ge=0, description="Comments already received.")] = 0,
) -> InvoiceProgress:
    _, user = auth
    found = request.app.state.invoice_progress.read(progress_id, user.id, after)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Progress not found")
    return InvoiceProgress(**found)


@router.get(
    "/{invoice_id}",
    response_model=ApiInvoice,
    summary="Get a stored invoice",
    description=(
        "One invoice of the caller's account: review status, confidence, plain field values "
        "(`data`) and the fields marked for checking. Poll this after `POST /v1/invoices/extract?mode=async`."
    ),
    response_description="The invoice.",
    responses={
        200: {"description": "The invoice.", "content": {"application/json": {"example": EXAMPLE_API_INVOICE}}},
        401: UNAUTHORIZED,
        403: FORBIDDEN,
        404: error_response("No such invoice (or it belongs to another account).",
                            {"not_found": ("Unknown invoice", "not_found_error", "Invoice not found", None)}),
        422: error_response("`invoice_id` is not a UUID.",
                            {"malformed_id": ("Not a UUID", "invalid_request_error", f"invoice_id: String should match pattern '{UUID_PATTERN}'", "invoice_id")}),
        429: VISION_RESPONSES[429],
    },
)
async def get_invoice(
    invoice_id: Annotated[str, Path(pattern=UUID_PATTERN, description="Invoice id.")],
    auth: Annotated[tuple[APIKey, User], Depends(get_current_api_key)],
    _rate_limit: Annotated[None, Depends(get_rate_limiter)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> ApiInvoice:
    _, user = auth
    async with session_factory() as db:
        record = (
            await db.execute(select(InvoiceRecord).where(InvoiceRecord.id == invoice_id.lower(), InvoiceRecord.user_id == user.id))
        ).scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    return ApiInvoice(**clean_record(record))
