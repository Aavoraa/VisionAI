"""Customer dashboard and admin endpoints (D2)."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.security import api_key_display_prefix, generate_api_key, hash_api_key
from app.database.models import APIKey, Job, UsageLog, User, UserSession
from app.models.local_transformers import MockVisionModel
from app.services.usage_service import UsageWindow
from conftest import ClientFactory
from scripts.create_admin import create_or_promote_admin

pytestmark = pytest.mark.anyio

SECRET = "correct horse battery staple"
KEYS = "/v1/dashboard/keys"
USAGE = "/v1/dashboard/usage"
JOBS = "/v1/dashboard/jobs"
USERS = "/v1/admin/users"
ADMIN_USAGE = "/v1/admin/usage"
MISSING_ID = "00000000-0000-4000-8000-000000000000"


def error(response: httpx.Response) -> dict[str, Any]:
    return response.json()["error"]


class Account:
    """A signed-in dashboard client."""

    def __init__(self, client: httpx.AsyncClient, user_id: str, csrf: str) -> None:
        self.client = client
        self.user_id = user_id
        self.csrf = {"X-CSRF-Token": csrf}

    async def create_key(self, name: str = "laptop", **extra: Any) -> dict[str, Any]:
        response = await self.client.post(KEYS, headers=self.csrf, json={"name": name, **extra})
        assert response.status_code == 201, response.text
        return response.json()


async def sign_in(
    client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    email: str,
    *,
    admin: bool = False,
) -> Account:
    if admin:
        await create_or_promote_admin(sessionmaker, email, SECRET)
        response = await client.post("/v1/auth/login", json={"email": email, "password": SECRET})
    else:
        response = await client.post("/v1/auth/signup", json={"email": email, "password": SECRET})
    assert response.status_code in (200, 201), response.text
    body = response.json()
    return Account(client, body["user"]["id"], body["csrf_token"])


@pytest.fixture
async def clients(settings: Settings, make_client: ClientFactory) -> AsyncIterator[list[httpx.AsyncClient]]:
    """Three independent browsers (separate cookie jars) against one app database."""
    async with make_client(settings) as a, make_client(settings) as b, make_client(settings) as c:
        yield [a, b, c]


@pytest.fixture
async def alice(clients: list[httpx.AsyncClient], db_sessionmaker: async_sessionmaker[AsyncSession]) -> Account:
    return await sign_in(clients[0], db_sessionmaker, "alice@example.com")


@pytest.fixture
async def bob(clients: list[httpx.AsyncClient], db_sessionmaker: async_sessionmaker[AsyncSession]) -> Account:
    return await sign_in(clients[1], db_sessionmaker, "bob@example.com")


@pytest.fixture
async def root(clients: list[httpx.AsyncClient], db_sessionmaker: async_sessionmaker[AsyncSession]) -> Account:
    return await sign_in(clients[2], db_sessionmaker, "root@example.com", admin=True)


def bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


async def api_key_works(client: httpx.AsyncClient, secret: str) -> int:
    """Status of an API-key-authenticated call (404 = authenticated, job simply unknown)."""
    return (await client.get(f"/v1/jobs/{MISSING_ID}", headers=bearer(secret))).status_code


# ===========================================================================
# API keys
# ===========================================================================


async def test_create_key_returns_secret_once_and_it_works(
    alice: Account, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    created = await alice.create_key("production server")
    secret = created["secret"]
    key = created["key"]
    assert secret.startswith("sk_live_") and key["prefix"] == secret[:12]
    assert key["name"] == "production server" and key["is_active"] is True and key["expires_at"] is None
    assert await api_key_works(alice.client, secret) == 404

    listed = (await alice.client.get(KEYS)).json()["items"]
    assert [k["id"] for k in listed] == [key["id"]]
    assert secret not in str(listed)  # never shown again
    async with db_sessionmaker() as session:
        stored = await session.get(APIKey, key["id"])
    assert stored is not None and stored.key_hash == hash_api_key(secret)


async def test_create_key_with_expiry(alice: Account) -> None:
    key = (await alice.create_key("temp", expires_in_days=7))["key"]
    expires = datetime.fromisoformat(key["expires_at"])
    assert expires.utcoffset() == timedelta(0)
    assert timedelta(days=6, hours=23) < expires - datetime.now(UTC) <= timedelta(days=7)


@pytest.mark.parametrize(
    ("payload", "param"),
    [
        ({"name": ""}, "name"),
        ({"name": "x" * 101}, "name"),
        ({}, "name"),
        ({"name": "ok", "expires_in_days": 0}, "expires_in_days"),
        ({"name": "ok", "user_id": MISSING_ID}, "user_id"),  # cannot create keys for someone else
    ],
)
async def test_create_key_validation(alice: Account, payload: dict[str, Any], param: str) -> None:
    response = await alice.client.post(KEYS, headers=alice.csrf, json=payload)
    assert response.status_code == 422
    assert error(response)["param"] == param


async def test_create_key_requires_csrf(alice: Account) -> None:
    response = await alice.client.post(KEYS, json={"name": "x"})
    assert response.status_code == 403


async def test_active_key_limit(settings: Settings, make_client: ClientFactory, db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with make_client(settings.model_copy(update={"max_api_keys_per_user": 2})) as client:
        user = await sign_in(client, db_sessionmaker, "limit@example.com")
        first = await user.create_key("a")
        await user.create_key("b")
        blocked = await client.post(KEYS, headers=user.csrf, json={"name": "c"})
        assert blocked.status_code == 409
        assert error(blocked)["message"] == "Active API key limit (2) reached; revoke a key first"

        await client.delete(f"{KEYS}/{first['key']['id']}", headers=user.csrf)
        assert (await client.post(KEYS, headers=user.csrf, json={"name": "c"})).status_code == 201


async def test_rename_key(alice: Account) -> None:
    key = (await alice.create_key("old"))["key"]
    response = await alice.client.patch(f"{KEYS}/{key['id']}", headers=alice.csrf, json={"name": "  new name  "})
    assert response.status_code == 200
    assert response.json()["name"] == "new name"
    assert response.json()["prefix"] == key["prefix"]


async def test_revoke_key_stops_it_immediately(alice: Account) -> None:
    created = await alice.create_key()
    key_url = f"{KEYS}/{created['key']['id']}"
    assert (await alice.client.delete(key_url, headers=alice.csrf)).status_code == 204
    assert await api_key_works(alice.client, created["secret"]) == 401
    assert (await alice.client.delete(key_url, headers=alice.csrf)).status_code == 204  # idempotent


async def test_keys_listed_active_first_then_newest(alice: Account) -> None:
    first = (await alice.create_key("first"))["key"]
    second = (await alice.create_key("second"))["key"]
    third = (await alice.create_key("third"))["key"]
    await alice.client.delete(f"{KEYS}/{third['id']}", headers=alice.csrf)
    listed = [k["name"] for k in (await alice.client.get(KEYS)).json()["items"]]
    assert listed == ["second", "first", "third"]
    assert second["id"] != first["id"]


async def test_users_cannot_touch_each_others_keys(alice: Account, bob: Account) -> None:
    alice_key = await alice.create_key()
    url = f"{KEYS}/{alice_key['key']['id']}"
    rename = await bob.client.patch(url, headers=bob.csrf, json={"name": "pwned"})
    revoke = await bob.client.delete(url, headers=bob.csrf)
    assert rename.status_code == revoke.status_code == 404
    assert error(revoke)["message"] == "API key not found"
    assert (await bob.client.get(KEYS)).json()["items"] == []
    assert await api_key_works(alice.client, alice_key["secret"]) == 404  # still works


@pytest.mark.parametrize("key_id", ["not-a-uuid", "1' OR '1'='1"])
async def test_malformed_key_id(alice: Account, key_id: str) -> None:
    response = await alice.client.delete(f"{KEYS}/{key_id}", headers=alice.csrf)
    assert response.status_code == 422
    assert error(response)["param"] == "key_id"


async def test_last_used_is_recorded_and_throttled(
    alice: Account, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    created = await alice.create_key()
    assert created["key"]["last_used_at"] is None
    await api_key_works(alice.client, created["secret"])
    first = (await alice.client.get(KEYS)).json()["items"][0]["last_used_at"]
    assert first is not None
    await api_key_works(alice.client, created["secret"])
    assert (await alice.client.get(KEYS)).json()["items"][0]["last_used_at"] == first  # within 5 minutes


async def test_dashboard_requires_a_session_not_an_api_key(alice: Account, clients: list[httpx.AsyncClient]) -> None:
    secret = (await alice.create_key())["secret"]
    anonymous = clients[1]
    for path in (KEYS, USAGE, JOBS):
        assert (await anonymous.get(path)).status_code == 401
        assert (await anonymous.get(path, headers=bearer(secret))).status_code == 401


# ===========================================================================
# Usage
# ===========================================================================


def at(day_offset: int, hour: int = 12) -> datetime:
    """A timestamp `day_offset` days before today (UTC); today's entries use 00:00:01."""
    today = datetime.now(UTC).date()
    moment = time(hour) if day_offset else time(0, 0, 1)
    return datetime.combine(today - timedelta(days=day_offset), moment, tzinfo=UTC)


