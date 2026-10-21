"""Phase 4: confidence, routing, storage, corrections and the review workflow."""

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.routes.invoices import get_invoice_mapper
from app.core.config import Settings
from app.schemas.invoice import Extracted, Invoice
from app.services.document_store import DocumentStore
from app.services.invoice_confidence import score_invoice
from app.services.invoice_extraction import map_invoice
from app.services.invoice_service import CorrectionError, apply_corrections
from app.services.invoice_validation import validate_invoice
from conftest import ClientFactory
from test_dashboard import Account, sign_in
from test_invoice_extraction import FOOTER, HEADER, TABLE, FakePaddle, page_png
from test_invoice_llm import FakeMapper

pytestmark = pytest.mark.anyio

TODAY = dt.date(2026, 9, 26)
INVOICES = "/v1/dashboard/invoices"


def routed(invoice: Invoice) -> tuple[Invoice, float, str]:
    checked, report = validate_invoice(invoice, "\n".join([HEADER, TABLE, FOOTER]), today=TODAY)
    return score_invoice(checked, report, auto_approve=0.9, review=0.7)


def confirmed(invoice: Invoice) -> Invoice:
    """As if the reasoning model agreed with every present field."""
    updates = {
        name: getattr(invoice, name).model_copy(update={"note": "confirmed by tiny-llm"})
        for name in Invoice.model_fields
        if name != "items" and getattr(invoice, name).value is not None and getattr(invoice, name).status == "ok"
    }
    return invoice.model_copy(update=updates)


# ===========================================================================
# Confidence and routing
# ===========================================================================


def test_rules_only_goes_to_review() -> None:
    invoice, _ = map_invoice(HEADER, [TABLE], FOOTER)
    scored, confidence, route = routed(invoice)
    assert route == "needs_review" and confidence == 0.8
    assert scored.invoice_number.confidence == 0.8  # nothing vouches for it without a second reader
    assert scored.total_amount.confidence == 0.85  # the grand-total check passed (no amount in words here)
    assert scored.place_of_supply.confidence == 0.75  # derived, marked check
    assert scored.delivery_note.confidence == 0.8 and scored.cgst.confidence is None  # no value, no score
    assert scored.items[0].taxable_value.confidence == 0.85  # line arithmetic passed


def test_confirmed_and_consistent_is_approved() -> None:
    invoice, _ = map_invoice(HEADER, [TABLE], FOOTER)
    scored, confidence, route = routed(confirmed(invoice))
    assert route == "approved" and confidence >= 0.9
    assert scored.total_amount.confidence == 0.95  # read, confirmed, grand total passed


def test_inconsistent_totals_are_sent_for_reprocessing() -> None:
    invoice, _ = map_invoice(HEADER, [TABLE], FOOTER)
    wrong = confirmed(invoice).model_copy(update={"total_amount": Extracted.found(Decimal("2097.20"))})
    scored, confidence, route = routed(wrong)
    assert route == "reprocess" and confidence == 0.5
    assert scored.total_amount.confidence == 0.5


def test_flagged_items_and_missing_fields_need_review() -> None:
    invoice, _ = map_invoice(HEADER, [TABLE], FOOTER)
    flagged_item = invoice.items[0].model_copy(update={"description": Extracted.check("COTTON SHIRT", "rows misaligned")})
    _, confidence, route = routed(confirmed(invoice).model_copy(update={"items": [flagged_item, invoice.items[1]]}))
    assert route == "needs_review" and confidence == 0.75
    _, confidence, route = routed(confirmed(invoice).model_copy(update={"invoice_number": Extracted()}))
    assert route == "needs_review" and confidence == 0.75


def test_reviewer_corrections_score_full_confidence() -> None:
    invoice, _ = map_invoice(HEADER, [TABLE], FOOTER)
    corrected, _ = apply_corrections(invoice, {"invoice_number": "AB/7/26-27X"}, "r@example.com")
    scored, _, _ = routed(corrected)
    assert (scored.invoice_number.value, scored.invoice_number.confidence) == ("AB/7/26-27X", 1.0)


# ===========================================================================
# Corrections
# ===========================================================================


