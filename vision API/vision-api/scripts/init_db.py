"""Database bootstrap and API key management.

Run from the project root (the directory containing ``app/``):

    python scripts/init_db.py                  # create all tables (same as `create-tables`)
    python scripts/init_db.py create-key --email dev@example.com [--name laptop] [--expires-days 90]
    python scripts/init_db.py revoke-key --key-id <uuid>

``python -m scripts.init_db ...`` works identically.

``create-tables`` is idempotent (existing tables are left untouched). It creates missing
tables only; it does not alter existing ones, so schema changes to a live database need a
migration tool (e.g. Alembic).

``create-key`` prints the raw key exactly once. Only its SHA-256 hash is stored, so a lost
key cannot be recovered; revoke it and create a new one.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

if not __package__:
    # Executed as a file (`python scripts/init_db.py`): Python put scripts/ on sys.path,
    # not the project root, so make the `app` package importable.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker  # noqa: E402

from app.core.security import api_key_display_prefix, generate_api_key, normalize_email  # noqa: E402
from app.database.database import build_sessionmaker  # noqa: E402
from app.database.models import APIKey, Base, User  # noqa: E402
from app.services.billing_service import ensure_default_plan  # noqa: E402


async def create_tables(engine: AsyncEngine) -> None:
    """Create missing tables and make sure a default pricing plan exists."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with build_sessionmaker(engine)() as session:
        await ensure_default_plan(session)


async def create_api_key_for(
    sessionmaker: async_sessionmaker[AsyncSession],
    email: str,
    name: str = "default",
    expires_days: int | None = None,
) -> tuple[str, APIKey, bool]:
    """Create a key for ``email`` (creating the user if needed).

    Returns ``(raw_key, api_key_row, user_was_created)``.
    """
    try:
        email = normalize_email(email)
    except ValueError:
        raise ValueError(f"invalid email address: {email.strip()!r}") from None
    name = name.strip()
    if not 1 <= len(name) <= 100:
        raise ValueError("key name must be 1-100 characters")
    if expires_days is not None and expires_days <= 0:
        raise ValueError("expires_days must be positive")

    raw_key, key_hash = generate_api_key()
    async with sessionmaker() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        created = user is None
        if user is None:
            user = User(email=email)
            session.add(user)
            await session.flush()
        elif not user.is_active:
            raise ValueError(f"user {email!r} is inactive")

        api_key = APIKey(
            user_id=user.id,
            name=name,
            key_hash=key_hash,
            prefix=api_key_display_prefix(raw_key),
            expires_at=datetime.now(UTC) + timedelta(days=expires_days) if expires_days else None,
        )
        session.add(api_key)
        await session.commit()
    return raw_key, api_key, created


async def revoke_api_key(sessionmaker: async_sessionmaker[AsyncSession], key_id: str) -> bool:
    """Deactivate a key (rows are kept so usage history stays attributable)."""
    async with sessionmaker() as session:
        result = await session.execute(
            update(APIKey).where(APIKey.id == key_id, APIKey.is_active.is_(True)).values(is_active=False)
        )
        await session.commit()
    return result.rowcount == 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python scripts/init_db.py", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", metavar="{create-tables,create-key,revoke-key}")
    # No command given -> create-tables, so `python scripts/init_db.py` alone initialises the DB.
    # (Must follow add_subparsers: set_defaults only updates arguments that already exist.)
    parser.set_defaults(command="create-tables")

    commands.add_parser("create-tables", help="create all missing tables (idempotent)")

    create_key = commands.add_parser("create-key", help="issue an API key (creates the user if needed)")
    create_key.add_argument("--email", required=True)
    create_key.add_argument("--name", default="default", help="label to identify the key (default: %(default)s)")
    create_key.add_argument("--expires-days", type=int, default=None, help="expire after N days (default: never)")

    revoke = commands.add_parser("revoke-key", help="deactivate an API key")
    revoke.add_argument("--key-id", required=True)
    return parser


async def run(args: argparse.Namespace, engine: AsyncEngine) -> int:
    try:
        if args.command == "create-tables":
            await create_tables(engine)
            print("Tables are up to date.")
            return 0

        sessionmaker = build_sessionmaker(engine)
        if args.command == "create-key":
            raw_key, api_key, created = await create_api_key_for(
                sessionmaker, args.email, args.name, args.expires_days
            )
            if created:
                print(f"Created user {args.email.strip().lower()}")
            print(f"Key id:  {api_key.id}")
            print(f"Expires: {api_key.expires_at.isoformat() if api_key.expires_at else 'never'}")
            print("API key (shown only once; store it securely):")
            print(raw_key)
            return 0

        if await revoke_api_key(sessionmaker, args.key_id):
            print(f"Revoked key {args.key_id}")
            return 0
        print(f"No active key with id {args.key_id}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Imported here so --help works without a configured/reachable database.
    from app.database.database import engine

    return asyncio.run(run(args, engine))


if __name__ == "__main__":
    raise SystemExit(main())
