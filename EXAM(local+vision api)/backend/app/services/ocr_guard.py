# backend/app/services/ocr_guard.py
"""Guards against a known vision-model failure: on blank or near-blank input it can
"hallucinate" long looping text (for example numbered Chinese headings). Such output is
removed, and the removal is reported as a warning so it is never silent.
"""

from __future__ import annotations

import re
from collections import Counter

_CJK = re.compile(r"[㐀-鿿豈-﫿]")
_LATIN = re.compile(r"[A-Za-z]")
_NUMERALS = re.compile(r"[\d一二三四五六七八九十百千零〇]+")


def _shape(line: str) -> str:
    """A line with its numbers removed: 卷一 / 卷二 / 卷三 all have the shape 卷."""
    return _NUMERALS.sub("#", line.strip().lower())


def clean(text: str) -> tuple[str, list[str]]:
    """Returns (cleaned_text, warnings)."""
    lines = text.splitlines()
    warnings: list[str] = []
    if not lines:
        return text, warnings

    # 1. a looping tail: one line shape repeated many times
    shapes = [_shape(ln) for ln in lines]
    counts = Counter(s for s in shapes if s and s != "#")
    loop = next((s for s, n in counts.most_common(1) if n >= 6), None)
    if loop is not None:
        first = shapes.index(loop)
        # also drop a short header just before the loop that belongs to it (same script)
        start = first
        while start > 0 and _CJK.search(lines[start - 1]) and not _LATIN.search(lines[start - 1]):
            start -= 1
        removed = len(lines) - start
        warnings.append(f"removed {removed} lines of repeated text produced by the reader (likely a blank area)")
        lines = lines[:start]

    # 2. lines in a script that does not occur anywhere else on a Latin-script page
    latin = sum(len(_LATIN.findall(ln)) for ln in lines)
    cjk_lines = [ln for ln in lines if _CJK.search(ln) and not _LATIN.search(ln)]
    if cjk_lines and latin >= 20 and sum(len(_CJK.findall(ln)) for ln in cjk_lines) < latin:
        lines = [ln for ln in lines if ln not in cjk_lines]
        warnings.append(f"removed {len(cjk_lines)} line(s) in an unexpected script: "
                        + " / ".join(cjk_lines[:3])[:120])
    return "\n".join(lines).strip(), warnings
