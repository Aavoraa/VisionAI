"""Create a dashboard administrator, or promote an existing user to administrator.

Run from the project root (the directory containing ``app/``) after the tables exist:

    python scripts/create_admin.py --email admin@example.com

The password is read from the ``VISION_ADMIN_PASSWORD`` environment variable when set
(for unattended provisioning), otherwise prompted for twice without echo. It is never
accepted as a command-line argument, which would leak it into shell history and process
listings.

For an existing account the role becomes ``admin`` and the account is re-activated; its
password is replaced only when one is supplied (prompting can be skipped with
``--keep-password`` if the user already has one).
"""

import argparse
import asyncio
import getpass
import os
import sys
from collections.abc import Sequence
from pathlib import Path

if not __package__:
    # Executed as a file: put the project root (not scripts/) on sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker  # noqa: E402

from app.core.security import hash_password, normalize_email, validate_password  # noqa: E402
from app.database.database import build_sessionmaker  # noqa: E402
from app.database.models import User  # noqa: E402

PASSWORD_ENV_VAR = "VISION_ADMIN_PASSWORD"


async def create_or_promote_admin(
    sessionmaker: async_sessionmaker[AsyncSession], email: str, password: str | None
) -> tuple[User, bool]:
    """Returns ``(user, created)``. ``password=None`` keeps an existing user's password."""
    email = normalize_email(email)
    password_hash = await asyncio.to_thread(hash_password, validate_password(password)) if password else None

    async with sessionmaker() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        created = user is None
        if user is None:
            if password_hash is None:
                raise ValueError("a password is required to create a new administrator")
            user = User(email=email, role="admin", password_hash=password_hash)
            session.add(user)
        else:
            if password_hash is None and user.password_hash is None:
                raise ValueError(f"{email} has no password yet; supply one to promote it")
            user.role = "admin"
            user.is_active = True
            if password_hash is not None:
                user.password_hash = password_hash
        await session.commit()
    return user, created


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/create_admin.py",
        description="Create a dashboard administrator or promote an existing user.",
        epilog=f"The password comes from ${PASSWORD_ENV_VAR} or an interactive prompt, never from arguments.",
    )
    parser.add_argument("--email", required=True, help="administrator email")
    parser.add_argument(
        "--keep-password",
        action="store_true",
        help="promote an existing user without changing their password (no prompt)",
    )
    return parser


def read_password() -> str:
    from_env = os.environ.get(PASSWORD_ENV_VAR)
    if from_env:
        return from_env
    # Both ends must be a terminal: on Windows the null device reports isatty() == True for
    # stdin, and getpass would then block reading the console of a non-interactive job.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ValueError(f"no terminal to prompt on; set {PASSWORD_ENV_VAR}")
    first = getpass.getpass("New admin password: ")
    if first != getpass.getpass("Repeat password: "):
        raise ValueError("passwords do not match")
    return first


async def run(email: str, password: str | None, engine: AsyncEngine) -> int:
    try:
        user, created = await create_or_promote_admin(build_sessionmaker(engine), email, password)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()
    print(f"{'Created' if created else 'Promoted'} administrator {user.email} (id {user.id})")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        password = None if args.keep_password else read_password()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Imported here so --help works without a configured/reachable database.
    from app.database.database import engine

    return asyncio.run(run(args.email, password, engine))


if __name__ == "__main__":
    raise SystemExit(main())
