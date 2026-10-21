"""Phase 4: stored invoices - extraction, confidence routing, reviewer corrections, reprocessing."""

import asyncio
import datetime as dt
import logging
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.invoice_progress import ProgressFn, silent
from app.core.config import Settings
from app.database.models import InvoiceCorrection, InvoiceRecord
from app.models.base import ModelError, VisionModel
from app.schemas.invoice import Extracted, Invoice, InvoiceExtraction, LineItem
from app.services.document_store import DocumentStore
from app.services.invoice_confidence import score_invoice
from app.services.invoice_fallback import FallbackReader
from app.services.invoice_extraction import InvoiceExtractor, ZoomOptions, parse_date, validate_extraction
from app.services.invoice_llm import LlmFieldMapper
from app.services.usage_service import UsageContext, record_usage

logger = logging.getLogger(__name__)

DECIMAL_FIELDS = {"total_quantity", "subtotal", "cgst", "sgst", "igst", "discount", "round_off", "total_amount"}
ITEM_DECIMAL_FIELDS = {"quantity", "rate", "discount_percent", "taxable_value", "tax_rate", "tax_amount", "total"}
_ITEM_PATH = re.compile(r"items\.(\d{1,4})\.([a-z_]+)")


class CorrectionError(ValueError):
    """A correction names an unknown field or has a value of the wrong type."""


def finalize(result: InvoiceExtraction, settings: Settings, invoice_id: str | None, base: Invoice | None = None) -> InvoiceExtraction:
    """Validate ``base`` (default: the result's invoice), score it and decide the route."""
    validated = validate_extraction(result, base)
    scored, confidence, route = score_invoice(
        validated.invoice,
        validated.validation,
        auto_approve=settings.invoice_auto_approve_confidence,
        review=settings.invoice_review_confidence,
    )
    return validated.model_copy(update={"invoice": scored, "confidence": confidence, "review_status": route, "id": invoice_id})


def store_result(record: InvoiceRecord, result: InvoiceExtraction, base: Invoice) -> None:
    """Copy a finalised result onto the record (status, confidence, listing fields)."""
    invoice = result.invoice
    record.extraction = result.model_dump(mode="json")
    record.base_invoice = base.model_dump(mode="json")
    record.status = result.review_status or "needs_review"
    record.confidence = result.confidence
    record.pages = result.pages
    record.invoice_number = str(invoice.invoice_number.value)[:64] if invoice.invoice_number.value else None
    record.vendor_name = str(invoice.vendor_name.value)[:255] if invoice.vendor_name.value else None
    record.total_amount = str(invoice.total_amount.value) if invoice.total_amount.value is not None else None
    record.error_message = None
    if record.status == "approved":
        record.approved_by = None  # approved automatically
        record.approved_at = dt.datetime.now(dt.UTC)
    else:
        record.approved_by = record.approved_at = None


async def extract_and_store(
    session_factory: async_sessionmaker[AsyncSession],
    record_id: str,
    pages: list[bytes],
    model: VisionModel,
    mapper: LlmFieldMapper | None,
    settings: Settings,
    usage: UsageContext | None,
    progress: ProgressFn | None = None,
    fallback: "FallbackReader | None" = None,
) -> InvoiceExtraction:
    """Run extraction for a stored invoice and save the outcome (``failed`` on errors, then re-raise).

    ``progress`` receives live comments (see ``invoice_progress``); the last one has ``done``.
    """
    say = progress or silent
    started = time.perf_counter()
    try:
        async with asyncio.timeout(settings.invoice_timeout_seconds):
            extractor = InvoiceExtractor(model, mapper, ZoomOptions.from_settings(settings), progress=say, fallback=fallback)
            read = await extractor.read(pages)
    except Exception as exc:
        if isinstance(exc, TimeoutError):
            message = "Model inference timed out"
        elif isinstance(exc, ModelError):
            message = "Model inference failed"
        else:  # a bug in the pipeline: never leave the record stuck in "processing"
            message = "Extraction failed (internal error)"
            logger.exception("invoice extraction crashed", extra={"invoice_id": record_id})
        say("result", f"Stopped: {message.lower()}", level="warn", done=True)
        async with session_factory() as db:
            record = await db.get(InvoiceRecord, record_id)
            if record is not None:
                record.status, record.error_message = "failed", message
                await db.commit()
        if usage is not None:
            await record_usage(session_factory, usage, status="error", error_message=message,
                               processing_time_ms=int((time.perf_counter() - started) * 1000))
        raise
    finalized = time.perf_counter()
    result = finalize(read, settings, record_id)
    if settings.invoice_profile_log:
        profile = extractor.profile
        profile["stages_ms"]["validation_and_scoring"] = round((time.perf_counter() - finalized) * 1000)
        logger.info("invoice profile", extra={"invoice_id": record_id, "pages": len(pages), **profile})
    if result.validation is not None:
        checks = result.validation.checks
        failed = [c.name for c in checks if c.status == "fail"]
        say("validate", f"Checks: {sum(c.status == 'pass' for c in checks)} passed, {len(failed)} failed"
            + (f" ({', '.join(failed)})" if failed else ""), level="warn" if failed else "ok")
    route = {"approved": "approved automatically", "needs_review": "needs review", "reprocess": "reprocess"}.get(
        result.review_status or "", result.review_status or "done")
    confidence = f" (confidence {result.confidence:.0%})" if result.confidence is not None else ""
    say("result", f"Done: {route}{confidence}", level="ok" if result.review_status == "approved" else "info", done=True)
    async with session_factory() as db:
        record = await db.get(InvoiceRecord, record_id)
        if record is not None:
            store_result(record, result, read.invoice)
            await db.commit()
    if usage is not None:
        await record_usage(session_factory, usage, status="success", input_tokens=result.usage.input_tokens,
                           output_tokens=result.usage.output_tokens,
                           processing_time_ms=int((time.perf_counter() - started) * 1000))
    return result


