"""POST /v1/invoices/extract."""

import asyncio
import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.routes.invoices import get_invoice_mapper
from app.core.config import Settings
from app.database.models import InvoiceRecord, UsageLog
from app.models.base import ModelOutput
from conftest import ClientFactory
from test_invoice_extraction import BUYER_GSTIN, FakePaddle, page_png, pdf_bytes
from test_invoice_llm import FakeMapper

pytestmark = pytest.mark.anyio

EXTRACT = "/v1/invoices/extract"


def upload(data: bytes, name: str = "invoice.png", mime: str = "image/png") -> dict[str, Any]:
    return {"files": [("file", (name, data, mime))]}


async def usage_rows(sessionmaker: async_sessionmaker[AsyncSession]) -> list[UsageLog]:
    async with sessionmaker() as session:
        return list((await session.execute(select(UsageLog))).scalars())


async def test_extracts_an_invoice_and_bills_the_tokens(
    settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str], db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with make_client(settings, model=FakePaddle()) as client:
        response = await client.post(EXTRACT, headers=auth_headers, **upload(page_png()))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["invoice"]["buyer_gstin"] == {
        "value": BUYER_GSTIN, "status": "ok", "source_text": f"GSTIN/UIN : {BUYER_GSTIN}", "confidence": 0.85, "note": None,
    }
    assert [i["description"]["value"] for i in body["invoice"]["items"]] == ["COTTON SHIRT", "DENIM - 32 JEANS"]
    assert body["model"] == "PaddleOCR-VL-test" and body["reasoning_model"] is None and body["pages"] == 1
    assert body["usage"] == {"input_tokens": 300, "output_tokens": 150}
    assert body["validation"]["status"] == "passed"
    assert {c["id"] for c in body["validation"]["checks"]} >= {"grand_total", "line_arithmetic", "tax_type"}
    [row] = await usage_rows(db_sessionmaker)
    assert (row.status, row.model, row.image_count, row.input_tokens, row.output_tokens) == ("success", "PaddleOCR-VL-test", 1, 300, 150)
    # Stored for review. Without a reasoning model nothing is confirmed, so nothing auto-approves.
    assert body["review_status"] == "needs_review" and 0.7 <= body["confidence"] < 0.9
    async with db_sessionmaker() as session:
        record = await session.get(InvoiceRecord, body["id"])
    assert record is not None
    assert (record.status, record.filename, record.content_type, record.pages) == ("needs_review", "invoice.png", "image/png", 1)
    assert (record.invoice_number, record.total_amount) == ("AB/7/26-27", "2079.20")
    folder = Path(settings.document_storage_dir) / body["id"]
    assert (folder / "original.png").is_file() and (folder / "page-1.png").is_file()


async def test_reasoning_model_cross_checks_when_configured(
    settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str]
) -> None:
    async with make_client(settings, model=FakePaddle(), overrides={get_invoice_mapper: lambda: FakeMapper()}) as client:
        body = (await client.post(EXTRACT, headers=auth_headers, **upload(page_png()))).json()
    assert body["reasoning_model"] == "tiny-llm"
    assert "confirmed by tiny-llm" in body["invoice"]["buyer_gstin"]["note"]
    # Every key field confirmed and every check passed: approved without a reviewer.
    assert body["review_status"] == "approved" and body["confidence"] >= 0.9


async def test_pdf_pages_are_all_read(settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str]) -> None:
    model = FakePaddle()
    async with make_client(settings, model=model) as client:
        body = (await client.post(EXTRACT, headers=auth_headers, **upload(pdf_bytes(2), "invoice.pdf", "application/pdf"))).json()
    assert body["pages"] == 2
    assert [c[0] for c in model.calls].count("Table Recognition:") == 2


async def test_needs_a_document_model(settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str]) -> None:
    async with make_client(settings) as client:  # the default mock model cannot read documents
        response = await client.post(EXTRACT, headers=auth_headers, **upload(page_png()))
    assert response.status_code == 503
    assert response.json()["error"]["message"].startswith("Invoice extraction needs PaddleOCR-VL")


@pytest.mark.parametrize(
    ("kwargs", "status", "message"),
    [
        (upload(b"MZ\x90 not an invoice", "x.exe", "application/octet-stream"), 400, "Unsupported file type"),
        (upload(pdf_bytes(1), "x.pdf", "application/pdf") | {"data": {"prompt": "hi"}}, 400, None),
        ({"files": [("images", ("a.png", page_png(), "image/png"))]}, 400, "Upload the invoice as the 'file' field"),
        ({"json": {"file": "abc"}}, 415, "Content-Type must be multipart/form-data"),
    ],
    ids=["not-a-document", "extra-field", "wrong-field", "json-body"],
)
async def test_bad_uploads(
    settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str], kwargs: dict[str, Any], status: int, message: str | None
) -> None:
    async with make_client(settings, model=FakePaddle()) as client:
        response = await client.post(EXTRACT, headers=auth_headers, **kwargs)
    assert response.status_code == status, response.text
    if message:
        assert message in response.json()["error"]["message"]


async def test_limits(settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str]) -> None:
    small = settings.model_copy(update={"max_document_bytes": 2000, "max_document_pages": 1})
    async with make_client(small, model=FakePaddle()) as client:
        big = io.BytesIO()
        Image.effect_noise((300, 300), 100).convert("RGB").save(big, format="PNG")
        assert (await client.post(EXTRACT, headers=auth_headers, **upload(big.getvalue()))).status_code == 413
    async with make_client(settings.model_copy(update={"max_document_pages": 1}), model=FakePaddle()) as client:
        response = await client.post(EXTRACT, headers=auth_headers, **upload(pdf_bytes(2), "x.pdf", "application/pdf"))
    assert response.status_code == 400 and "at most 1" in response.json()["error"]["message"]


async def test_requires_an_api_key(settings: Settings, make_client: ClientFactory) -> None:
    async with make_client(settings, model=FakePaddle()) as client:
        assert (await client.post(EXTRACT, **upload(page_png()))).status_code == 401


async def test_a_crash_marks_the_invoice_failed(
    settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str], db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    class Broken(FakePaddle):
        async def analyze(self, images: list[bytes], prompt: str, options: dict[str, Any] | None = None) -> ModelOutput:
            raise ArithmeticError("bug")

    async with make_client(settings, model=Broken()) as client:
        try:
            response = await client.post(EXTRACT, headers=auth_headers, **upload(page_png()))
            assert response.status_code == 500
        except ArithmeticError:
            pass  # the test transport may re-raise the app's exception
    async with db_sessionmaker() as session:
        [record] = (await session.execute(select(InvoiceRecord))).scalars()
    assert (record.status, record.error_message) == ("failed", "Extraction failed (internal error)")  # not stuck in processing


async def test_timeout_is_reported_and_logged(
    settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str], db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    class Slow(FakePaddle):
        async def analyze(self, images: list[bytes], prompt: str, options: dict[str, Any] | None = None) -> ModelOutput:
            await asyncio.sleep(5)
            return await super().analyze(images, prompt, options)

    async with make_client(settings.model_copy(update={"invoice_timeout_seconds": 0.05}), model=Slow()) as client:
        response = await client.post(EXTRACT, headers=auth_headers, **upload(page_png()))
    assert response.status_code == 504
    [row] = await usage_rows(db_sessionmaker)
    assert (row.status, row.cost_micros) == ("error", 0)  # failures are not billed
    async with db_sessionmaker() as session:
        [record] = (await session.execute(select(InvoiceRecord))).scalars()
    assert (record.status, record.error_message) == ("failed", "Model inference timed out")  # kept for reprocessing
