# backend/app/providers/base.py
"""The common vision-provider interface and the normalised result both adapters return.

Fields a provider does not supply (OCR confidence, word coordinates) stay ``None``; they
are never invented. Downstream code must not care which provider produced a result.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol


class ProviderError(RuntimeError):
    """A provider call failed. ``kind``: not_configured|auth|unavailable|timeout|rate_limited|refused|bad_response."""

    def __init__(self, message: str, kind: str = "unavailable", retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


@dataclass
class PageExtraction:
    page_no: int
    text: str
    warnings: list[str] = field(default_factory=list)
    uncertain: list[str] = field(default_factory=list)  # words the provider marked as unsure
    confidence: float | None = None  # only if the provider reports one
    regions: list | None = None  # coordinates, only if the provider reports them
    blank: bool = False
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DocumentExtraction:
    provider: str
    model: str | None
    pages: list[PageExtraction]
    warnings: list[str] = field(default_factory=list)
    duration_ms: int = 0
    usage: dict = field(default_factory=dict)
    redacted: bool = False

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)


@dataclass
class HealthResult:
    ok: bool
    provider: str
    model: str | None = None
    detail: str = ""
    duration_ms: int = 0


class VisionProvider(Protocol):
    name: str

    def extract_page(self, image: bytes, page_no: int, options: dict | None = None) -> PageExtraction: ...

    def health_check(self) -> HealthResult: ...

    @property
    def model(self) -> str | None: ...


# Marks the edges of illegible or uncertain text in every provider's output, so the
# rest of the system has one convention: [?word?] uncertain, [illegible] unreadable.
UNCERTAIN_OPEN, UNCERTAIN_CLOSE, ILLEGIBLE = "[?", "?]", "[illegible]"
