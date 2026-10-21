from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.dependencies import get_current_api_key
from app.core.config import Settings
from app.core.security import (
    API_KEY_PREFIX,
    api_key_display_prefix,
    generate_api_key,
    hash_api_key,
    is_api_key_expired,
    is_valid_api_key_format,
    verify_api_key,
)
from app.database.database import get_db
from app.database.models import APIKey, UsageLog, User
from app.main import create_app

PROTECTED = "/v1/_test/protected"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def auth_client(
    settings: Settings, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[httpx.AsyncClient]:
    """App with a test-only protected route; the production app exposes no dummy endpoints."""
    app = create_app(settings)

    @app.get(PROTECTED)
    async def protected(auth: tuple[APIKey, User] = Depends(get_current_api_key)) -> dict[str, str]:
        api_key, user = auth
        return {"api_key_id": api_key.id, "user_id": user.id}

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        yield client


async def create_key(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    email: str = "dev@example.com",
    user_active: bool = True,
    key_active: bool = True,
    expires_at: datetime | None = None,
) -> tuple[str, APIKey, User]:
    raw_key, key_hash = generate_api_key()
    async with sessionmaker() as session:
        user = User(email=email, is_active=user_active)
        session.add(user)
        await session.flush()
        api_key = APIKey(
            user_id=user.id,
            name="test key",
            key_hash=key_hash,
            prefix=api_key_display_prefix(raw_key),
            is_active=key_active,
            expires_at=expires_at,
        )
        session.add(api_key)
        await session.commit()
    return raw_key, api_key, user


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Key generation / hashing
# ---------------------------------------------------------------------------


def test_api_key_generation_and_hashing() -> None:
    raw_key, key_hash = generate_api_key()
    assert raw_key.startswith("sk_live_")
    assert len(raw_key) == len(API_KEY_PREFIX) + 32
    assert len(key_hash) == 64
    assert all(c in "0123456789abcdef" for c in key_hash)
    assert key_hash == hash_api_key(raw_key)
    assert raw_key not in key_hash
    assert is_valid_api_key_format(raw_key)
    assert verify_api_key(raw_key, key_hash)


def test_generated_keys_are_unique() -> None:
    assert len({generate_api_key()[0] for _ in range(100)}) == 100


def test_verify_rejects_other_keys() -> None:
    _, key_hash = generate_api_key()
    other, _ = generate_api_key()
    assert not verify_api_key(other, key_hash)


def test_display_prefix_reveals_only_a_few_characters() -> None:
    raw_key, _ = generate_api_key()
    prefix = api_key_display_prefix(raw_key)
    assert prefix == raw_key[:12]
    assert prefix.startswith("sk_live_")


@pytest.mark.parametrize(
    "candidate",
    [
        "",
        "sk_live_",
        "sk_live_fake",
        "sk_test_" + "A" * 32,
        "sk_live_" + "A" * 31,
        "sk_live_" + "A" * 33,
        "sk_live_" + "A" * 31 + "!",
        "sk_live_" + "A" * 32 + "\n",
        "x" * 1000,
    ],
)
def test_malformed_keys_are_rejected(candidate: str) -> None:
    assert not is_valid_api_key_format(candidate)


@pytest.mark.parametrize(
    ("expires_at", "expected"),
    [
        (None, False),
        (datetime.now(UTC) + timedelta(days=1), False),
        (datetime.now(UTC) - timedelta(seconds=1), True),
        (datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1), True),  # naive = UTC
        (datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=1), False),
    ],
)
def test_is_api_key_expired(expires_at: datetime | None, expected: bool) -> None:
    assert is_api_key_expired(expires_at) is expected


# ---------------------------------------------------------------------------
# Authentication dependency
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_unauthorized_access(auth_client: httpx.AsyncClient) -> None:
    response = await auth_client.get(PROTECTED)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.anyio
async def test_invalid_api_key(auth_client: httpx.AsyncClient) -> None:
    response = await auth_client.get(PROTECTED, headers=bearer("sk_live_fake"))
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Invalid API key"