async def add_key(sessionmaker: async_sessionmaker[AsyncSession], user_id: str, name: str) -> str:
    raw, key_hash = generate_api_key()
    async with sessionmaker() as session:
        key = APIKey(user_id=user_id, name=name, key_hash=key_hash, prefix=api_key_display_prefix(raw))
        session.add(key)
        await session.commit()
        return key.id


async def add_usage(
    sessionmaker: async_sessionmaker[AsyncSession],
    user_id: str,
    key_id: str,
    created_at: datetime,
    *,
    model: str = "model-a",
    status: str = "success",
    tokens: tuple[int, int] = (100, 10),
    images: int = 1,
    ms: int = 500,
) -> None:
    async with sessionmaker() as session:
        session.add(
            UsageLog(
                user_id=user_id,
                api_key_id=key_id,
                request_id=uuid.uuid4().hex,
                model=model,
                input_tokens=tokens[0],
                output_tokens=tokens[1],
                image_count=images,
                status=status,
                processing_time_ms=ms,
                created_at=created_at,
            )
        )
        await session.commit()


@pytest.fixture
async def seeded(alice: Account, bob: Account, db_sessionmaker: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    """Alice: 4 requests in the last week (2 keys, 2 models, 1 error) + 1 old one. Bob: 2."""
    laptop = await add_key(db_sessionmaker, alice.user_id, "laptop")
    server = await add_key(db_sessionmaker, alice.user_id, "server")
    bob_key = await add_key(db_sessionmaker, bob.user_id, "bob key")
    add = add_usage
    await add(db_sessionmaker, alice.user_id, laptop, at(2), model="model-a", tokens=(100, 10), ms=400)
    await add(db_sessionmaker, alice.user_id, laptop, at(2, hour=23), model="model-b", tokens=(200, 20), ms=600)
    await add(db_sessionmaker, alice.user_id, server, at(2, hour=1), status="error", tokens=(0, 0), images=2, ms=100)
    await add(db_sessionmaker, alice.user_id, server, at(0), model="model-a", tokens=(300, 30), ms=900)
    await add(db_sessionmaker, alice.user_id, laptop, at(40), model="model-a", tokens=(999, 99))  # outside window
    await add(db_sessionmaker, bob.user_id, bob_key, at(1), model="model-b", tokens=(50, 5))
    await add(db_sessionmaker, bob.user_id, bob_key, at(0), model="model-b", tokens=(50, 5))
    return {"laptop": laptop, "server": server, "bob_key": bob_key}


async def test_usage_report_for_the_signed_in_user(alice: Account, seeded: dict[str, str]) -> None:
    report = (await alice.client.get(USAGE, params={"days": 7})).json()

    today = datetime.now(UTC).date()
    assert report["days"] == 7
    assert report["end"] == today.isoformat()
    assert report["start"] == (today - timedelta(days=6)).isoformat()
    assert report["totals"] == {
        "requests": 4,
        "successful": 3,
        "failed": 1,
        "input_tokens": 600,
        "output_tokens": 60,
        "images": 5,
        "avg_processing_ms": 500.0,
        "cost_usd": "0.000000",  # seeded rows carry no charges; see test_billing.py
    }

    daily = report["daily"]
    assert [d["date"] for d in daily] == [(today - timedelta(days=6 - i)).isoformat() for i in range(7)]
    by_date = {d["date"]: d for d in daily}
    two_days_ago = by_date[(today - timedelta(days=2)).isoformat()]
    assert (two_days_ago["requests"], two_days_ago["failed"], two_days_ago["input_tokens"]) == (3, 1, 300)
    assert by_date[today.isoformat()]["requests"] == 1
    assert sum(d["requests"] for d in daily) == 4  # other days zero-filled

    assert report["by_model"] == [
        {"model": "model-a", "requests": 3, "input_tokens": 400, "output_tokens": 40},
        {"model": "model-b", "requests": 1, "input_tokens": 200, "output_tokens": 20},
    ]
    assert [(k["name"], k["requests"]) for k in report["by_key"]] == [("laptop", 2), ("server", 2)]
    assert {k["api_key_id"] for k in report["by_key"]} == {seeded["laptop"], seeded["server"]}


async def test_usage_never_includes_other_users(bob: Account, seeded: dict[str, str]) -> None:
    report = (await bob.client.get(USAGE)).json()
    assert report["totals"]["requests"] == 2
    assert [k["name"] for k in report["by_key"]] == ["bob key"]


async def test_usage_window_excludes_older_data(alice: Account, seeded: dict[str, str]) -> None:
    thirty = (await alice.client.get(USAGE)).json()
    ninety = (await alice.client.get(USAGE, params={"days": 90})).json()
    assert thirty["days"] == 30 and len(thirty["daily"]) == 30
    assert thirty["totals"]["requests"] == 4
    assert ninety["totals"]["requests"] == 5


async def test_empty_usage_report(alice: Account) -> None:
    report = (await alice.client.get(USAGE, params={"days": 3})).json()
    assert report["totals"]["requests"] == 0
    assert report["totals"]["avg_processing_ms"] is None
    assert [d["requests"] for d in report["daily"]] == [0, 0, 0]
    assert report["by_model"] == report["by_key"] == []


@pytest.mark.parametrize("days", [0, 91, -1])
async def test_usage_days_validation(alice: Account, days: int) -> None:
    response = await alice.client.get(USAGE, params={"days": days})
    assert response.status_code == 422
    assert error(response)["param"] == "days"


async def test_real_api_call_appears_in_usage(alice: Account) -> None:
    import io

    from PIL import Image

    secret = (await alice.create_key())["secret"]
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buffer, format="PNG")
    response = await alice.client.post(
        "/v1/vision",
        headers=bearer(secret),
        data={"prompt": "describe"},
        files=[("images", ("a.png", buffer.getvalue(), "image/png"))],
    )
    assert response.status_code == 200
    report = (await alice.client.get(USAGE, params={"days": 1})).json()
    assert report["totals"]["requests"] == 1
    assert report["totals"]["input_tokens"] == response.json()["usage"]["input_tokens"]
    assert report["by_key"][0]["name"] == "laptop"


