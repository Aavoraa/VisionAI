"""Dashboard accounts: signup, login, sessions, CSRF, roles and the admin script."""

import os
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Depends
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import main as main_module
from app.api.dependencies import AuthenticatedSession, require_admin
from app.core.config import Settings
from app.core.security import (
    hash_password,
    hash_token,
    normalize_email,
    password_needs_rehash,
    validate_password,
    verify_password,
)
from app.database.database import get_db, get_session_factory
from app.database.models import User, UserSession
from app.main import create_app
from conftest import ClientFactory, create_api_key
from scripts.create_admin import create_or_promote_admin

pytestmark = pytest.mark.anyio

SIGNUP = "/v1/auth/signup"
LOGIN = "/v1/auth/login"
LOGOUT = "/v1/auth/logout"
ME = "/v1/auth/me"
PASSWORD = "/v1/auth/password"
ADMIN_ONLY = "/v1/_test/admin-only"

EMAIL = "ada@example.com"
SECRET = "correct horse battery staple"


def error(response: httpx.Response) -> dict[str, Any]:
    return response.json()["error"]


async def signup(client: httpx.AsyncClient, email: str = EMAIL, password: str = SECRET) -> httpx.Response:
    return await client.post(SIGNUP, json={"email": email, "password": password})


async def rows(sessionmaker: async_sessionmaker[AsyncSession], model: type[Any]) -> list[Any]:
    async with sessionmaker() as session:
        return list((await session.execute(select(model))).scalars())


