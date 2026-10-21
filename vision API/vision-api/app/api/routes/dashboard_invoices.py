"""Stored invoices for the signed-in user: list, review, correct, approve, reprocess, delete."""

import asyncio
import datetime as dt
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.dependencies import AuthenticatedSession, get_current_session, get_vision_service
from app.api.docs import NOT_SIGNED_IN, SESSION_FORBIDDEN, error_response, invalid_id_response, not_found_response
from app.api.routes.invoices import get_document_store, get_invoice_fallback, get_invoice_mapper
from app.core.logging import request_id_ctx
from app.database.database import get_db, get_session_factory
from app.database.models import InvoiceCorrection, InvoiceRecord, User
from app.schemas.common import UUID_PATTERN
from app.schemas.invoice import (
    EXAMPLE_EXTRACTION,
    Invoice,
    InvoiceCorrectionOut,
    InvoiceCorrections,
    InvoiceDetail,
    InvoiceExtraction,
    InvoiceList,
    InvoiceSummary,
    ReviewStatus,
)
from app.services.billing_service import PlanPricing, QuotaExceeded, check_quota
from app.services.document_store import DocumentStore
from app.services.invoice_export import clean_record, invoices_csv, items_csv
from app.services.invoice_extraction import ExtractionUnavailableError, InvoiceExtractor
from app.services.invoice_integration import notify_approved
from app.services.invoice_service import CorrectionError, apply_corrections, correction_rows, finalize, reprocess, store_result
from app.services.usage_service import UsageContext
from app.services.vision_service import VisionService

router = APIRouter(prefix="/v1/dashboard/invoices", tags=["dashboard"])

Session = Annotated[AuthenticatedSession, Depends(get_current_session)]
Db = Annotated[AsyncSession, Depends(get_db)]
InvoiceId = Annotated[
    str, Path(pattern=UUID_PATTERN, description="Invoice id (from the list).", examples=[EXAMPLE_EXTRACTION["id"]])
]

_SUMMARY_EXAMPLE: dict[str, Any] = {
    "id": EXAMPLE_EXTRACTION["id"],
    "filename": "invoice-aug.pdf",
    "status": "needs_review",
    "confidence": 0.75,
    "invoice_number": "AB/7/26-27",
    "vendor_name": "ACME TEXTILES PRIVATE LIMITED",
    "total_amount": "945.00",
    "pages": 1,
    "error": None,
    "created_at": "2026-09-26T10:15:00Z",
    "updated_at": "2026-09-26T10:17:22Z",
    "approved_at": None,
    "approved_automatically": False,
}
_DETAIL_EXAMPLE: dict[str, Any] = {
    **_SUMMARY_EXAMPLE,
    "content_type": "application/pdf",
    "extraction": EXAMPLE_EXTRACTION,
    "corrections": [
        {
            "field": "vendor_name", "old_value": "ACME TEXTLES", "new_value": "ACME TEXTILES PRIVATE LIMITED",
            "old_status": "check", "corrected_by": "reviewer@example.com", "created_at": "2026-09-26T10:20:05Z",
        }
    ],
}
_LIST_EXAMPLE = {
    "items": [_SUMMARY_EXAMPLE],
    "total": 1,
    "counts": {"processing": 0, "needs_review": 1, "approved": 4, "reprocess": 0, "failed": 0},
    "limit": 25,
    "offset": 0,
}
_NOT_FOUND = not_found_response("Invoice")
_BUSY = error_response(
    "The invoice is being processed; try again when it has finished.",
    {"processing": ("Still processing", "conflict_error", "The invoice is still being processed", None)},
)


def _json(example: dict[str, Any], description: str) -> dict[str, Any]:
    return {"description": description, "content": {"application/json": {"example": example}}}


async def _own(db: AsyncSession, user_id: str, invoice_id: str) -> InvoiceRecord:
    record = (
        await db.execute(select(InvoiceRecord).where(InvoiceRecord.id == invoice_id.lower(), InvoiceRecord.user_id == user_id))
    ).scalar_one_or_none()
    if record is None:  # someone else's invoice looks exactly like a missing one
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    return record


def _summary(record: InvoiceRecord) -> dict[str, Any]:
    return {
        "id": record.id, "filename": record.filename, "status": record.status, "confidence": record.confidence,
        "invoice_number": record.invoice_number, "vendor_name": record.vendor_name, "total_amount": record.total_amount,
        "pages": record.pages, "error": record.error_message, "created_at": record.created_at,
        "updated_at": record.updated_at, "approved_at": record.approved_at,
        "approved_automatically": record.status == "approved" and record.approved_by is None,
    }


