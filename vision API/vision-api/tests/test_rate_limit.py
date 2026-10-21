import io
import types
from typing import Any

import fakeredis
import pytest
from PIL import Image
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import main as main_module
from app.core import rate_limit
from app.core.config import Settings
from app.core.rate_limit import check_rate_limit, consume_rate_limit
from app.database.models import UsageLog
from app.models.base import ModelOutput
from app.models.local_transformers import MockVisionModel
from conftest import ClientFactory, create_api_key

pytestmark = pytest.mark.anyio

VISION = "/v1/vision"


def png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(buffer, format="PNG")
    return buffer.getvalue()


def upload() -> dict[str, Any]:
    return {"data": {"prompt": "describe"}, "files": [("images", ("a.png", png_bytes(), "image/png"))]}


class CountingModel(MockVisionModel):
    """Mock model that records whether inference was reached."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def analyze(self, images: list[bytes], prompt: str, options: dict[str, Any] | None = None) -> ModelOutput:
        self.calls += 1
        return await super().analyze(images, prompt, options)


class StubPipeline:
    def __init__(self, count: int | None, error: Exception | None) -> None:
        self._count = count
        self._error = error

    async def __aenter__(self) -> "StubPipeline":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def incr(self, key: str) -> None:
        return None

    def expire(self, key: str, seconds: int) -> None:
        return None

    async def execute(self) -> list[Any]:
        if self._error is not None:
            raise self._error
        return [self._count, True]


class StubRedis:
    """Redis double: reports a fixed counter value, or fails every command."""

    def __init__(self, count: int | None = None, error: Exception | None = None) -> None:
        self._count = count
        self._error = error
        self.pipelines = 0

    def pipeline(self, transaction: bool = True) -> StubPipeline:
        self.pipelines += 1
        return StubPipeline(self._count, self._error)

    async def ping(self) -> bool:
        if self._error is not None:
            raise self._error
        return True

    async def aclose(self) -> None:
        return None


def use_redis(monkeypatch: pytest.MonkeyPatch, redis: object) -> None:
    monkeypatch.setattr(main_module, "create_redis_client", lambda _settings: redis)


async def usage_rows(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    async with sessionmaker() as session:
        return (await session.execute(select(func.count()).select_from(UsageLog))).scalar_one()


# ===========================================================================
# check_rate_limit / consume_rate_limit (in-memory Redis)
# ===========================================================================


@pytest.fixture
def redis() -> fakeredis.FakeAsyncRedis:
    return fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer())


async def test_allows_requests_up_to_the_limit(redis: fakeredis.FakeAsyncRedis) -> None:
    results = [await check_rate_limit(redis, "user-1", limit=3, window_seconds=60) for _ in range(5)]
    assert results == [True, True, True, False, False]


async def test_limits_are_per_user(redis: fakeredis.FakeAsyncRedis) -> None:
    assert await check_rate_limit(redis, "user-a", limit=1)
    assert not await check_rate_limit(redis, "user-a", limit=1)
    assert await check_rate_limit(redis, "user-b", limit=1)


async def test_new_window_resets_the_count(redis: fakeredis.FakeAsyncRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(rate_limit, "time", types.SimpleNamespace(time=lambda: clock["now"]))

    assert await check_rate_limit(redis, "user-1", limit=1, window_seconds=60)
    assert not await check_rate_limit(redis, "user-1", limit=1, window_seconds=60)
    clock["now"] += 60
    assert await check_rate_limit(redis, "user-1", limit=1, window_seconds=60)


async def test_result_reports_remaining_and_reset(
    redis: fakeredis.FakeAsyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1_000_020 is a multiple of 60, so this is 20 s into a 60 s window.
    monkeypatch.setattr(rate_limit, "time", types.SimpleNamespace(time=lambda: 1_000_040.0))
    first = await consume_rate_limit(redis, "user-1", limit=5, window_seconds=60)
    assert (first.allowed, first.limit, first.remaining, first.reset_after) == (True, 5, 4, 40)


async def test_counter_keys_always_expire(redis: fakeredis.FakeAsyncRedis) -> None:
    await check_rate_limit(redis, "user-1", limit=5, window_seconds=60)
    keys = await redis.keys("ratelimit:*")
    assert len(keys) == 1
    ttl = await redis.ttl(keys[0])
    assert 0 < ttl <= 61


@pytest.mark.parametrize(("limit", "window"), [(0, 60), (5, 0), (-1, 60)])
async def test_invalid_parameters_are_rejected(redis: fakeredis.FakeAsyncRedis, limit: int, window: int) -> None:
    with pytest.raises(ValueError):
        await check_rate_limit(redis, "user-1", limit=limit, window_seconds=window)


# ===========================================================================
# get_rate_limiter dependency on POST /v1/vision
# ===========================================================================


async def test_rate_limit_exceeded(
    settings: Settings,
    make_client: ClientFactory,
    auth_headers: dict[str, str],
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Redis reports this user is already past the limit for the current window.
    use_redis(monkeypatch, StubRedis(count=settings.rate_limit_requests + 1))
    model = CountingModel()
    async with make_client(settings, model=model) as client:
        response = await client.post(VISION, headers=auth_headers, **upload())

    assert response.status_code == 429
    body = response.json()
    assert body["error"] == {"type": "rate_limit_error", "message": "Rate limit exceeded", "param": None}
    assert int(response.headers["retry-after"]) >= 1
    assert response.headers["x-ratelimit-limit"] == str(settings.rate_limit_requests)
    assert response.headers["x-ratelimit-remaining"] == "0"
    assert model.calls == 0  # rejected before any image processing or inference
    assert await usage_rows(db_sessionmaker) == 0  # rate-limited requests are not billed


async def test_limit_is_enforced_across_real_requests(
    settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str]
) -> None:
    tight = settings.model_copy(update={"rate_limit_requests": 2})
    async with make_client(tight) as client:
        statuses = [(await client.post(VISION, headers=auth_headers, **upload())).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]


async def test_successful_responses_carry_rate_limit_headers(
    settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str]
) -> None:
    async with make_client(settings.model_copy(update={"rate_limit_requests": 5})) as client:
        first = await client.post(VISION, headers=auth_headers, **upload())
        second = await client.post(VISION, headers=auth_headers, **upload())
    assert first.status_code == 200
    assert first.headers["x-ratelimit-limit"] == "5"
    assert first.headers["x-ratelimit-remaining"] == "4"
    assert second.headers["x-ratelimit-remaining"] == "3"


async def test_limits_are_isolated_between_users(
    settings: Settings,
    make_client: ClientFactory,
    auth_headers: dict[str, str],
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    other_headers, _, _ = await create_api_key(db_sessionmaker, "other@example.com")
    async with make_client(settings.model_copy(update={"rate_limit_requests": 1})) as client:
        assert (await client.post(VISION, headers=auth_headers, **upload())).status_code == 200
        assert (await client.post(VISION, headers=auth_headers, **upload())).status_code == 429
        assert (await client.post(VISION, headers=other_headers, **upload())).status_code == 200


async def test_unauthenticated_requests_do_not_consume_quota(
    settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str]
) -> None:
    async with make_client(settings.model_copy(update={"rate_limit_requests": 1})) as client:
        for _ in range(3):
            bad = await client.post(VISION, headers={"Authorization": "Bearer sk_live_fake"}, **upload())
            assert bad.status_code == 401
        assert (await client.post(VISION, headers=auth_headers, **upload())).status_code == 200


async def test_redis_outage_fails_open_by_default(
    settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    use_redis(monkeypatch, StubRedis(error=RedisConnectionError("connection refused")))
    async with make_client(settings) as client:
        response = await client.post(VISION, headers=auth_headers, **upload())
    assert response.status_code == 200
    assert "x-ratelimit-limit" not in response.headers


async def test_redis_outage_can_fail_closed(
    settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    use_redis(monkeypatch, StubRedis(error=RedisConnectionError("connection refused")))
    async with make_client(settings.model_copy(update={"rate_limit_fail_open": False})) as client:
        response = await client.post(VISION, headers=auth_headers, **upload())
    assert response.status_code == 503
    assert response.json()["error"]["message"] == "Rate limiting is temporarily unavailable"


async def test_disabled_rate_limiting_never_touches_redis(
    settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = StubRedis(count=10**6)
    use_redis(monkeypatch, stub)
    async with make_client(settings.model_copy(update={"rate_limit_enabled": False})) as client:
        response = await client.post(VISION, headers=auth_headers, **upload())
    assert response.status_code == 200
    assert stub.pipelines == 0


async def test_async_submissions_are_rate_limited_too(
    settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str]
) -> None:
    async with make_client(settings.model_copy(update={"rate_limit_requests": 1})) as client:
        data = {"prompt": "describe", "mode": "async"}
        files = [("images", ("a.png", png_bytes(), "image/png"))]
        assert (await client.post(VISION, headers=auth_headers, data=data, files=files)).status_code == 202
        assert (await client.post(VISION, headers=auth_headers, data=data, files=files)).status_code == 429