@pytest.fixture
async def accounts_client(
    settings: Settings, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[httpx.AsyncClient]:
    """Full app (lifespan + fake Redis) plus a test-only admin route; production exposes none."""
    app = create_app(settings)

    @app.get(ADMIN_ONLY)
    async def admin_only(auth: AuthenticatedSession = Depends(require_admin)) -> dict[str, str]:
        return {"admin": auth.user.email}

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: db_sessionmaker
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            yield client


# ===========================================================================
# Password / email primitives
# ===========================================================================


def test_password_hashing_uses_argon2id_and_verifies() -> None:
    hashed = hash_password(SECRET)
    assert hashed.startswith("$argon2id$")
    assert SECRET not in hashed
    assert hash_password(SECRET) != hashed  # salted
    assert verify_password(hashed, SECRET)
    assert not verify_password(hashed, SECRET + "!")


@pytest.mark.parametrize("stored", [None, "", "not-a-hash", "$argon2id$garbage"])
def test_verify_password_fails_closed(stored: str | None) -> None:
    assert verify_password(stored, SECRET) is False


def test_weak_argon2_parameters_are_flagged_for_rehash() -> None:
    from argon2 import PasswordHasher

    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(SECRET)
    assert password_needs_rehash(weak)
    assert not password_needs_rehash(hash_password(SECRET))


@pytest.mark.parametrize("password", ["short", " " * 12, "x" * 129])
def test_password_policy_rejects(password: str) -> None:
    with pytest.raises(ValueError):
        validate_password(password)


def test_password_policy_accepts_passphrases() -> None:
    assert validate_password("four plain words") == "four plain words"


@pytest.mark.parametrize(("raw", "expected"), [("  Ada@Example.COM ", "ada@example.com"), ("a.b+c@d.co", "a.b+c@d.co")])
def test_normalize_email(raw: str, expected: str) -> None:
    assert normalize_email(raw) == expected


@pytest.mark.parametrize("raw", ["", "no-at-sign", "a@b", "a b@c.com", "@example.com"])
def test_normalize_email_rejects(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_email(raw)


# ===========================================================================
# Signup
# ===========================================================================


async def test_signup_creates_user_and_signs_in(
    accounts_client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    response = await signup(accounts_client, email="  Ada@Example.com ")
    assert response.status_code == 201
    body = response.json()
    assert body["user"]["email"] == EMAIL
    assert body["user"]["role"] == "user"
    assert body["user"]["last_login_at"] is not None
    assert "password" not in response.text and "hash" not in response.text

    session_cookie = response.cookies["vision_session"]
    assert response.cookies["vision_csrf"] == body["csrf_token"]
    set_cookie = "\n".join(response.headers.get_list("set-cookie")).lower()
    assert "vision_session=" in set_cookie and "httponly" in set_cookie and "samesite=lax" in set_cookie

    (user,) = await rows(db_sessionmaker, User)
    assert user.password_hash.startswith("$argon2id$")
    (session,) = await rows(db_sessionmaker, UserSession)
    assert session.token_hash == hash_token(session_cookie)  # only the hash is stored
    assert session_cookie not in (session.token_hash, session.csrf_token_hash)

    me = await accounts_client.get(ME)
    assert me.status_code == 200 and me.json()["email"] == EMAIL


async def test_timestamps_always_carry_a_utc_offset(accounts_client: httpx.AsyncClient) -> None:
    # SQLite hands back naive datetimes; the API must still emit explicit UTC for browsers.
    await signup(accounts_client)
    body = (await accounts_client.get(ME)).json()
    for field in ("created_at", "last_login_at"):
        parsed = datetime.fromisoformat(body[field])
        assert parsed.utcoffset() == timedelta(0), body[field]


async def test_signup_rejects_existing_email(accounts_client: httpx.AsyncClient) -> None:
    await signup(accounts_client)
    duplicate = await signup(accounts_client, email="ADA@example.com")
    assert duplicate.status_code == 409
    assert error(duplicate) == {
        "type": "conflict_error",
        "message": "An account with this email already exists",
        "param": None,
    }


async def test_signup_cannot_take_over_api_key_only_user(
    accounts_client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await create_api_key(db_sessionmaker, EMAIL)  # user without a password
    response = await signup(accounts_client)
    assert response.status_code == 409
    (user,) = await rows(db_sessionmaker, User)
    assert user.password_hash is None


@pytest.mark.parametrize(
    ("payload", "param"),
    [
        ({"email": EMAIL, "password": "short"}, "password"),
        ({"email": "nope", "password": SECRET}, "email"),
        ({"email": EMAIL}, "password"),
        ({"email": EMAIL, "password": SECRET, "role": "admin"}, "role"),  # no self-promotion
    ],
)
async def test_signup_validation(accounts_client: httpx.AsyncClient, payload: dict[str, Any], param: str) -> None:
    response = await accounts_client.post(SIGNUP, json=payload)
    assert response.status_code == 422
    assert error(response)["param"] == param


async def test_signup_disabled_in_production_by_default(
    settings: Settings, make_client: ClientFactory
) -> None:
    production = settings.model_copy(update={"environment": "production", "use_mock_model": True})
    assert production.signup_enabled is False
    async with make_client(production, run_lifespan=False) as client:
        response = await signup(client)
    assert response.status_code == 403
    assert error(response)["message"] == "Signup is disabled"


async def test_signup_can_be_forced_on_or_off(settings: Settings, make_client: ClientFactory) -> None:
    assert settings.model_copy(update={"environment": "production", "allow_signup": True}).signup_enabled
    async with make_client(settings.model_copy(update={"allow_signup": False})) as client:
        assert (await signup(client)).status_code == 403


# ===========================================================================
# Login
# ===========================================================================


async def test_login_success(accounts_client: httpx.AsyncClient) -> None:
    await signup(accounts_client)
    accounts_client.cookies.clear()

    response = await accounts_client.post(LOGIN, json={"email": "ADA@example.com", "password": SECRET})
    assert response.status_code == 200
    assert response.json()["user"]["email"] == EMAIL
    assert response.cookies["vision_csrf"] == response.json()["csrf_token"]
    assert (await accounts_client.get(ME)).status_code == 200


@pytest.mark.parametrize("email", [EMAIL, "nobody@example.com"])
async def test_login_failures_are_indistinguishable(accounts_client: httpx.AsyncClient, email: str) -> None:
    await signup(accounts_client)
    accounts_client.cookies.clear()
    response = await accounts_client.post(LOGIN, json={"email": email, "password": "wrong password!"})
    assert response.status_code == 401
    assert error(response)["message"] == "Invalid email or password"
    assert "vision_session" not in response.cookies


async def test_password_less_user_cannot_log_in(
    accounts_client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await create_api_key(db_sessionmaker, EMAIL)
    response = await accounts_client.post(LOGIN, json={"email": EMAIL, "password": SECRET})
    assert response.status_code == 401


async def test_inactive_user_cannot_log_in(
    accounts_client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await signup(accounts_client)
    accounts_client.cookies.clear()
    async with db_sessionmaker() as session:
        user = (await session.execute(select(User))).scalar_one()
        user.is_active = False
        await session.commit()
    response = await accounts_client.post(LOGIN, json={"email": EMAIL, "password": SECRET})
    assert response.status_code == 403
    assert error(response)["message"] == "User account is inactive"


async def test_login_upgrades_weak_password_hash(
    accounts_client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from argon2 import PasswordHasher

    async with db_sessionmaker() as session:
        session.add(User(email=EMAIL, password_hash=PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(SECRET)))
        await session.commit()
    assert (await accounts_client.post(LOGIN, json={"email": EMAIL, "password": SECRET})).status_code == 200
    (user,) = await rows(db_sessionmaker, User)
    assert not password_needs_rehash(user.password_hash)
    assert verify_password(user.password_hash, SECRET)


async def test_login_is_throttled_per_email(settings: Settings, make_client: ClientFactory) -> None:
    tight = settings.model_copy(update={"login_rate_limit_attempts": 3})
    async with make_client(tight) as client:
        await signup(client)
        client.cookies.clear()
        statuses = [
            (await client.post(LOGIN, json={"email": EMAIL, "password": "wrong password!"})).status_code
            for _ in range(4)
        ]
        # Even the right password is refused while throttled.
        blocked = await client.post(LOGIN, json={"email": EMAIL, "password": SECRET})
        other_account = await client.post(LOGIN, json={"email": "other@example.com", "password": SECRET})

    # The signup counted as the first attempt for this email.
    assert statuses == [401, 401, 429, 429]
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1
    assert error(blocked)["type"] == "rate_limit_error"
    assert other_account.status_code == 401  # per-email budget is separate


async def test_login_is_throttled_per_ip(settings: Settings, make_client: ClientFactory) -> None:
    tight = settings.model_copy(update={"login_rate_limit_attempts": 2})  # IP budget = 6
    async with make_client(tight) as client:
        statuses = [
            (await client.post(LOGIN, json={"email": f"u{i}@example.com", "password": SECRET})).status_code
            for i in range(7)
        ]
    assert statuses == [401] * 6 + [429]


async def test_login_survives_redis_outage_when_fail_open(
    settings: Settings, make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    class DownRedis:
        def pipeline(self, transaction: bool = True) -> Any:
            raise RedisConnectionError("down")

        async def ping(self) -> bool:
            raise RedisConnectionError("down")

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(main_module, "create_redis_client", lambda _settings: DownRedis())
    async with make_client(settings) as client:
        assert (await signup(client)).status_code == 201
    async with make_client(settings.model_copy(update={"rate_limit_fail_open": False})) as client:
        assert (await client.post(LOGIN, json={"email": EMAIL, "password": SECRET})).status_code == 503


# ===========================================================================
# Sessions and CSRF
# ===========================================================================


async def test_me_requires_a_session(accounts_client: httpx.AsyncClient) -> None:
    response = await accounts_client.get(ME)
    assert response.status_code == 401
    assert error(response)["message"] == "Not signed in"


async def test_forged_or_oversized_cookie_is_rejected(accounts_client: httpx.AsyncClient) -> None:
    await signup(accounts_client)
    for forged in ("forged-token", "x" * 500):
        accounts_client.cookies.set("vision_session", forged)
        assert (await accounts_client.get(ME)).status_code == 401


async def test_session_cookie_does_not_authenticate_the_vision_api(accounts_client: httpx.AsyncClient) -> None:
    await signup(accounts_client)
    response = await accounts_client.post("/v1/vision", json={"prompt": "x", "image_url": "https://example.com/a.png"})
    assert response.status_code == 401  # API keys only


async def expire(sessionmaker: async_sessionmaker[AsyncSession], **fields: datetime) -> None:
    async with sessionmaker() as session:
        record = (await session.execute(select(UserSession))).scalar_one()
        for name, value in fields.items():
            setattr(record, name, value)
        await session.commit()


async def test_expired_session_is_rejected_and_deleted(
    accounts_client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await signup(accounts_client)
    await expire(db_sessionmaker, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    response = await accounts_client.get(ME)
    assert response.status_code == 401
    assert error(response)["message"] == "Session expired; please sign in again"
    assert await rows(db_sessionmaker, UserSession) == []


async def test_idle_session_times_out(
    accounts_client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    await signup(accounts_client)
    stale = datetime.now(UTC) - timedelta(hours=settings.session_idle_timeout_hours, minutes=1)
    await expire(db_sessionmaker, last_seen_at=stale)
    assert (await accounts_client.get(ME)).status_code == 401


async def test_activity_refreshes_last_seen(
    accounts_client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await signup(accounts_client)
    earlier = datetime.now(UTC) - timedelta(hours=1)
    await expire(db_sessionmaker, last_seen_at=earlier)
    assert (await accounts_client.get(ME)).status_code == 200
    (session,) = await rows(db_sessionmaker, UserSession)
    last_seen = session.last_seen_at.replace(tzinfo=UTC) if session.last_seen_at.tzinfo is None else session.last_seen_at
    assert last_seen > earlier + timedelta(minutes=59)


async def test_deactivated_user_loses_access_immediately(
    accounts_client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await signup(accounts_client)
    async with db_sessionmaker() as session:
        user = (await session.execute(select(User))).scalar_one()
        user.is_active = False
        await session.commit()
    assert (await accounts_client.get(ME)).status_code == 403


@pytest.mark.parametrize("csrf", [None, "", "wrong-token", "x" * 500])
async def test_state_changes_require_the_csrf_token(accounts_client: httpx.AsyncClient, csrf: str | None) -> None:
    await signup(accounts_client)
    headers = {} if csrf is None else {"X-CSRF-Token": csrf}
    response = await accounts_client.post(LOGOUT, headers=headers)
    assert response.status_code == 403
    assert error(response)["message"] == "Missing or invalid CSRF token"
    assert (await accounts_client.get(ME)).status_code == 200  # still signed in


async def test_csrf_token_is_bound_to_its_session(settings: Settings, make_client: ClientFactory) -> None:
    async with make_client(settings) as alice, make_client(settings) as mallory:
        await signup(alice)
        mallory_csrf = (await signup(mallory, email="mallory@example.com")).json()["csrf_token"]
        response = await alice.post(LOGOUT, headers={"X-CSRF-Token": mallory_csrf})
    assert response.status_code == 403


async def test_logout_revokes_the_session_server_side(
    accounts_client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    csrf = (await signup(accounts_client)).json()["csrf_token"]
    stolen_cookie = accounts_client.cookies["vision_session"]

    response = await accounts_client.post(LOGOUT, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 204
    cleared = "\n".join(response.headers.get_list("set-cookie"))
    assert 'vision_session=""' in cleared and 'vision_csrf=""' in cleared
    assert await rows(db_sessionmaker, UserSession) == []

    accounts_client.cookies.set("vision_session", stolen_cookie)  # a copy kept by an attacker
    assert (await accounts_client.get(ME)).status_code == 401


async def test_expired_sessions_are_cleaned_up_on_login(
    accounts_client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await signup(accounts_client)
    await expire(db_sessionmaker, expires_at=datetime.now(UTC) - timedelta(days=1))
    accounts_client.cookies.clear()
    await accounts_client.post(LOGIN, json={"email": EMAIL, "password": SECRET})
    assert len(await rows(db_sessionmaker, UserSession)) == 1


# ===========================================================================
# Password change
# ===========================================================================


async def test_change_password_revokes_other_sessions(settings: Settings, make_client: ClientFactory) -> None:
    new_secret = "an even better passphrase"
    async with make_client(settings) as laptop, make_client(settings) as phone:
        csrf = (await signup(laptop)).json()["csrf_token"]
        assert (await phone.post(LOGIN, json={"email": EMAIL, "password": SECRET})).status_code == 200

        response = await laptop.post(
            PASSWORD,
            headers={"X-CSRF-Token": csrf},
            json={"current_password": SECRET, "new_password": new_secret},
        )
        assert response.status_code == 204
        assert (await laptop.get(ME)).status_code == 200  # current session kept
        assert (await phone.get(ME)).status_code == 401  # other sessions signed out

        laptop.cookies.clear()
        assert (await laptop.post(LOGIN, json={"email": EMAIL, "password": SECRET})).status_code == 401
        assert (await laptop.post(LOGIN, json={"email": EMAIL, "password": new_secret})).status_code == 200


async def test_change_password_checks_current_password(accounts_client: httpx.AsyncClient) -> None:
    csrf = (await signup(accounts_client)).json()["csrf_token"]
    wrong = await accounts_client.post(
        PASSWORD, headers={"X-CSRF-Token": csrf}, json={"current_password": "not it at all", "new_password": "x" * 12}
    )
    weak = await accounts_client.post(
        PASSWORD, headers={"X-CSRF-Token": csrf}, json={"current_password": SECRET, "new_password": "short"}
    )
    assert wrong.status_code == 400 and error(wrong)["message"] == "Current password is incorrect"
    assert weak.status_code == 422 and error(weak)["param"] == "new_password"


# ===========================================================================
# Roles
# ===========================================================================


async def test_require_admin_blocks_regular_users(accounts_client: httpx.AsyncClient) -> None:
    await signup(accounts_client)
    response = await accounts_client.get(ADMIN_ONLY)
    assert response.status_code == 403
    assert error(response)["message"] == "Administrator access required"


async def test_require_admin_allows_admins(
    accounts_client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await create_or_promote_admin(db_sessionmaker, "root@example.com", SECRET)
    await accounts_client.post(LOGIN, json={"email": "root@example.com", "password": SECRET})
    response = await accounts_client.get(ADMIN_ONLY)
    assert response.status_code == 200
    assert response.json() == {"admin": "root@example.com"}
    assert (await accounts_client.get(ME)).json()["role"] == "admin"


async def test_role_is_constrained(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    from sqlalchemy.exc import IntegrityError

    async with db_sessionmaker() as session:
        session.add(User(email="x@example.com", role="superuser"))
        with pytest.raises(IntegrityError):
            await session.commit()


# ===========================================================================
# scripts/create_admin.py
# ===========================================================================


async def test_create_admin_creates_and_promotes(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    user, created = await create_or_promote_admin(db_sessionmaker, "Root@Example.com", SECRET)
    assert created and user.role == "admin" and user.email == "root@example.com"

    async with db_sessionmaker() as session:
        session.add(User(email="promote@example.com", password_hash=hash_password(SECRET), is_active=False))
        await session.commit()
    promoted, created = await create_or_promote_admin(db_sessionmaker, "promote@example.com", None)
    assert not created and promoted.role == "admin" and promoted.is_active
    assert verify_password(promoted.password_hash, SECRET)  # --keep-password left it alone


@pytest.mark.parametrize(
    ("email", "password", "message"),
    [
        ("root@example.com", "short", "password"),
        ("not-an-email", SECRET, "email"),
        ("root@example.com", None, "password is required"),
    ],
)
async def test_create_admin_rejects_bad_input(
    db_sessionmaker: async_sessionmaker[AsyncSession], email: str, password: str | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        await create_or_promote_admin(db_sessionmaker, email, password)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_create_admin_script_end_to_end(tmp_path: Path) -> None:
    db_file = tmp_path / "admin.db"
    base_env = {k: v for k, v in os.environ.items() if k != "VISION_ADMIN_PASSWORD"}
    base_env.update({"VISION_DATABASE_URL": f"sqlite+aiosqlite:///{db_file.as_posix()}", "PYTHONPATH": ""})

    def run(*args: str, password: str | None) -> subprocess.CompletedProcess[str]:
        env = {**base_env, "VISION_ADMIN_PASSWORD": password} if password else base_env
        return subprocess.run(
            [sys.executable, *args],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,  # non-interactive: must fail fast instead of prompting
        )

    assert run("scripts/init_db.py", password=None).returncode == 0
    created = run("scripts/create_admin.py", "--email", "root@example.com", password=SECRET)
    assert created.returncode == 0, created.stderr
    assert "Created administrator root@example.com" in created.stdout
    assert SECRET not in created.stdout + created.stderr

    no_tty = run("scripts/create_admin.py", "--email", "second@example.com", password=None)
    assert no_tty.returncode == 2 and "VISION_ADMIN_PASSWORD" in no_tty.stderr

    with sqlite3.connect(db_file) as conn:
        role, password_hash = conn.execute("SELECT role, password_hash FROM users").fetchone()
        count = conn.execute("SELECT count(*) FROM users").fetchone()[0]
    assert (role, count) == ("admin", 1)
    assert verify_password(password_hash, SECRET)


async def test_no_session_rows_leak_between_users(
    settings: Settings, make_client: ClientFactory, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with make_client(settings) as a, make_client(settings) as b:
        await signup(a, email="a@example.com")
        await signup(b, email="b@example.com")
        assert (await a.get(ME)).json()["email"] == "a@example.com"
        assert (await b.get(ME)).json()["email"] == "b@example.com"
    async with db_sessionmaker() as session:
        assert (await session.execute(select(func.count()).select_from(UserSession))).scalar_one() == 2


# ===========================================================================
# Documented examples match real behaviour
# ===========================================================================


def _documented(spec: dict[str, Any], path: str, method: str, code: int, name: str) -> dict[str, Any]:
    examples = spec["paths"][path][method]["responses"][str(code)]["content"]["application/json"]["examples"]
    return examples[name]["value"]["error"]


def _without_nulls(err: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in err.items() if value is not None}  # FastAPI drops nulls in docs


async def test_documented_auth_errors_match_real_responses(accounts_client: httpx.AsyncClient) -> None:
    spec = (await accounts_client.get("/openapi.json")).json()

    not_signed_in = await accounts_client.get(ME)
    assert _without_nulls(error(not_signed_in)) == _documented(spec, ME, "get", 401, "not_signed_in")

    await signup(accounts_client)
    exists = await signup(accounts_client)
    assert _without_nulls(error(exists)) == _documented(spec, SIGNUP, "post", 409, "exists")

    csrf = await accounts_client.post(LOGOUT)
    assert _without_nulls(error(csrf)) == _documented(spec, LOGOUT, "post", 403, "csrf")

    short = await accounts_client.post(SIGNUP, json={"email": "new@example.com", "password": "short"})
    assert error(short) == _documented(spec, SIGNUP, "post", 422, "short_password")

    accounts_client.cookies.clear()
    wrong = await accounts_client.post(LOGIN, json={"email": EMAIL, "password": "wrong password!"})
    assert _without_nulls(error(wrong)) == _documented(spec, LOGIN, "post", 401, "invalid")