@pytest.mark.anyio
@pytest.mark.parametrize("header", ["Basic dXNlcjpwYXNz", "Bearer", "Bearer    ", "Token sk_live_" + "A" * 32])
async def test_malformed_authorization_header(auth_client: httpx.AsyncClient, header: str) -> None:
    response = await auth_client.get(PROTECTED, headers={"Authorization": header})
    assert response.status_code == 401


@pytest.mark.anyio
async def test_well_formed_but_unknown_key(auth_client: httpx.AsyncClient) -> None:
    unknown, _ = generate_api_key()
    response = await auth_client.get(PROTECTED, headers=bearer(unknown))
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Invalid API key"


@pytest.mark.anyio
async def test_valid_key_authenticates(
    auth_client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    raw_key, api_key, user = await create_key(db_sessionmaker)
    response = await auth_client.get(PROTECTED, headers=bearer(raw_key))
    assert response.status_code == 200
    assert response.json() == {"api_key_id": api_key.id, "user_id": user.id}


@pytest.mark.anyio
async def test_bearer_scheme_is_case_insensitive(
    auth_client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    raw_key, _, _ = await create_key(db_sessionmaker)
    response = await auth_client.get(PROTECTED, headers={"Authorization": f"bearer {raw_key}"})
    assert response.status_code == 200


@pytest.mark.anyio
async def test_key_with_future_expiry_is_accepted(
    auth_client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    raw_key, _, _ = await create_key(db_sessionmaker, expires_at=datetime.now(UTC) + timedelta(days=30))
    response = await auth_client.get(PROTECTED, headers=bearer(raw_key))
    assert response.status_code == 200


@pytest.mark.anyio
async def test_revoked_key_returns_401(
    auth_client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    raw_key, _, _ = await create_key(db_sessionmaker, key_active=False)
    response = await auth_client.get(PROTECTED, headers=bearer(raw_key))
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "API key has been revoked"


@pytest.mark.anyio
async def test_expired_key_returns_403(
    auth_client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    raw_key, _, _ = await create_key(db_sessionmaker, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    response = await auth_client.get(PROTECTED, headers=bearer(raw_key))
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "API key has expired"


@pytest.mark.anyio
async def test_inactive_user_returns_403(
    auth_client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    raw_key, _, _ = await create_key(db_sessionmaker, user_active=False)
    response = await auth_client.get(PROTECTED, headers=bearer(raw_key))
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "User account is inactive"


# ---------------------------------------------------------------------------
# Models / persistence
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_raw_key_is_never_persisted(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    raw_key, _, _ = await create_key(db_sessionmaker)
    async with db_sessionmaker() as session:
        stored = (await session.execute(select(APIKey))).scalar_one()
    assert stored.key_hash == hash_api_key(raw_key)
    assert raw_key not in (stored.key_hash, stored.prefix, stored.name)
    assert stored.prefix == raw_key[:12]
    assert "key_hash" not in repr(stored)


@pytest.mark.anyio
async def test_model_defaults(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    _, api_key, user = await create_key(db_sessionmaker)
    assert len(user.id) == 36 and len(api_key.id) == 36
    assert user.created_at.tzinfo is not None
    assert api_key.is_active is True


@pytest.mark.anyio
async def test_email_is_unique(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    await create_key(db_sessionmaker, email="same@example.com")
    with pytest.raises(IntegrityError):
        await create_key(db_sessionmaker, email="same@example.com")


@pytest.mark.anyio
async def test_usage_log_round_trip_and_status_constraint(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _, api_key, user = await create_key(db_sessionmaker)
    async with db_sessionmaker() as session:
        session.add(
            UsageLog(
                user_id=user.id,
                api_key_id=api_key.id,
                request_id="req-12345678",
                model="qwen2.5-vl-7b",
                input_tokens=120,
                output_tokens=40,
                image_count=1,
                status="success",
                processing_time_ms=850,
            )
        )
        await session.commit()
        log = (await session.execute(select(UsageLog))).scalar_one()
        assert (log.input_tokens, log.output_tokens, log.error_message) == (120, 40, None)

    async with db_sessionmaker() as session:
        session.add(
            UsageLog(
                user_id=user.id,
                api_key_id=api_key.id,
                request_id="req-87654321",
                model="qwen2.5-vl-7b",
                status="pending",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