async def reprocess(
    session_factory: async_sessionmaker[AsyncSession],
    store: DocumentStore,
    record_id: str,
    model: VisionModel,
    mapper: LlmFieldMapper | None,
    settings: Settings,
    usage: UsageContext | None,
    fallback: "FallbackReader | None" = None,
) -> None:
    """Background task: read the stored pages again. Never raises (the outcome is stored)."""
    try:
        pages = await asyncio.to_thread(store.pages, record_id)
        if not pages:
            raise ModelError("the stored pages are missing")
        result = await extract_and_store(session_factory, record_id, pages, model, mapper, settings, usage, fallback=fallback)
        if result.review_status == "approved":
            from app.services.invoice_integration import notify_approved  # integration imports this module's peers

            await notify_approved(session_factory, settings, record_id)
    except Exception as exc:  # background context: record the failure, never crash the worker
        logger.warning("invoice reprocessing failed", extra={"invoice_id": record_id, "error": str(exc)})
        async with session_factory() as db:
            record = await db.get(InvoiceRecord, record_id)
            if record is not None and record.status == "processing":
                record.status, record.error_message = "failed", "Reprocessing failed"
                await db.commit()


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


def _parse(name: str, raw: str | None) -> Any:
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    if name in DECIMAL_FIELDS or name in ITEM_DECIMAL_FIELDS:
        try:
            return Decimal(text.replace(",", "").replace("₹", "").strip())
        except InvalidOperation as exc:
            raise CorrectionError(f"{name}: {raw!r} is not a number") from exc
    if name == "invoice_date":
        date = parse_date(text)
        if date is None:
            raise CorrectionError(f"invoice_date: {raw!r} is not a date (use YYYY-MM-DD or DD-MM-YYYY)")
        return date
    if name == "line_number":
        if not text.isdigit():
            raise CorrectionError(f"line_number: {raw!r} is not a whole number")
        return int(text)
    if name in ("vendor_gstin", "buyer_gstin"):
        return text.replace(" ", "").upper()
    return " ".join(text.split())


def _corrected(field: Extracted[Any], value: Any, reviewer: str) -> Extracted[Any]:
    if value is None:
        return Extracted(value=None, status="missing", source_text=field.source_text, note=f"cleared by {reviewer}")
    return Extracted(value=value, status="ok", source_text=field.source_text, note=f"corrected by {reviewer}")


def apply_corrections(
    base: Invoice, changes: dict[str, str | None], reviewer: str
) -> tuple[Invoice, list[tuple[str, Any, Any, str]]]:
    """Apply ``{"vendor_name": "...", "items.3.rate": "320.00"}``; returns (invoice, changed fields).

    Unchanged values are ignored. Each change is (path, old value, new value, old status).
    """
    updates: dict[str, Any] = {}
    items: list[LineItem] = list(base.items)
    changed: list[tuple[str, Any, Any, str]] = []
    for path, raw in changes.items():
        if path in Invoice.model_fields and path != "items":
            field: Extracted[Any] = getattr(base, path)
            value = _parse(path, raw)
            if value != field.value:
                updates[path] = _corrected(field, value, reviewer)
                changed.append((path, field.value, value, field.status))
            continue
        match = _ITEM_PATH.fullmatch(path)
        if not match or match.group(2) not in LineItem.model_fields:
            raise CorrectionError(f"unknown field {path!r}")
        index, name = int(match.group(1)), match.group(2)
        if index >= len(items):
            raise CorrectionError(f"{path}: there is no line item {index}")
        cell: Extracted[Any] = getattr(items[index], name)
        value = _parse(name, raw)
        if value != cell.value:
            items[index] = items[index].model_copy(update={name: _corrected(cell, value, reviewer)})
            changed.append((path, cell.value, value, cell.status))
    updates["items"] = items
    return base.model_copy(update=updates), changed


def correction_rows(invoice_id: str, user_id: str, changed: list[tuple[str, Any, Any, str]]) -> list[InvoiceCorrection]:
    return [
        InvoiceCorrection(
            invoice_id=invoice_id, user_id=user_id, field=path,
            old_value=None if old is None else str(old), new_value=None if new is None else str(new), old_status=status,
        )
        for path, old, new, status in changed
    ]
