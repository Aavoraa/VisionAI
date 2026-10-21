"""Phase 5: clean export format, CSV, webhook, retention, restart recovery, async mode, ERP API."""

import csv
import datetime as dt
import hashlib
import hmac
import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.routes import dashboard_invoices, invoices
from app.api.routes.invoices import get_invoice_mapper
from app.core.config import Settings
from app.database.models import InvoiceRecord, User
from app.services.document_store import DocumentStore
from app.services.invoice_export import clean_record, invoices_csv, items_csv
from app.services.invoice_extraction import map_invoice
from app.services.invoice_integration import notify_approved, purge_expired_files, recover_interrupted, sign
from app.services.invoice_service import finalize
from app.schemas.invoice import InvoiceExtraction
from conftest import ClientFactory
from test_dashboard import sign_in
from test_invoice_extraction import BUYER_GSTIN, FOOTER, HEADER, TABLE, FakePaddle, page_png
from test_invoice_llm import FakeMapper

pytestmark = pytest.mark.anyio

NOW = dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.UTC)


async def stored_invoice(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, *, status: str | None = None, created: dt.datetime = NOW
) -> InvoiceRecord:
    """A stored invoice from the fixture text (its own user), optionally forcing its status."""
    invoice, _ = map_invoice(HEADER, [TABLE], FOOTER)
    raw = f"--- header OCR (page 1, top 35%) ---\n{HEADER}\n\n--- table (page 1) ---\n{TABLE}\n\n--- footer OCR (page 1, bottom 15%) ---\n{FOOTER}"
    base = InvoiceExtraction(invoice=invoice, model="PaddleOCR-VL-test", pages=1, raw_text=raw, processing_ms=1)
    async with sessionmaker() as db:
        user = User(email=f"owner-{created.timestamp()}-{status}@example.com")
        db.add(user)
        await db.flush()
        record = InvoiceRecord(user_id=user.id, filename="aug.png", content_type="image/png", size_bytes=10, pages=1,
                               status="processing", created_at=created, updated_at=created)
        db.add(record)
        await db.flush()
        from app.services.invoice_service import store_result

        store_result(record, finalize(base, settings, record.id), invoice)
        if status:
            record.status = status
        await db.commit()
        return record


# ===========================================================================
# Clean format and CSV
# ===========================================================================


