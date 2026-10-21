"""Live extraction comments: the in-memory hub, the X-Progress-Id header and the polling endpoint."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.services.invoice_progress import MAX_EVENTS, ProgressHub
from conftest import ClientFactory, create_api_key
from test_invoice_extraction import FakePaddle, page_png
from test_invoices_api import EXTRACT, upload

pytestmark = pytest.mark.anyio

PROGRESS_ID = "tester-3f9a2c7e51b0"


def test_hub_keeps_comments_per_user_in_order() -> None:
    hub = ProgressHub()
    say = hub.open(PROGRESS_ID, "user-1")
    assert say is not None
    say("header", "Reading the header", region=(0.0, 0.0, 1.0, 0.35), page=1)
    say("result", "Done", level="ok", done=True)
    first = hub.read(PROGRESS_ID, "user-1")
    assert first is not None and first["count"] == 2 and first["done"] is True
    assert first["events"][0] == {"t": first["events"][0]["t"], "stage": "header", "text": "Reading the header",
                                  "level": "info", "page": 1, "region": [0.0, 0.0, 1.0, 0.35]}
    assert hub.read(PROGRESS_ID, "user-1", after=1)["events"][0]["text"] == "Done"  # type: ignore[index]
    assert hub.read(PROGRESS_ID, "user-2") is None  # someone else's
    assert hub.open(PROGRESS_ID, "user-2") is None  # cannot be taken over
    assert hub.open("short", "user-1") is None and hub.open("bad id with spaces", "user-1") is None


def test_hub_caps_the_comments() -> None:
    hub = ProgressHub()
    say = hub.open(PROGRESS_ID, "u")
    for n in range(MAX_EVENTS + 50):
        say("bands", f"band {n}")  # type: ignore[misc]
    assert hub.read(PROGRESS_ID, "u")["count"] == MAX_EVENTS  # type: ignore[index]


async def test_extraction_comments_can_be_polled(
    settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str],
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    other_headers, _, _ = await create_api_key(db_sessionmaker, "other@example.com")
    async with make_client(settings, model=FakePaddle()) as client:
        response = await client.post(EXTRACT, headers={**auth_headers, "X-Progress-Id": PROGRESS_ID}, **upload(page_png()))
        assert response.status_code == 200, response.text
        progress = (await client.get(f"/v1/invoices/progress/{PROGRESS_ID}", headers=auth_headers)).json()
        later = (await client.get(f"/v1/invoices/progress/{PROGRESS_ID}?after=3", headers=auth_headers)).json()
        stranger = await client.get(f"/v1/invoices/progress/{PROGRESS_ID}", headers=other_headers)
        malformed = await client.post(EXTRACT, headers={**auth_headers, "X-Progress-Id": "x y"}, **upload(page_png()))

    stages = [e["stage"] for e in progress["events"]]
    assert stages[0] == "start" and stages[-1] == "result" and progress["done"] is True
    assert {"deskew", "header", "table", "footer", "rules", "details", "validate"} <= set(stages)
    header = next(e for e in progress["events"] if e["stage"] == "header")
    assert header["region"] == [0.0, 0.0, 1.0, 0.35] and header["page"] == 1  # where the step reads
    assert progress["events"][-1]["text"].startswith("Done: needs review")
    assert later["events"] == progress["events"][3:] and later["count"] == progress["count"]
    assert stranger.status_code == 404  # another account cannot read the comments
    assert malformed.status_code == 400