def test_corrections_are_typed_and_logged() -> None:
    invoice, _ = map_invoice(HEADER, [TABLE], FOOTER)
    corrected, changed = apply_corrections(
        invoice,
        {
            "total_amount": "₹ 2,080.00",
            "invoice_date": "04-08-2026",
            "buyer_gstin": " 27pqrst6789k1zw ",  # same value, different spelling: no change
            "items.1.rate": "1,250",
            "delivery_note": "",
        },
        "r@example.com",
    )
    assert corrected.total_amount.value == Decimal("2080.00") and corrected.total_amount.note == "corrected by r@example.com"
    assert corrected.invoice_date.value == dt.date(2026, 8, 4)
    assert corrected.items[1].rate.value == Decimal("1250")
    assert (corrected.delivery_note.value, corrected.delivery_note.status) == (None, "missing")
    assert [path for path, *_ in changed] == ["total_amount", "invoice_date", "items.1.rate", "delivery_note"]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"colour": "red"}, "unknown field 'colour'"),
        ({"items.9.rate": "1"}, "there is no line item 9"),
        ({"items.0.colour": "1"}, "unknown field"),
        ({"total_amount": "lots"}, "not a number"),
        ({"invoice_date": "someday"}, "not a date"),
        ({"items.0.line_number": "1a"}, "not a whole number"),
    ],
)
def test_bad_corrections_are_rejected(changes: dict[str, str], message: str) -> None:
    invoice, _ = map_invoice(HEADER, [TABLE], FOOTER)
    with pytest.raises(CorrectionError, match=message):
        apply_corrections(invoice, changes, "r@example.com")


# ===========================================================================
# Storage
# ===========================================================================


def test_document_store(tmp_path: Path) -> None:
    store = DocumentStore(tmp_path)
    invoice_id = "5b0e7c1a-9d2f-4e3b-8a6c-1f2e3d4c5b6a"
    store.save(invoice_id, b"%PDF-1.7 original", "application/pdf", [b"page one", b"page two"])
    assert (tmp_path / invoice_id / "original.pdf").read_bytes() == b"%PDF-1.7 original"
    assert store.pages(invoice_id) == [b"page one", b"page two"]
    assert store.page_path(invoice_id, 2) is not None and store.page_path(invoice_id, 3) is None
    assert store.page_path(invoice_id, 0) is None
    store.replace_pages(invoice_id, [b"new"])
    assert store.pages(invoice_id) == [b"new"]
    store.delete(invoice_id)
    assert not (tmp_path / invoice_id).exists()
    for bad in ("../../etc", "5B0E7C1A-9D2F-4E3B-8A6C-1F2E3D4C5B6A", "x"):
        with pytest.raises(ValueError):
            store.page_path(bad, 1)


# ===========================================================================
# Review workflow over HTTP
# ===========================================================================


