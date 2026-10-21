import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import fakeredis
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("VISION_ENVIRONMENT", "test")

from app import main as main_module  # noqa: E402
from app.api.dependencies import get_vision_service  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.security import api_key_display_prefix, generate_api_key  # noqa: E402
from app.database.database import build_sessionmaker, get_db, get_session_factory  # noqa: E402
from app.database.models import APIKey, Base, User  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.base import VisionModel  # noqa: E402
from app.services.vision_service import VisionService  # noqa: E402

ClientFactory = Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="test",
        database_url="postgresql+asyncpg://test:test@localhost:5432/vision_test",  # type: ignore[arg-type]
        document_storage_dir=str(tmp_path / "invoices"),  # stored invoices never land in the repo
    )


@pytest.fixture
def fake_redis_server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


@pytest.fixture(autouse=True)
def _isolated_redis(monkeypatch: pytest.MonkeyPatch, fake_redis_server: fakeredis.FakeServer) -> None:
    """No test may ever reach a real Redis: the lifespan gets a per-test in-memory server."""
    monkeypatch.setattr(
        main_module, "create_redis_client", lambda _settings: fakeredis.FakeAsyncRedis(server=fake_redis_server)
    )


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """Isolated in-memory SQLite database per test (schema is dialect-portable)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def db_sessionmaker(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return build_sessionmaker(db_engine)


async def create_api_key(
    sessionmaker: async_sessionmaker[AsyncSession], email: str
) -> tuple[dict[str, str], APIKey, User]:
    raw_key, key_hash = generate_api_key()
    async with sessionmaker() as session:
        user = User(email=email)
        session.add(user)
        await session.flush()
        api_key = APIKey(
            user_id=user.id, name="fixture key", key_hash=key_hash, prefix=api_key_display_prefix(raw_key)
        )
        session.add(api_key)
        await session.commit()
    return {"Authorization": f"Bearer {raw_key}"}, api_key, user


@pytest.fixture
async def auth_headers(db_sessionmaker: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    """Authorization header for a freshly created active user + API key."""
    headers, _, _ = await create_api_key(db_sessionmaker, "client@example.com")
    return headers


@pytest.fixture
def make_client(db_sessionmaker: async_sessionmaker[AsyncSession]) -> ClientFactory:
    """Build a test client wired to the per-test SQLite DB (request sessions *and* the
    session factory used by background tasks). ``model`` bypasses the lifespan-loaded one."""

    @asynccontextmanager
    async def factory(
        settings: Settings,
        *,
        model: VisionModel | None = None,
        run_lifespan: bool = True,
        overrides: dict[Callable[..., Any], Callable[..., Any]] | None = None,
    ) -> AsyncIterator[httpx.AsyncClient]:
        app = create_app(settings)

        async def override_get_db() -> AsyncIterator[AsyncSession]:
            async with db_sessionmaker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_session_factory] = lambda: db_sessionmaker
        if model is not None:
            app.dependency_overrides[get_vision_service] = lambda: VisionService(model, settings)
        app.dependency_overrides.update(overrides or {})

        async with AsyncExitStack() as stack:
            if run_lifespan:
                # ASGITransport does not emit lifespan events, so run startup/shutdown explicitly.
                await stack.enter_async_context(app.router.lifespan_context(app))
            client = await stack.enter_async_context(
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
            )
            yield client

    return factory


@pytest.fixture
async def client(settings: Settings, make_client: ClientFactory) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client(settings) as http_client:
        yield http_client
