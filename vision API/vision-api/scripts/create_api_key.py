"""Create a user (if needed) and issue an API key for them.

Run from the project root (the directory containing ``app/``) after the tables exist:

    python scripts/create_api_key.py                                  # default user
    python scripts/create_api_key.py --email you@example.com
    python scripts/create_api_key.py --email ci@example.com --name ci --expires-days 90

Steps: create the user (the default one unless ``--email`` is given; an existing user is
reused), generate a key with ``generate_api_key()``, store only its SHA-256 hash, and print
the raw key.

The raw key is printed exactly once. Only its SHA-256 hash is stored, so a lost key cannot
be recovered; revoke it (``python scripts/init_db.py revoke-key --key-id <id>``) and create
a new one.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

if not __package__:
    # Executed as a file: put the project root (not scripts/) on sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.init_db import run  # noqa: E402

DEFAULT_USER_EMAIL = "default@vision-api.local"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/create_api_key.py", description="Create a user (if needed) and issue an API key."
    )
    parser.add_argument(
        "--email",
        default=DEFAULT_USER_EMAIL,
        help="owner of the key; created if new (default: %(default)s)",
    )
    parser.add_argument("--name", default="default", help="label to identify the key (default: %(default)s)")
    parser.add_argument("--expires-days", type=int, default=None, help="expire after N days (default: never)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.command = "create-key"
    # Imported here so --help works without a configured/reachable database.
    from app.database.database import engine

    return asyncio.run(run(args, engine))


if __name__ == "__main__":
    raise SystemExit(main())
