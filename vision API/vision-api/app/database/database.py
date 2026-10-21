"""Async SQLAlchemy engine and session management."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, settings


def build_engine(config: Settings) -> AsyncEngine:
    url = config.database_url.get_secret_value()
    options: dict[str, object] = {"echo": config.database_echo, "pool_pre_ping": True}
    if not url.startswith("sqlite"):
        options.update(
            pool_size=config.database_pool_size,
            max_overflow=config.database_max_overflow,
            pool_recycle=1800,
        )
    return create_async_engine(url, **options)


def build_sessionmaker(bind: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: attribute access after commit would otherwise trigger implicit I/O.
    return async_sessionmaker(bind=bind, class_=AsyncSession, expire_on_commit=False, autoflush=False)


engine: AsyncEngine = build_engine(settings)
AsyncSessionLocal: async_sessionmaker[AsyncSession] = build_sessionmaker(engine)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """For work that outlives the request (background tasks), which must open its own session."""
    return AsyncSessionLocal


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield a session; roll back on error. The context manager always closes it."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
