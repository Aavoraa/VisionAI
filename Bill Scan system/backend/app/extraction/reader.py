# backend/app/extraction/reader.py
"""Picks the bill reader named by BILLS_EXTRACTOR (vision_api or claude)."""

from ..config import settings
from .claude_vision import ExtractionError

if settings.extractor == "claude":
    from .claude_vision import extract_bill
else:
    from .vision_api import extract_bill

__all__ = ["ExtractionError", "extract_bill"]
