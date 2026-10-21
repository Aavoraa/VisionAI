# backend/app/db.py
"""Engine, sessions and schema migrations (SQLite)."""

from __future__ import annotations

from collections.abc import Callable, Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import Settings, ensure_dirs, settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


def configure(s: Settings = settings) -> Engine:
    """(Re)create the engine for ``s`` and apply migrations."""
    global _engine, _SessionLocal
    ensure_dirs(s)
    _engine = create_engine(f"sqlite:///{s.db_path}", connect_args={"check_same_thread": False, "timeout": 30})
    event.listen(_engine, "connect", _sqlite_pragmas)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    migrate(_engine)
    return _engine


def engine() -> Engine:
    if _engine is None:
        configure()
    return _engine  # type: ignore[return-value]


def new_session() -> Session:
    if _SessionLocal is None:
        configure()
    return _SessionLocal()  # type: ignore[misc]


def get_db() -> Iterator[Session]:
    db = new_session()
    try:
        yield db
    finally:
        db.close()


# --- migrations --------------------------------------------------------------
# Each entry upgrades the schema by one version. Never edit an applied migration:
# append a new one. Version 1 creates every table from the models.

def _v1(conn) -> None:
    from . import models  # noqa: F401  (registers the tables)

    Base.metadata.create_all(conn)


def _v2(conn) -> None:
    # who wrote a rubric version: "teacher" or "ai_draft"
    cols = [r[1] for r in conn.execute(text("PRAGMA table_info(rubric_versions)"))]
    if "source" not in cols:
        conn.execute(text("ALTER TABLE rubric_versions ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT 'teacher'"))


MIGRATIONS: list[Callable] = [_v1, _v2]


def migrate(eng: Engine) -> int:
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"))
        row = conn.execute(text("SELECT version FROM schema_version")).fetchone()
        current = row[0] if row else 0
        for number, step in enumerate(MIGRATIONS, start=1):
            if number > current:
                step(conn)
        if row is None:
            conn.execute(text("INSERT INTO schema_version (version) VALUES (:v)"), {"v": len(MIGRATIONS)})
        elif current < len(MIGRATIONS):
            conn.execute(text("UPDATE schema_version SET version = :v"), {"v": len(MIGRATIONS)})
    return len(MIGRATIONS)
