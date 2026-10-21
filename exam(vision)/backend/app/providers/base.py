# backend/app/providers/base.py
"""Results shared by the reader and the rest of the system.

Fields Claude does not supply (OCR confidence, word coordinates) are ``None``; they are
never invented.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


class ProviderError(RuntimeError):
    """A Claude call failed. ``kind``: not_configured|auth|unavailable|timeout|rate_limited|refused|bad_response."""

    def __init__(self, message: str, kind: str = "unavailable", retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


@dataclass
class PageText:
    page_no: int
    text: str
    warnings: list[str] = field(default_factory=list)
    uncertain: list[str] = field(default_factory=list)
    confidence: float | None = None  # Claude reports none
    blank: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SheetReading:
    """Claude's reading of a whole answer sheet."""

    model: str | None
    pages: list[PageText]
    answers: dict[str, dict]  # question number -> {"text", "pages", "uncertain"}
    unassigned_text: str = ""
    name_on_sheet: str | None = None
    warnings: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    duration_ms: int = 0


@dataclass
class HealthResult:
    ok: bool
    provider: str
    model: str | None = None
    detail: str = ""
    duration_ms: int = 0


# One convention for doubtful text everywhere: [?word?] uncertain, [illegible] unreadable.
UNCERTAIN_OPEN, UNCERTAIN_CLOSE, ILLEGIBLE = "[?", "?]", "[illegible]"
