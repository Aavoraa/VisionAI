"""Phase 5: approval webhook, file retention and restart recovery for stored invoices."""

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import logging

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.database.models import InvoiceRecord
from app.services.document_store import DocumentStore
from app.services.invoice_export import clean_record

logger = logging.getLogger(__name__)

WEBHOOK_RETRY_DELAYS = (0.0, 2.0, 10.0)  # seconds before each attempt
RETENTION_INTERVAL_SECONDS = 6 * 3600


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def notify_approved(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    record_id: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    retry_delays: tuple[float, ...] = WEBHOOK_RETRY_DELAYS,
) -> bool:
    """POST ``invoice.approved`` to the configured webhook. Never raises; True when delivered.

    The body is signed: ``X-Vision-Signature: sha256=<hex HMAC-SHA256 of the raw body>``.
    Receivers should verify it and use ``X-Vision-Delivery`` to ignore duplicates (a retry after
    a lost response delivers the same event again).
    """
    if not settings.invoice_webhook_url:
        return False
    async with session_factory() as db:
        record = await db.get(InvoiceRecord, record_id)
        if record is None or record.status != "approved":
            return False
        payload = {"event": "invoice.approved", "sent_at": dt.datetime.now(dt.UTC).isoformat(), "invoice": clean_record(record)}
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    headers = {"Content-Type": "application/json", "X-Vision-Event": "invoice.approved", "X-Vision-Delivery": record_id}
    if settings.invoice_webhook_secret:
        headers["X-Vision-Signature"] = sign(body, settings.invoice_webhook_secret.get_secret_value())
    async with httpx.AsyncClient(timeout=10.0, transport=transport, follow_redirects=False) as client:
        for attempt, delay in enumerate(retry_delays, 1):
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await client.post(settings.invoice_webhook_url, content=body, headers=headers)
            except httpx.HTTPError as exc:
                logger.warning("invoice webhook failed", extra={"invoice_id": record_id, "attempt": attempt, "error": str(exc)})
                continue
            if 200 <= response.status_code < 300:
                logger.info("invoice webhook delivered", extra={"invoice_id": record_id, "attempt": attempt})
                return True
            logger.warning(
                "invoice webhook rejected", extra={"invoice_id": record_id, "attempt": attempt, "status": response.status_code}
            )
    logger.error("invoice webhook gave up", extra={"invoice_id": record_id})
    return False


# ---------------------------------------------------------------------------
# Retention and recovery
# ---------------------------------------------------------------------------


async def purge_expired_files(
    session_factory: async_sessionmaker[AsyncSession],
    store: DocumentStore,
    days: int,
    *,
    now: dt.datetime | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Delete the files of invoices uploaded more than ``days`` ago (the records stay).

    Returns the ids whose files were (or, with ``dry_run``, would be) deleted.
    """
    cutoff = (now or dt.datetime.now(dt.UTC)) - dt.timedelta(days=days)
    async with session_factory() as db:
        ids = (
            await db.execute(
                select(InvoiceRecord.id).where(InvoiceRecord.created_at < cutoff, InvoiceRecord.status != "processing")
            )
        ).scalars().all()
    expired = [invoice_id for invoice_id in ids if await asyncio.to_thread(store.has_files, invoice_id)]
    if not dry_run:
        for invoice_id in expired:
            await asyncio.to_thread(store.delete, invoice_id)
    if expired:
        logger.info("invoice files purged", extra={"count": len(expired), "days": days, "dry_run": dry_run})
    return expired


async def retention_loop(session_factory: async_sessionmaker[AsyncSession], store: DocumentStore, days: int) -> None:
    """Purge now and then every few hours, until cancelled (app shutdown)."""
    while True:
        try:
            await purge_expired_files(session_factory, store, days)
        except Exception:  # keep the loop alive; the next round retries
            logger.exception("invoice retention run failed")
        await asyncio.sleep(RETENTION_INTERVAL_SECONDS)


async def recover_interrupted(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """At startup: invoices left ``processing`` by a restart can never finish; mark them failed."""
    async with session_factory() as db:
        result = await db.execute(
            update(InvoiceRecord)
            .where(InvoiceRecord.status == "processing")
            .values(status="failed", error_message="Interrupted by a server restart; reprocess it")
        )
        await db.commit()
    count = result.rowcount or 0  # type: ignore[attr-defined]
    if count:
        logger.warning("interrupted invoices marked failed", extra={"count": count})
    return count
