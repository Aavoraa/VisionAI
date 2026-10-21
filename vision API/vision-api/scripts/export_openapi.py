"""Write the API's OpenAPI schema to a file (used to generate the dashboard's TypeScript types).

    python scripts/export_openapi.py ../dashboard/openapi.json

The schema is built from the code, not fetched from a running server, so no database, Redis
or model is needed.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

if not __package__:
    # Executed as a file: put the project root (not scripts/) on sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


def export(destination: Path) -> dict[str, object]:
    # Development settings so docs are enabled regardless of the local environment.
    schema = create_app(Settings(_env_file=None, environment="development")).openapi()  # type: ignore[call-arg]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return schema


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/export_openapi.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("destination", type=Path, help="output file, e.g. ../dashboard/openapi.json")
    args = parser.parse_args(argv)
    schema = export(args.destination)
    print(f"Wrote {len(schema['paths'])} paths to {args.destination}")  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