async def test_clean_record_has_plain_values_and_flags(
    settings: Settings, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    record = await stored_invoice(db_sessionmaker, settings)
    clean = clean_record(record)
    assert clean["status"] == "needs_review" and clean["validation"] == "passed"
    data = clean["data"]
    assert (data["invoice_number"], data["invoice_date"], data["total_amount"], data["buyer_gstin"]) == (
        "AB/7/26-27", "2026-08-03", "2079.20", BUYER_GSTIN,
    )
    assert data["items"][0] == {
        "line_number": 1, "description": "COTTON SHIRT", "design": None, "size": None, "hsn_sac": "6205", "quantity": "2", "unit": "pcs",
        "rate": "500.00", "mrp": None, "discount_percent": "10", "taxable_value": "900.00", "tax_rate": None, "tax_amount": None, "total": None,
    }
    assert clean["flags"] == ["place_of_supply"]
    json.dumps(clean)  # plain JSON all the way down


async def test_csv_exports(settings: Settings, db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    record = await stored_invoice(db_sessionmaker, settings)
    rows = list(csv.DictReader(io.StringIO(invoices_csv([record]))))
    assert len(rows) == 1
    assert (rows[0]["invoice_number"], rows[0]["total_amount"], rows[0]["flags"]) == ("AB/7/26-27", "2079.20", "place_of_supply")
    items = list(csv.DictReader(io.StringIO(items_csv([record]))))
    assert [i["description"] for i in items] == ["COTTON SHIRT", "DENIM - 32 JEANS"]
    assert items[1]["invoice_number"] == "AB/7/26-27" and items[1]["taxable_value"] == "1080.00"


async def test_csv_cells_cannot_become_spreadsheet_formulas(
    settings: Settings, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    record = await stored_invoice(db_sessionmaker, settings)
    extraction = dict(record.extraction or {})
    extraction["invoice"] = {**extraction["invoice"], "vendor_name": {**extraction["invoice"]["vendor_name"], "value": "=HYPERLINK(\"x\")"},
                             "round_off": {**extraction["invoice"]["round_off"], "value": "-0.20"}}
    record.extraction = extraction
    row = next(csv.DictReader(io.StringIO(invoices_csv([record]))))
    assert row["vendor_name"] == "'=HYPERLINK(\"x\")"
    assert row["round_off"] == "-0.20"  # negative numbers stay numbers


# ===========================================================================
# Webhook
# ===========================================================================


def receiver(calls: list[httpx.Request], statuses: list[int]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(statuses[min(len(calls), len(statuses)) - 1])

    return httpx.MockTransport(handler)


async def test_webhook_posts_a_signed_approval(settings: Settings, db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    record = await stored_invoice(db_sessionmaker, settings, status="approved")
    configured = settings.model_copy(update={"invoice_webhook_url": "https://erp.example/hooks/invoices",
                                             "invoice_webhook_secret": SecretStr("s3cret")})
    calls: list[httpx.Request] = []
    delivered = await notify_approved(db_sessionmaker, configured, record.id, transport=receiver(calls, [503, 200]), retry_delays=(0, 0, 0))
    assert delivered and len(calls) == 2  # retried after the 503
    request = calls[-1]
    expected = "sha256=" + hmac.new(b"s3cret", request.content, hashlib.sha256).hexdigest()
    assert request.headers["X-Vision-Signature"] == expected == sign(request.content, "s3cret")
    assert request.headers["X-Vision-Delivery"] == record.id and request.headers["X-Vision-Event"] == "invoice.approved"
    body = json.loads(request.content)
    assert body["event"] == "invoice.approved" and body["invoice"]["data"]["invoice_number"] == "AB/7/26-27"


async def test_webhook_gives_up_quietly(settings: Settings, db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    record = await stored_invoice(db_sessionmaker, settings, status="approved")
    configured = settings.model_copy(update={"invoice_webhook_url": "https://erp.example/hooks"})
    calls: list[httpx.Request] = []
    assert not await notify_approved(db_sessionmaker, configured, record.id, transport=receiver(calls, [500]), retry_delays=(0, 0, 0))
    assert len(calls) == 3 and "X-Vision-Signature" not in calls[0].headers  # no secret configured: unsigned


async def test_webhook_only_for_approved_invoices_when_configured(
    settings: Settings, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    pending = await stored_invoice(db_sessionmaker, settings)
    calls: list[httpx.Request] = []
    assert not await notify_approved(db_sessionmaker, settings, pending.id, transport=receiver(calls, [200]))  # no URL
    configured = settings.model_copy(update={"invoice_webhook_url": "https://erp.example/hooks"})
    assert not await notify_approved(db_sessionmaker, configured, pending.id, transport=receiver(calls, [200]))  # not approved
    assert calls == []


# ===========================================================================
# Retention and recovery
# ===========================================================================


async def test_retention_deletes_old_files_but_keeps_the_data(
    settings: Settings, db_sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    store = DocumentStore(tmp_path)
    old = await stored_invoice(db_sessionmaker, settings, created=NOW - dt.timedelta(days=100))
    recent = await stored_invoice(db_sessionmaker, settings, created=NOW - dt.timedelta(days=10))
    busy = await stored_invoice(db_sessionmaker, settings, status="processing", created=NOW - dt.timedelta(days=100))
    for record in (old, recent, busy):
        store.save(record.id, b"original", "image/png", [b"page"])

    assert await purge_expired_files(db_sessionmaker, store, 90, now=NOW, dry_run=True) == [old.id]
    assert store.has_files(old.id)  # dry run changed nothing
    assert await purge_expired_files(db_sessionmaker, store, 90, now=NOW) == [old.id]
    assert not store.has_files(old.id) and store.has_files(recent.id) and store.has_files(busy.id)
    assert await purge_expired_files(db_sessionmaker, store, 90, now=NOW) == []  # idempotent
    async with db_sessionmaker() as db:
        assert (await db.get(InvoiceRecord, old.id)) is not None  # the extracted data stays


async def test_restart_recovery(settings: Settings, db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    stuck = await stored_invoice(db_sessionmaker, settings, status="processing")
    done = await stored_invoice(db_sessionmaker, settings)
    assert await recover_interrupted(db_sessionmaker) == 1
    async with db_sessionmaker() as db:
        stuck_now, done_now = await db.get(InvoiceRecord, stuck.id), await db.get(InvoiceRecord, done.id)
    assert stuck_now is not None and (stuck_now.status, stuck_now.error_message) == ("failed", "Interrupted by a server restart; reprocess it")
    assert done_now is not None and done_now.status == "needs_review"


# ===========================================================================
# HTTP: async mode, ERP endpoints, exports
# ===========================================================================


async def test_async_mode_and_integration_api(
    settings: Settings, make_client: ClientFactory, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with make_client(settings, model=FakePaddle()) as client, make_client(settings, model=FakePaddle()) as other:
        alice = await sign_in(client, db_sessionmaker, "alice@example.com")
        key = {"Authorization": f"Bearer {(await alice.create_key())['secret']}"}
        started = await client.post("/v1/invoices/extract", params={"mode": "async"}, headers=key,
                                    files=[("file", ("aug.png", page_png(), "image/png"))])
        assert started.status_code == 202, started.text
        invoice_id = started.json()["id"]
        assert started.headers["location"] == f"/v1/invoices/{invoice_id}" == started.json()["status_url"]

        # The background extraction has run by the time the test client returns.
        one = (await client.get(f"/v1/invoices/{invoice_id}", headers=key)).json()
        assert one["status"] == "needs_review" and one["data"]["invoice_number"] == "AB/7/26-27"
        assert one["flags"] == ["place_of_supply"] and one["validation"] == "passed"

        listing = (await client.get("/v1/invoices", headers=key, params={"status": "needs_review"})).json()
        assert [i["id"] for i in listing["items"]] == [invoice_id] and listing["total"] == 1
        later = (NOW + dt.timedelta(days=3650)).isoformat()
        assert (await client.get("/v1/invoices", headers=key, params={"updated_since": later})).json()["total"] == 0
        assert (await client.get("/v1/invoices", headers=key, params={"status": "approved"})).json()["items"] == []

        bob = await sign_in(other, db_sessionmaker, "bob@example.com")
        bob_key = {"Authorization": f"Bearer {(await bob.create_key())['secret']}"}
        assert (await other.get(f"/v1/invoices/{invoice_id}", headers=bob_key)).status_code == 404
        assert (await other.get("/v1/invoices", headers=bob_key)).json()["total"] == 0
        assert (await client.get(f"/v1/invoices/{invoice_id}")).status_code == 401

        # Dashboard exports (session).
        csv_response = await client.get("/v1/dashboard/invoices/export", params={"format": "csv"})
        assert csv_response.status_code == 200 and csv_response.headers["content-type"].startswith("text/csv")
        assert 'filename="invoices-all-' in csv_response.headers["content-disposition"]
        assert csv_response.text.startswith("﻿id,status,confidence")
        items = await client.get("/v1/dashboard/invoices/export", params={"format": "items_csv", "status": "needs_review"})
        assert "COTTON SHIRT" in items.text and "-items.csv" in items.headers["content-disposition"]
        exported = (await client.get("/v1/dashboard/invoices/export", params={"format": "json"})).json()
        assert [e["id"] for e in exported] == [invoice_id]
        assert (await bob.client.get("/v1/dashboard/invoices/export", params={"format": "json"})).json() == []

        # Retention removed the files: the review page is told, and reprocessing is refused.
        DocumentStore(settings.document_storage_dir).delete(invoice_id)
        detail = (await client.get(f"/v1/dashboard/invoices/{invoice_id}")).json()
        assert detail["files_available"] is False
        refused = await client.post(f"/v1/dashboard/invoices/{invoice_id}/reprocess", headers=alice.csrf)
        assert refused.status_code == 409 and "retention" in refused.json()["error"]["message"]


async def test_webhook_is_scheduled_on_approval(
    settings: Settings, make_client: ClientFactory, db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    async def fake_notify(session_factory: Any, settings: Any, record_id: str, **kwargs: Any) -> bool:
        sent.append(record_id)
        return True

    monkeypatch.setattr(invoices, "notify_approved", fake_notify)
    monkeypatch.setattr(dashboard_invoices, "notify_approved", fake_notify)

    async with make_client(settings, model=FakePaddle()) as client:
        alice = await sign_in(client, db_sessionmaker, "alice@example.com")
        key = {"Authorization": f"Bearer {(await alice.create_key())['secret']}"}
        upload = [("file", ("aug.png", page_png(), "image/png"))]
        needs_review = (await client.post("/v1/invoices/extract", headers=key, files=upload)).json()
        assert sent == []  # not approved: nothing sent
        await client.post(f"/v1/dashboard/invoices/{needs_review['id']}/approve", headers=alice.csrf)
        assert sent == [needs_review["id"]]  # approved by a reviewer

    async with make_client(settings, model=FakePaddle(), overrides={get_invoice_mapper: lambda: FakeMapper()}) as client:
        auto = (await client.post("/v1/invoices/extract", headers=key, files=upload)).json()
    assert auto["review_status"] == "approved" and sent[-1] == auto["id"]  # approved automatically


def test_purge_script_needs_a_period(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from scripts import purge_invoice_files

    monkeypatch.setattr(purge_invoice_files, "get_settings", lambda: Settings(_env_file=None))  # type: ignore[call-arg]
    assert purge_invoice_files.main([]) == 2
    assert "give --days N" in capsys.readouterr().err