async def _detail(db: AsyncSession, record: InvoiceRecord, store: DocumentStore | None = None) -> InvoiceDetail:
    rows = (
        await db.execute(
            select(InvoiceCorrection, User.email)
            .outerjoin(User, User.id == InvoiceCorrection.user_id)
            .where(InvoiceCorrection.invoice_id == record.id)
            .order_by(InvoiceCorrection.created_at, InvoiceCorrection.id)
        )
    ).all()
    corrections = [
        InvoiceCorrectionOut(
            field=c.field, old_value=c.old_value, new_value=c.new_value, old_status=c.old_status,
            corrected_by=email, created_at=c.created_at,
        )
        for c, email in rows
    ]
    extraction = InvoiceExtraction.model_validate(record.extraction) if record.extraction else None
    files = await asyncio.to_thread(store.has_files, record.id) if store is not None else True
    return InvoiceDetail(
        **_summary(record), content_type=record.content_type, files_available=files, extraction=extraction, corrections=corrections
    )


def _not_busy(record: InvoiceRecord) -> None:
    if record.status == "processing":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The invoice is still being processed")


@router.get(
    "",
    response_model=InvoiceList,
    summary="My invoices",
    description=(
        "Invoices uploaded with the signed-in user's API keys (`POST /v1/invoices/extract`), newest "
        "first, optionally filtered by review status, with counts per status for the review tabs."
    ),
    response_description="A page of invoices.",
    responses={
        200: _json(_LIST_EXAMPLE, "A page of invoices."),
        401: NOT_SIGNED_IN,
        403: SESSION_FORBIDDEN,
        422: error_response(
            "A query parameter is out of range or unknown.",
            {
                "limit": ("limit too large", "invalid_request_error", "limit: Input should be less than or equal to 100", "limit"),
                "status": ("Unknown status", "invalid_request_error", "status: Input should be 'processing', 'needs_review', 'approved', 'reprocess' or 'failed'", "status"),
            },
        ),
    },
)
async def list_invoices(
    auth: Session,
    db: Db,
    status_filter: Annotated[ReviewStatus | None, Query(alias="status", description="Only this review status.")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Page size, 1-100.")] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000, description="Items to skip.")] = 0,
) -> InvoiceList:
    owner = InvoiceRecord.user_id == auth.user.id
    counts = dict(
        (await db.execute(select(InvoiceRecord.status, func.count()).where(owner).group_by(InvoiceRecord.status))).all()
    )
    query = select(InvoiceRecord).where(owner)
    if status_filter:
        query = query.where(InvoiceRecord.status == status_filter)
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    records = (
        await db.execute(query.order_by(InvoiceRecord.created_at.desc(), InvoiceRecord.id).limit(limit).offset(offset))
    ).scalars()
    return InvoiceList(
        items=[InvoiceSummary(**_summary(r)) for r in records],
        total=total,
        counts={s: counts.get(s, 0) for s in ("processing", "needs_review", "approved", "reprocess", "failed")},
        limit=limit,
        offset=offset,
    )


EXPORT_LIMIT = 5000


def _json_error(response: dict[str, Any]) -> dict[str, Any]:
    """An error response documented as JSON on a route whose success body is not JSON.

    FastAPI files a response ``model`` under the route's own media type (text/plain here), so
    the standard error schema is referenced explicitly instead.
    """
    doc = {key: value for key, value in response.items() if key != "model"}
    media = dict(doc["content"]["application/json"])
    media["schema"] = {"$ref": "#/components/schemas/VisionErrorResponse"}
    return {**doc, "content": {"application/json": media}}


@router.get(
    "/export",
    response_class=PlainTextResponse,
    summary="Export invoices",
    description=(
        "Download the signed-in user's invoices (optionally one review status), newest first, at "
        f"most {EXPORT_LIMIT}: `csv` = one row per invoice with every header field, `items_csv` = one "
        "row per line item, `json` = the same records as the integration API (plain values + flags)."
    ),
    response_description="The export file.",
    responses={
        200: {
            "description": "The export file (attachment).",
            "content": {
                "text/csv": {"schema": {"type": "string"}},
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}, "description": "format=json: a JSON array of invoices"},
            },
        },
        401: _json_error(NOT_SIGNED_IN), 403: _json_error(SESSION_FORBIDDEN),
        422: _json_error(error_response(
            "A query parameter is invalid.",
            {"format": ("Unknown format", "invalid_request_error", "format: Input should be 'csv', 'items_csv' or 'json'", "format")},
        )),
    },
)
async def export_invoices(
    auth: Session,
    db: Db,
    status_filter: Annotated[ReviewStatus | None, Query(alias="status", description="Only this review status.")] = None,
    export_format: Annotated[Literal["csv", "items_csv", "json"], Query(alias="format", description="File format.")] = "csv",
) -> Response:
    query = select(InvoiceRecord).where(InvoiceRecord.user_id == auth.user.id)
    if status_filter:
        query = query.where(InvoiceRecord.status == status_filter)
    records = list(
        (await db.execute(query.order_by(InvoiceRecord.created_at.desc(), InvoiceRecord.id).limit(EXPORT_LIMIT))).scalars()
    )
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
    name = f"invoices-{status_filter or 'all'}-{stamp}"
    if export_format == "json":
        return JSONResponse(
            [clean_record(r) for r in records], headers={"Content-Disposition": f'attachment; filename="{name}.json"'}
        )
    text = invoices_csv(records) if export_format == "csv" else items_csv(records)
    suffix = "" if export_format == "csv" else "-items"
    # A byte-order mark makes Excel read the UTF-8 (₹, names) correctly.
    return Response(
        "\ufeff" + text, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}{suffix}.csv"'},
    )


