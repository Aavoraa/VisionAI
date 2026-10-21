"""Types shared by the dashboard schemas."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator


def _as_utc(value: datetime) -> datetime:
    # Always emit an explicit offset: browsers read offset-less ISO strings as *local* time.
    # (PostgreSQL returns aware datetimes; SQLite returns naive UTC ones.)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


UTCDateTime = Annotated[datetime, AfterValidator(_as_utc)]

# Path parameters holding row ids (UUID4 strings); validated before touching the database.
UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
