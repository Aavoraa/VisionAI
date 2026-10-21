import os
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import hash_api_key, is_valid_api_key_format
from app.database.models import APIKey, Base, User
from scripts.init_db import build_parser, create_api_key_for, create_tables, revoke_api_key, run

pytestmark = pytest.mark.anyio


@pytest.fixture
async def empty_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """A file-backed database with no tables, as on first deployment. File-backed because
    ``run()`` disposes its engine (like the real CLI), which would discard an in-memory DB."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'init.db'}")
    yield engine
    await engine.dispose()


async def table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return set(await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names()))


async def test_create_tables_builds_full_schema_and_is_idempotent(empty_engine: AsyncEngine) -> None:
    assert await table_names(empty_engine) == set()
    await create_tables(empty_engine)
    await create_tables(empty_engine)  # second run must be a no-op, not an error
    assert await table_names(empty_engine) == set(Base.metadata.tables)
    assert {"users", "api_keys", "usage_logs", "jobs"} <= await table_names(empty_engine)


async def test_create_key_creates_user_and_stores_only_the_hash(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    raw_key, api_key, created = await create_api_key_for(db_sessionmaker, "  Dev@Example.COM ", name="laptop")
    assert created is True
    assert is_valid_api_key_format(raw_key)
    async with db_sessionmaker() as session:
        stored = (await session.execute(select(APIKey))).scalar_one()
        user = (await session.execute(select(User))).scalar_one()
    assert user.email == "dev@example.com"
    assert stored.key_hash == hash_api_key(raw_key)
    assert stored.prefix == raw_key[:12]
    assert stored.name == "laptop"
    assert stored.expires_at is None
    assert api_key.id == stored.id


async def test_second_key_reuses_existing_user(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    _, first, created_first = await create_api_key_for(db_sessionmaker, "dev@example.com")
    _, second, created_second = await create_api_key_for(db_sessionmaker, "DEV@example.com")
    assert (created_first, created_second) == (True, False)
    assert first.user_id == second.user_id
    assert first.id != second.id


async def test_create_key_with_expiry(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    _, api_key, _ = await create_api_key_for(db_sessionmaker, "dev@example.com", expires_days=30)
    assert api_key.expires_at is not None
    assert timedelta(days=29) < api_key.expires_at - datetime.now(UTC) <= timedelta(days=30)


@pytest.mark.parametrize(
    ("email", "name", "expires_days"),
    [
        ("not-an-email", "default", None),
        ("a@b", "default", None),
        ("dev@example.com", "", None),
        ("dev@example.com", "x" * 101, None),
        ("dev@example.com", "default", 0),
        ("dev@example.com", "default", -5),
    ],
)
async def test_create_key_rejects_invalid_input(
    db_sessionmaker: async_sessionmaker[AsyncSession], email: str, name: str, expires_days: int | None
) -> None:
    with pytest.raises(ValueError):
        await create_api_key_for(db_sessionmaker, email, name=name, expires_days=expires_days)


async def test_create_key_refuses_inactive_user(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with db_sessionmaker() as session:
        session.add(User(email="gone@example.com", is_active=False))
        await session.commit()
    with pytest.raises(ValueError, match="inactive"):
        await create_api_key_for(db_sessionmaker, "gone@example.com")


async def test_revoke_key(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    _, api_key, _ = await create_api_key_for(db_sessionmaker, "dev@example.com")
    assert await revoke_api_key(db_sessionmaker, api_key.id) is True
    assert await revoke_api_key(db_sessionmaker, api_key.id) is False  # already revoked
    assert await revoke_api_key(db_sessionmaker, "00000000-0000-0000-0000-000000000000") is False
    async with db_sessionmaker() as session:
        assert (await session.get(APIKey, api_key.id)).is_active is False  # type: ignore[union-attr]


async def test_issued_key_authenticates_against_the_api(
    client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    raw_key, api_key, _ = await create_api_key_for(db_sessionmaker, "dev@example.com")
    job_url = "/v1/jobs/00000000-0000-0000-0000-000000000000"
    authorized = await client.get(job_url, headers={"Authorization": f"Bearer {raw_key}"})
    assert authorized.status_code == 404  # authenticated; the job simply does not exist

    await revoke_api_key(db_sessionmaker, api_key.id)
    revoked = await client.get(job_url, headers={"Authorization": f"Bearer {raw_key}"})
    assert revoked.status_code == 401


# --- CLI ---------------------------------------------------------------------


async def test_cli_create_tables_then_create_key(empty_engine: AsyncEngine, capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()
    assert await run(parser.parse_args(["create-tables"]), empty_engine) == 0
    assert "Tables are up to date." in capsys.readouterr().out

    assert await run(parser.parse_args(["create-key", "--email", "cli@example.com", "--name", "ci"]), empty_engine) == 0
    output = capsys.readouterr().out
    raw_key = output.strip().splitlines()[-1]
    assert is_valid_api_key_format(raw_key)
    assert "Created user cli@example.com" in output
    assert "Expires: never" in output


async def test_cli_reports_invalid_input(empty_engine: AsyncEngine, capsys: pytest.CaptureFixture[str]) -> None:
    await run(build_parser().parse_args(["create-tables"]), empty_engine)
    code = await run(build_parser().parse_args(["create-key", "--email", "nope"]), empty_engine)
    assert code == 2
    assert "invalid email" in capsys.readouterr().err


async def test_cli_revoke_unknown_key(empty_engine: AsyncEngine, capsys: pytest.CaptureFixture[str]) -> None:
    await run(build_parser().parse_args(["create-tables"]), empty_engine)
    code = await run(build_parser().parse_args(["revoke-key", "--key-id", "missing"]), empty_engine)
    assert code == 1
    assert "No active key" in capsys.readouterr().err


def test_cli_defaults_to_create_tables() -> None:
    assert build_parser().parse_args([]).command == "create-tables"


def test_cli_rejects_unknown_command() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["drop-everything"])


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_script(args: list[str], db_file: Path) -> subprocess.CompletedProcess[str]:
    """Run a script in a fresh interpreter, as a user would, against a SQLite file."""
    env = {
        **os.environ,
        "VISION_DATABASE_URL": f"sqlite+aiosqlite:///{db_file.as_posix()}",
        "PYTHONPATH": "",  # prove the scripts do not rely on an externally configured path
    }
    return subprocess.run(
        [sys.executable, *args], cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=60
    )


@pytest.mark.parametrize("invocation", [["scripts/init_db.py"], ["-m", "scripts.init_db"]])
def test_script_creates_tables_as_a_standalone_command(tmp_path: Path, invocation: list[str]) -> None:
    """End to end: `python scripts/init_db.py` (no arguments) in a fresh interpreter."""
    db_file = tmp_path / "fresh.db"
    completed = run_script(invocation, db_file)
    assert completed.returncode == 0, completed.stderr
    assert "Tables are up to date." in completed.stdout

    with sqlite3.connect(db_file) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == set(Base.metadata.tables)


def test_create_api_key_script_issues_a_working_key(tmp_path: Path) -> None:
    """End to end: init tables, then `python scripts/create_api_key.py --email ...`."""
    db_file = tmp_path / "keys.db"
    assert run_script(["scripts/init_db.py"], db_file).returncode == 0

    completed = run_script(
        ["scripts/create_api_key.py", "--email", "Script@Example.com", "--name", "laptop", "--expires-days", "7"],
        db_file,
    )
    assert completed.returncode == 0, completed.stderr
    raw_key = completed.stdout.strip().splitlines()[-1]
    assert is_valid_api_key_format(raw_key)
    assert "Created user script@example.com" in completed.stdout

    with sqlite3.connect(db_file) as conn:
        stored_hash, name, expires_at = conn.execute("SELECT key_hash, name, expires_at FROM api_keys").fetchone()
    assert stored_hash == hash_api_key(raw_key)
    assert raw_key not in (stored_hash, name)
    assert name == "laptop"
    assert expires_at is not None


def test_create_api_key_script_rejects_bad_email(tmp_path: Path) -> None:
    db_file = tmp_path / "keys.db"
    run_script(["scripts/init_db.py"], db_file)
    completed = run_script(["scripts/create_api_key.py", "--email", "not-an-email"], db_file)
    assert completed.returncode == 2
    assert "invalid email" in completed.stderr


def test_create_api_key_script_without_arguments_uses_default_user(tmp_path: Path) -> None:
    """`python scripts/create_api_key.py` alone: default user created once, then reused."""
    from scripts.create_api_key import DEFAULT_USER_EMAIL

    db_file = tmp_path / "keys.db"
    assert run_script(["scripts/init_db.py"], db_file).returncode == 0

    first = run_script(["scripts/create_api_key.py"], db_file)
    second = run_script(["scripts/create_api_key.py"], db_file)
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert f"Created user {DEFAULT_USER_EMAIL}" in first.stdout
    assert "Created user" not in second.stdout  # existing default user is reused

    keys = [completed.stdout.strip().splitlines()[-1] for completed in (first, second)]
    assert keys[0] != keys[1]
    assert all(is_valid_api_key_format(key) for key in keys)
    with sqlite3.connect(db_file) as conn:
        users = conn.execute("SELECT email FROM users").fetchall()
        hashes = {row[0] for row in conn.execute("SELECT key_hash FROM api_keys")}
    assert users == [(DEFAULT_USER_EMAIL,)]
    assert hashes == {hash_api_key(key) for key in keys}


def test_export_openapi_script_writes_the_schema(tmp_path: Path) -> None:
    """The dashboard's TypeScript types are generated from this file."""
    import json

    destination = tmp_path / "nested" / "openapi.json"
    completed = run_script(["scripts/export_openapi.py", str(destination)], tmp_path / "unused.db")
    assert completed.returncode == 0, completed.stderr
    schema = json.loads(destination.read_text(encoding="utf-8"))
    assert schema["openapi"].startswith("3.")
    assert {"/v1/vision", "/v1/auth/login", "/v1/dashboard/billing"} <= set(schema["paths"])
