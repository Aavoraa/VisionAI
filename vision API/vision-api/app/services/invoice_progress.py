"""Live progress of an invoice extraction: short comments the pipeline writes as it works.

A client that wants to show progress sends ``X-Progress-Id: <random id>`` with
``POST /v1/invoices/extract`` and polls ``GET /v1/invoices/progress/{id}`` with the same API
key's account. Comments live in this process's memory only (never stored), expire after an
hour, and are capped per extraction. Each comment is::

    {"t": 12.3, "stage": "bands", "text": "Reading item rows, band 2 of 4", "level": "info",
     "page": 1, "region": [0.0, 0.41, 1.0, 0.56]}

``region`` (fractions of the page: x1, y1, x2, y2) says where on the page the step is looking,
so a client can highlight it; ``level`` is ``info``, ``ok`` (a check passed, a value confirmed)
or ``warn`` (something to review).
"""

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

ProgressFn = Callable[..., None]

PROGRESS_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
TTL_SECONDS = 3600.0
MAX_EVENTS = 400
MAX_CHANNELS = 1000


@dataclass
class _Channel:
    user_id: str
    started: float = field(default_factory=time.monotonic)
    touched: float = field(default_factory=time.monotonic)
    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False


class ProgressHub:
    """In-memory progress channels, keyed by the client's progress id (thread-safe)."""

    def __init__(self) -> None:
        self._channels: dict[str, _Channel] = {}
        self._lock = threading.Lock()

    def open(self, progress_id: str, user_id: str) -> ProgressFn | None:
        """A reporter for a new extraction, or None if the id is malformed or taken by another user."""
        if not PROGRESS_ID.match(progress_id):
            return None
        with self._lock:
            self._expire()
            existing = self._channels.get(progress_id)
            if existing is not None and existing.user_id != user_id:
                return None
            channel = _Channel(user_id=user_id)
            self._channels[progress_id] = channel

        def report(stage: str, text: str, *, level: str = "info", page: int | None = None,
                   region: tuple[float, float, float, float] | None = None, done: bool = False) -> None:
            with self._lock:
                channel.touched = time.monotonic()
                channel.done = channel.done or done
                if len(channel.events) < MAX_EVENTS:
                    channel.events.append({
                        "t": round(time.monotonic() - channel.started, 1), "stage": stage, "text": text,
                        "level": level, "page": page,
                        "region": [round(v, 4) for v in region] if region else None,
                    })

        return report

    def read(self, progress_id: str, user_id: str, after: int = 0) -> dict[str, Any] | None:
        """Comments after the first ``after`` ones; None if unknown or someone else's."""
        with self._lock:
            channel = self._channels.get(progress_id)
            if channel is None or channel.user_id != user_id:
                return None
            return {"events": channel.events[max(0, after):], "count": len(channel.events), "done": channel.done}

    def _expire(self) -> None:
        now = time.monotonic()
        for key in [k for k, c in self._channels.items() if now - c.touched > TTL_SECONDS]:
            del self._channels[key]
        while len(self._channels) >= MAX_CHANNELS:
            del self._channels[min(self._channels, key=lambda k: self._channels[k].touched)]


def silent(*_args: Any, **_kwargs: Any) -> None:
    """The reporter used when nobody is listening."""