@router.get(
    "/{invoice_id}",
    response_model=InvoiceDetail,
    summary="Invoice for review",
    description=(
        "One invoice with its current extraction (every field with value, status, confidence and "
        "note; the validation report) and the history of reviewer corrections."
    ),
    response_description="The invoice.",
    responses={
        200: _json(_DETAIL_EXAMPLE, "The invoice."), 401: NOT_SIGNED_IN, 403: SESSION_FORBIDDEN,
        404: _NOT_FOUND, 422: invalid_id_response("invoice_id"),
    },
)
async def get_invoice(
    invoice_id: InvoiceId, auth: Session, db: Db, store: Annotated[DocumentStore, Depends(get_document_store)]
) -> InvoiceDetail:
    return await _detail(db, await _own(db, auth.user.id, invoice_id), store)


@router.get(
    "/{invoice_id}/pages/{page}",
    response_class=FileResponse,
    summary="Page image",
    description=(
        "Page `page` (1-based) of the invoice as a PNG, exactly as the OCR model saw it (sanitised: "
        "no metadata). Only the owner can load it."
    ),
    response_description="The page image.",
    responses={
        200: {"description": "The page image.", "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}},
        401: NOT_SIGNED_IN, 403: SESSION_FORBIDDEN, 404: not_found_response("Page"), 422: invalid_id_response("invoice_id"),
    },
)
async def invoice_page(
    invoice_id: InvoiceId,
    page: Annotated[int, Path(ge=1, le=1000, description="Page number, from 1.")],
    auth: Session,
    db: Db,
    store: Annotated[DocumentStore, Depends(get_document_store)],
) -> FileResponse:
    record = await _own(db, auth.user.id, invoice_id)
    path = await asyncio.to_thread(store.page_path, record.id, page)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found")
    # Private: the browser may keep it for the review session, shared caches must not.
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


@router.patch(
    "/{invoice_id}",
    response_model=InvoiceDetail,
    summary="Correct invoice fields",
    description=(
        "Set field values (as text; empty clears a value). Each change is logged with the reviewer, "
        "the corrected fields get confidence 1, and the whole invoice is validated and scored again, "
        "so a correction that makes the totals disagree is flagged at once. The invoice stays in "
        "review until it is approved."
    ),
    response_description="The corrected invoice.",
    responses={
        200: _json(_DETAIL_EXAMPLE, "The corrected invoice."),
        400: error_response(
            "A field name or value is invalid.",
            {
                "unknown_field": ("Unknown field", "invalid_request_error", "unknown field 'colour'", None),
                "not_a_number": ("Not a number", "invalid_request_error", "total_amount: 'abc' is not a number", None),
            },
        ),
        401: NOT_SIGNED_IN, 403: SESSION_FORBIDDEN, 404: _NOT_FOUND, 409: _BUSY, 422: invalid_id_response("invoice_id"),
    },
)
async def correct_invoice(invoice_id: InvoiceId, body: InvoiceCorrections, auth: Session, db: Db, request: Request) -> InvoiceDetail:
    record = await _own(db, auth.user.id, invoice_id)
    _not_busy(record)
    if record.extraction is None or record.base_invoice is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The invoice has no extraction to correct; reprocess it")
    base = Invoice.model_validate(record.base_invoice)
    try:
        corrected, changed = apply_corrections(base, body.fields, auth.user.email)
    except CorrectionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if changed:
        result = finalize(InvoiceExtraction.model_validate(record.extraction), request.app.state.settings, record.id, corrected)
        result = result.model_copy(update={"review_status": "needs_review"})  # a person decides from here
        store_result(record, result, corrected)
        db.add_all(correction_rows(record.id, auth.user.id, changed))
        await db.commit()
        await db.refresh(record)
    return await _detail(db, record)


@router.post(
    "/{invoice_id}/approve",
    response_model=InvoiceDetail,
    summary="Approve an invoice",
    description=(
        "Mark the invoice as approved by the signed-in user (after review). Allowed in any state "
        "except while processing; fields marked `check` stay visible in the record."
    ),
    response_description="The approved invoice.",
    responses={
        200: _json({**_DETAIL_EXAMPLE, "status": "approved", "approved_at": "2026-09-26T10:21:40Z"}, "The approved invoice."),
        401: NOT_SIGNED_IN, 403: SESSION_FORBIDDEN, 404: _NOT_FOUND, 409: _BUSY, 422: invalid_id_response("invoice_id"),
    },
)
async def approve_invoice(
    invoice_id: InvoiceId,
    auth: Session,
    db: Db,
    request: Request,
    background_tasks: BackgroundTasks,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> InvoiceDetail:
    record = await _own(db, auth.user.id, invoice_id)
    _not_busy(record)
    if record.extraction is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The invoice has no extraction to approve; reprocess it")
    record.status, record.approved_by, record.approved_at = "approved", auth.user.id, dt.datetime.now(dt.UTC)
    record.extraction = {**record.extraction, "review_status": "approved"}
    await db.commit()
    await db.refresh(record)
    background_tasks.add_task(notify_approved, session_factory, request.app.state.settings, record.id)
    return await _detail(db, record)


@router.post(
    "/{invoice_id}/reprocess",
    response_model=InvoiceDetail,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Read an invoice again",
    description=(
        "Run OCR and extraction again on the stored pages, in the background (the invoice shows "
        "`processing` until it finishes; poll this invoice). Replaces earlier values, including "
        "corrections (their history is kept). Billed like an extraction."
    ),
    response_description="Reprocessing started.",
    responses={
        202: _json({**_DETAIL_EXAMPLE, "status": "processing", "confidence": None}, "Reprocessing started."),
        401: NOT_SIGNED_IN, 402: error_response(
            "The monthly allowance is used up.",
            {"requests": ("Request quota used up", "quota_exceeded_error", "Monthly request quota of 5000 reached; it resets on 2026-10-01", None)},
        ),
        403: SESSION_FORBIDDEN, 404: _NOT_FOUND, 409: _BUSY, 422: invalid_id_response("invoice_id"),
        503: error_response(
            "The serving model cannot read invoices.",
            {"no_document_model": ("No document model", "service_unavailable", "Invoice extraction needs PaddleOCR-VL; the serving model is 'mock-vision-1'", None)},
        ),
    },
)
async def reprocess_invoice(
    invoice_id: InvoiceId,
    auth: Session,
    db: Db,
    request: Request,
    background_tasks: BackgroundTasks,
    service: Annotated[VisionService, Depends(get_vision_service)],
    mapper: Annotated[Any, Depends(get_invoice_mapper)],
    fallback: Annotated[Any, Depends(get_invoice_fallback)],
    store: Annotated[DocumentStore, Depends(get_document_store)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> InvoiceDetail:
    record = await _own(db, auth.user.id, invoice_id)
    _not_busy(record)
    if not await asyncio.to_thread(store.has_files, record.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="The document files were deleted (retention policy); upload it again"
        )
    try:
        InvoiceExtractor(service.model, mapper)
    except ExtractionUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    try:
        plan = await check_quota(db, auth.user)
    except QuotaExceeded as exc:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc)) from exc
    usage = (
        UsageContext(
            user_id=auth.user.id, api_key_id=record.api_key_id, request_id=request_id_ctx.get() or record.id,
            model=service.model.name, image_count=record.pages, pricing=PlanPricing.from_plan(plan),
        )
        if record.api_key_id
        else None  # the key was deleted: nothing to attribute the usage to
    )
    record.status, record.confidence, record.error_message = "processing", None, None
    record.approved_by = record.approved_at = None
    await db.commit()
    await db.refresh(record)
    background_tasks.add_task(
        reprocess, session_factory, store, record.id, service.model, mapper, request.app.state.settings, usage, fallback
    )
    return await _detail(db, record)


@router.delete(
    "/{invoice_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an invoice",
    description=(
        "Permanently delete the invoice: its record, corrections, the uploaded file and page images. "
        "Usage records (billing history) are kept."
    ),
    response_description="Deleted.",
    responses={401: NOT_SIGNED_IN, 403: SESSION_FORBIDDEN, 404: _NOT_FOUND, 409: _BUSY, 422: invalid_id_response("invoice_id")},
)
async def delete_invoice(
    invoice_id: InvoiceId, auth: Session, db: Db, store: Annotated[DocumentStore, Depends(get_document_store)]
) -> Response:
    record = await _own(db, auth.user.id, invoice_id)
    _not_busy(record)
    # Explicit, not only ON DELETE CASCADE: SQLite enforces foreign keys only when asked to.
    await db.execute(delete(InvoiceCorrection).where(InvoiceCorrection.invoice_id == record.id))
    await db.delete(record)
    await db.commit()
    await asyncio.to_thread(store.delete, record.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