async def upload_invoice(account: Account, client: httpx.AsyncClient) -> dict[str, Any]:
    secret = (await account.create_key())["secret"]
    response = await client.post(
        "/v1/invoices/extract", headers={"Authorization": f"Bearer {secret}"},
        files=[("file", ("aug.png", page_png(), "image/png"))],
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_review_workflow(
    settings: Settings, make_client: ClientFactory, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with make_client(settings, model=FakePaddle()) as client, make_client(settings, model=FakePaddle()) as other_client:
        alice = await sign_in(client, db_sessionmaker, "alice@example.com")
        extracted = await upload_invoice(alice, client)
        invoice_id = extracted["id"]

        listing = (await client.get(INVOICES)).json()
        assert listing["total"] == 1 and listing["counts"]["needs_review"] == 1
        [row] = listing["items"]
        assert (row["id"], row["status"], row["invoice_number"], row["filename"]) == (invoice_id, "needs_review", "AB/7/26-27", "aug.png")
        assert (await client.get(INVOICES, params={"status": "approved"})).json()["items"] == []

        page = await client.get(f"{INVOICES}/{invoice_id}/pages/1")
        assert page.status_code == 200 and page.headers["content-type"] == "image/png" and page.content.startswith(b"\x89PNG")
        assert "private" in page.headers["cache-control"]
        assert (await client.get(f"{INVOICES}/{invoice_id}/pages/2")).status_code == 404

        # Someone else's invoice is indistinguishable from a missing one.
        bob = await sign_in(other_client, db_sessionmaker, "bob@example.com")
        for method, path in (("GET", ""), ("GET", "/pages/1"), ("POST", "/approve"), ("DELETE", "")):
            response = await bob.client.request(method, f"{INVOICES}/{invoice_id}{path}", headers=bob.csrf)
            assert response.status_code == 404, (method, path)

        # A correction that breaks the arithmetic is flagged immediately.
        patch = await client.patch(f"{INVOICES}/{invoice_id}", headers=alice.csrf, json={"fields": {"total_amount": "2,100.00"}})
        assert patch.status_code == 200, patch.text
        detail = patch.json()
        total = detail["extraction"]["invoice"]["total_amount"]
        assert (total["value"], total["status"]) == ("2100.00", "check")
        assert "corrected by alice@example.com" in total["note"] and "total reads 2,100.00" in total["note"]
        assert detail["extraction"]["validation"]["status"] == "failed" and detail["status"] == "needs_review"
        assert detail["corrections"][0] | {"created_at": None} == {
            "field": "total_amount", "old_value": "2079.20", "new_value": "2100.00", "old_status": "ok",
            "corrected_by": "alice@example.com", "created_at": None,
        }

        # Putting it right clears the flag: every change is validated from the original reading.
        detail = (await client.patch(f"{INVOICES}/{invoice_id}", headers=alice.csrf, json={"fields": {"total_amount": "2079.20"}})).json()
        total = detail["extraction"]["invoice"]["total_amount"]
        assert (total["status"], total["confidence"]) == ("ok", 1.0) and detail["extraction"]["validation"]["status"] == "passed"
        assert len(detail["corrections"]) == 2

        bad = await client.patch(f"{INVOICES}/{invoice_id}", headers=alice.csrf, json={"fields": {"colour": "red"}})
        assert bad.status_code == 400 and "unknown field" in bad.json()["error"]["message"]
        assert (await client.patch(f"{INVOICES}/{invoice_id}", json={"fields": {"total_amount": "1"}})).status_code == 403  # CSRF

        approved = (await client.post(f"{INVOICES}/{invoice_id}/approve", headers=alice.csrf)).json()
        assert approved["status"] == "approved" and approved["approved_automatically"] is False and approved["approved_at"]

        # Reprocessing reads the stored pages again (in the background; done when the response returns here).
        started = await client.post(f"{INVOICES}/{invoice_id}/reprocess", headers=alice.csrf)
        assert started.status_code == 202 and started.json()["status"] == "processing"
        again = (await client.get(f"{INVOICES}/{invoice_id}")).json()
        assert again["status"] == "needs_review" and again["extraction"]["invoice"]["total_amount"]["value"] == "2079.20"
        assert len(again["corrections"]) == 2  # history kept

        deleted = await client.delete(f"{INVOICES}/{invoice_id}", headers=alice.csrf)
        assert deleted.status_code == 204
        assert (await client.get(f"{INVOICES}/{invoice_id}")).status_code == 404
        assert not (Path(settings.document_storage_dir) / invoice_id).exists()


async def test_confident_invoices_are_approved_automatically(
    settings: Settings, make_client: ClientFactory, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with make_client(settings, model=FakePaddle(), overrides={get_invoice_mapper: lambda: FakeMapper()}) as client:
        alice = await sign_in(client, db_sessionmaker, "alice@example.com")
        extracted = await upload_invoice(alice, client)
        detail = (await client.get(f"{INVOICES}/{extracted['id']}")).json()
    assert extracted["review_status"] == detail["status"] == "approved"
    assert detail["approved_automatically"] is True


async def test_reprocess_needs_a_document_model(
    settings: Settings, make_client: ClientFactory, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with make_client(settings, model=FakePaddle()) as client:
        alice = await sign_in(client, db_sessionmaker, "alice@example.com")
        invoice_id = (await upload_invoice(alice, client))["id"]
    async with make_client(settings) as client:  # restarted with the mock model
        client.cookies = alice.client.cookies
        response = await client.post(f"{INVOICES}/{invoice_id}/reprocess", headers=alice.csrf)
    assert response.status_code == 503
