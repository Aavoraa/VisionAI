"""Delete the uploaded files and page images of old invoices (the extracted data is kept).

The API already does this every 6 hours when ``VISION_INVOICE_FILE_RETENTION_DAYS`` is set;
use this script for a one-off clean-up or to run it from cron / Task Scheduler instead.
Run from the project root:

    python scripts/purge_invoice_files.py --days 90 --dry-run   # list what would go
    python scripts/purge_invoice_files.py --days 90
    python scripts/purge_invoice_files.py                       # uses VISION_INVOICE_FILE_RETENTION_DAYS
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.database.database import AsyncSessionLocal, engine  # noqa: E402
from app.services.document_store import DocumentStore  # noqa: E402
from app.services.invoice_integration import purge_expired_files  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python scripts/purge_invoice_files.py", description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, help="delete files of invoices older than this (default: the setting)")
    parser.add_argument("--dry-run", action="store_true", help="only list the invoices whose files would be deleted")
    return parser


async def run(days: int, dry_run: bool) -> list[str]:
    settings = get_settings()
    try:
        return await purge_expired_files(AsyncSessionLocal, DocumentStore(settings.document_storage_dir), days, dry_run=dry_run)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    days = args.days or get_settings().invoice_file_retention_days
    if not days or days < 1:
        print("error: give --days N or set VISION_INVOICE_FILE_RETENTION_DAYS", file=sys.stderr)
        return 2
    ids = asyncio.run(run(days, args.dry_run))
    verb = "Would delete" if args.dry_run else "Deleted"
    print(f"{verb} the files of {len(ids)} invoice(s) older than {days} days.")
    for invoice_id in ids:
        print(f"  {invoice_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