def test_usage_window_bounds() -> None:
    window = UsageWindow.last_days(7, today=date(2026, 3, 10))
    assert (window.start, window.end, window.days) == (date(2026, 3, 4), date(2026, 3, 10), 7)
    assert window.since == datetime(2026, 3, 4, tzinfo=UTC)


# ===========================================================================
# Jobs
# ===========================================================================


async def test_job_list_is_paginated_and_private(
    alice: Account, bob: Account, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    alice_key = await add_key(db_sessionmaker, alice.user_id, "k")
    bob_key = await add_key(db_sessionmaker, bob.user_id, "k")
    async with db_sessionmaker() as session:
        for i in range(3):
            session.add(
                Job(
                    user_id=alice.user_id,
                    api_key_id=alice_key,
                    status="failed" if i == 2 else "completed",
                    error_message="Model inference failed" if i == 2 else None,
                    created_at=at(0) + timedelta(minutes=i),
                )
            )
        session.add(Job(user_id=bob.user_id, api_key_id=bob_key, status="pending"))
        await session.commit()

    page = (await alice.client.get(JOBS, params={"limit": 2})).json()
    assert (page["total"], page["limit"], page["offset"], len(page["items"])) == (3, 2, 0, 2)
    assert page["items"][0]["status"] == "failed"  # newest first
    assert page["items"][0]["error"] == "Model inference failed"
    rest = (await alice.client.get(JOBS, params={"limit": 2, "offset": 2})).json()
    assert len(rest["items"]) == 1 and rest["items"][0]["status"] == "completed"
    assert (await bob.client.get(JOBS)).json()["total"] == 1


# ===========================================================================
# Admin
# ===========================================================================

ADMIN_CALLS = [
    ("get", USERS, None),
    ("patch", f"{USERS}/{MISSING_ID}", {"is_active": False}),
    ("get", f"{USERS}/{MISSING_ID}/keys", None),
    ("delete", f"/v1/admin/keys/{MISSING_ID}", None),
    ("get", ADMIN_USAGE, None),
]


@pytest.mark.parametrize(("method", "path", "body"), ADMIN_CALLS)
async def test_admin_endpoints_reject_regular_users(alice: Account, method: str, path: str, body: Any) -> None:
    kwargs: dict[str, Any] = {"headers": alice.csrf}
    if body is not None:
        kwargs["json"] = body
    response = await alice.client.request(method.upper(), path, **kwargs)
    assert response.status_code == 403
    assert error(response)["message"] == "Administrator access required"


@pytest.mark.parametrize(("method", "path", "body"), ADMIN_CALLS)
async def test_admin_endpoints_reject_anonymous(clients: list[httpx.AsyncClient], method: str, path: str, body: Any) -> None:
    kwargs: dict[str, Any] = {"json": body} if body is not None else {}
    assert (await clients[0].request(method.upper(), path, **kwargs)).status_code == 401


async def test_admin_lists_users_with_stats(root: Account, alice: Account, bob: Account, seeded: dict[str, str]) -> None:
    await alice.create_key()
    page = (await root.client.get(USERS)).json()
    assert page["total"] == 3
    rows = {u["email"]: u for u in page["items"]}
    assert rows["alice@example.com"]["active_keys"] == 3  # 2 seeded + 1 created
    assert rows["alice@example.com"]["requests_30d"] == 4  # the 40-day-old request excluded
    assert rows["bob@example.com"]["requests_30d"] == 2
    assert rows["root@example.com"]["role"] == "admin"
    assert all(u["has_password"] for u in page["items"])
    assert [u["email"] for u in page["items"]][0] == "bob@example.com"  # newest first


async def test_admin_user_search_and_pagination(
    root: Account, alice: Account, bob: Account, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as session:
        session.add(User(email="percent%sign@example.com"))
        await session.commit()

    found = (await root.client.get(USERS, params={"search": "  ALICE "})).json()
    assert [u["email"] for u in found["items"]] == ["alice@example.com"] and found["total"] == 1
    literal = (await root.client.get(USERS, params={"search": "%"})).json()
    assert [u["email"] for u in literal["items"]] == ["percent%sign@example.com"]  # % is not a wildcard
    assert (await root.client.get(USERS, params={"search": "_"})).json()["total"] == 0

    page = (await root.client.get(USERS, params={"limit": 2, "offset": 2})).json()
    assert (page["total"], len(page["items"])) == (4, 2)
    key_only = next(u for u in (await root.client.get(USERS)).json()["items"] if u["email"].startswith("percent"))
    assert key_only["has_password"] is False


async def test_deactivating_a_user_cuts_off_sessions_and_keys(root: Account, alice: Account) -> None:
    secret = (await alice.create_key())["secret"]
    response = await root.client.patch(f"{USERS}/{alice.user_id}", headers=root.csrf, json={"is_active": False})
    assert response.status_code == 200
    assert response.json()["is_active"] is False and response.json()["active_keys"] == 1

    assert (await alice.client.get("/v1/auth/me")).status_code == 401  # session deleted
    assert await api_key_works(alice.client, secret) == 403  # key refused while inactive

    await root.client.patch(f"{USERS}/{alice.user_id}", headers=root.csrf, json={"is_active": True})
    assert await api_key_works(alice.client, secret) == 404  # works again


async def test_promotion_grants_admin_access(root: Account, alice: Account) -> None:
    assert (await alice.client.get(USERS)).status_code == 403
    promoted = await root.client.patch(f"{USERS}/{alice.user_id}", headers=root.csrf, json={"role": "admin"})
    assert promoted.json()["role"] == "admin"
    assert (await alice.client.get(USERS)).status_code == 200  # same session, new rights


@pytest.mark.parametrize("body", [{"is_active": False}, {"role": "user"}, {"role": "user", "is_active": True}])
async def test_admins_cannot_lock_themselves_out(root: Account, body: dict[str, Any]) -> None:
    response = await root.client.patch(f"{USERS}/{root.user_id}", headers=root.csrf, json=body)
    assert response.status_code == 409
    assert error(response)["message"] == "You cannot deactivate or demote your own account"
    assert (await root.client.get(USERS)).status_code == 200


async def test_admin_update_validation(root: Account, alice: Account) -> None:
    empty = await root.client.patch(f"{USERS}/{alice.user_id}", headers=root.csrf, json={})
    bad_role = await root.client.patch(f"{USERS}/{alice.user_id}", headers=root.csrf, json={"role": "owner"})
    unknown = await root.client.patch(f"{USERS}/{MISSING_ID}", headers=root.csrf, json={"is_active": False})
    no_csrf = await root.client.patch(f"{USERS}/{alice.user_id}", json={"is_active": False})
    assert empty.status_code == 422 and error(empty)["message"] == "Value error, provide is_active, role and/or plan_id"
    assert bad_role.status_code == 422 and error(bad_role)["param"] == "role"
    assert unknown.status_code == 404 and error(unknown)["message"] == "User not found"
    assert no_csrf.status_code == 403


async def test_admin_views_and_revokes_any_key(root: Account, alice: Account) -> None:
    created = await alice.create_key("leaked")
    keys = (await root.client.get(f"{USERS}/{alice.user_id}/keys")).json()
    assert keys["user_id"] == alice.user_id
    assert [k["name"] for k in keys["items"]] == ["leaked"]
    assert "secret" not in keys["items"][0]

    url = f"/v1/admin/keys/{created['key']['id']}"
    assert (await root.client.delete(url)).status_code == 403  # CSRF required
    assert (await root.client.delete(url, headers=root.csrf)).status_code == 204
    assert await api_key_works(alice.client, created["secret"]) == 401
    assert (await root.client.delete(f"/v1/admin/keys/{MISSING_ID}", headers=root.csrf)).status_code == 404
    assert (await root.client.get(f"{USERS}/{MISSING_ID}/keys")).status_code == 404


async def test_platform_usage(root: Account, seeded: dict[str, str]) -> None:
    report = (await root.client.get(ADMIN_USAGE, params={"days": 7})).json()
    assert report["active_users"] == 2
    assert report["totals"]["requests"] == 6
    assert report["totals"]["failed"] == 1
    assert [(u["email"], u["requests"]) for u in report["top_users"]] == [
        ("alice@example.com", 4),
        ("bob@example.com", 2),
    ]
    assert [m["model"] for m in report["by_model"]] == ["model-a", "model-b"]
    assert {m["model"]: m["requests"] for m in report["by_model"]} == {"model-a": 3, "model-b": 3}
    assert len(report["daily"]) == 7


# ===========================================================================
# Documented examples match real behaviour
# ===========================================================================


def _documented(spec: dict[str, Any], path: str, method: str, code: int, name: str) -> dict[str, Any]:
    examples = spec["paths"][path][method]["responses"][str(code)]["content"]["application/json"]["examples"]
    return examples[name]["value"]["error"]


def _without_nulls(err: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in err.items() if value is not None}  # FastAPI drops nulls in docs


async def test_documented_dashboard_errors_match_real_responses(
    root: Account, alice: Account, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    spec = (await alice.client.get("/openapi.json")).json()
    key_path = "/v1/dashboard/keys/{key_id}"

    for _ in range(25):  # default limit
        await alice.create_key()
    limit = await alice.client.post(KEYS, headers=alice.csrf, json={"name": "one too many"})
    assert _without_nulls(error(limit)) == _documented(spec, KEYS, "post", 409, "limit")

    missing = await alice.client.delete(f"{KEYS}/{MISSING_ID}", headers=alice.csrf)
    assert _without_nulls(error(missing)) == _documented(spec, key_path, "delete", 404, "not_found")
    malformed = await alice.client.delete(f"{KEYS}/nope", headers=alice.csrf)
    assert error(malformed) == _documented(spec, key_path, "delete", 422, "malformed_id")

    not_admin = await alice.client.get(USERS)
    assert _without_nulls(error(not_admin)) == _documented(spec, USERS, "get", 403, "not_admin")
    self_lockout = await root.client.patch(f"{USERS}/{root.user_id}", headers=root.csrf, json={"is_active": False})
    assert _without_nulls(error(self_lockout)) == _documented(spec, f"{USERS}/{{user_id}}", "patch", 409, "self")
    empty = await root.client.patch(f"{USERS}/{alice.user_id}", headers=root.csrf, json={})
    assert _without_nulls(error(empty)) == _documented(spec, f"{USERS}/{{user_id}}", "patch", 422, "empty")
    days = await alice.client.get(USAGE, params={"days": 91})
    assert error(days) == _documented(spec, USAGE, "get", 422, "out_of_range")


async def test_no_session_rows_for_deactivated_user(
    root: Account, alice: Account, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await root.client.patch(f"{USERS}/{alice.user_id}", headers=root.csrf, json={"is_active": False})
    async with db_sessionmaker() as session:
        remaining = (await session.execute(select(UserSession).where(UserSession.user_id == alice.user_id))).all()
    assert remaining == []


# ===========================================================================
# Serving model
# ===========================================================================


async def test_model_info_for_chat_models(alice: Account, clients: list[httpx.AsyncClient]) -> None:
    response = await alice.client.get("/v1/dashboard/model")
    assert response.status_code == 200
    assert response.json() == {"name": "mock-vision-1", "invoice_extraction": False, "tasks": None}
    assert (await clients[1].get("/v1/dashboard/model")).status_code == 401


async def test_model_info_lists_tasks_of_task_models(
    settings: Settings, make_client: ClientFactory, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    class TaskModel(MockVisionModel):
        name = "org/florence"
        tasks = {"<OCR>": "Read all text in the image", "<CAPTION>": "Short description"}

    async with make_client(settings, model=TaskModel()) as client:
        account = await sign_in(client, db_sessionmaker, "carol@example.com")
        body = (await account.client.get("/v1/dashboard/model")).json()
    assert body == {
        "name": "org/florence",
        "invoice_extraction": False,  # Florence-2 has no table task
        "tasks": [
            {"task": "<OCR>", "description": "Read all text in the image"},
            {"task": "<CAPTION>", "description": "Short description"},
        ],
    }
