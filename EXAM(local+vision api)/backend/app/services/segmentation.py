# backend/app/services/segmentation.py
"""Split a transcribed answer sheet into one answer per question.

Students mark answers as "Q1", "Q.1", "1.", "1)", "Ans 1", "Answer 1:", "2(a)", "2 a)"...
Text before the first marker (name, roll number, headings) is not an answer. Text under a
number the exam does not have is kept and flagged, never silently dropped. A teacher can
correct the result in the review screen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_MARKER = re.compile(
    r"""^\s*
    (?:Q(?:uestion|ues)?\s*\.?\s*(?:no\.?)?\s*|Ans(?:wer)?\s*\.?\s*(?:no\.?)?\s*)?   # optional Q / Ans prefix
    (?P<num>\d{1,2})\s*
    (?:\(?\s*(?P<sub>[a-hA-H]|i{1,3}|iv|v)\s*\))?                                 # optional (a) / a)
    \s*(?P<sep>[.):\-]|\s(?=[A-Za-z]))                                           # 1.  1)  1:  1-  or "1 Plants"
    """,
    re.VERBOSE | re.IGNORECASE,
)
_HEADER = re.compile(r"^\s*(?:name\s*[:.]|roll\s*(?:no|number)|page\s*\d+\s*(?:of|/)\s*\d+|class\s*[:.]|section\s*[:.]|"
                     r"adm(?:ission)?\.?\s*no)", re.IGNORECASE)


def _norm_line(line: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", line.lower()).strip()


_PREFIXED = re.compile(r"^\s*(?:Q(?:uestion|ues)?|Ans(?:wer)?)(?=\s*\.?\s*(?:no\.?)?\s*\d)", re.IGNORECASE)


@dataclass
class Segment:
    number: str
    text: str
    pages: list[int] = field(default_factory=list)


@dataclass
class Segmentation:
    answers: dict[str, Segment]
    unassigned: list[Segment]
    missing: list[str]
    preamble: str


def _key(number: str) -> str:
    return re.sub(r"[^0-9a-z]", "", number.lower())


_STOP = {"what", "is", "are", "the", "a", "an", "of", "in", "on", "for", "and", "or", "to", "used", "use", "how",
         "why", "do", "does", "write", "explain", "define", "give", "name", "any", "two", "three", "short", "note",
         "notes", "about", "with", "by", "its", "their", "your", "describe", "list", "mention", "some", "which"}
_HEADING_END = re.compile(r"[:!\-]+\s*$|:-\s*$")


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP]


def _similar(a: str, b: str) -> bool:
    import difflib

    return a == b or (len(a) > 3 and len(b) > 3 and difflib.SequenceMatcher(a=a, b=b).ratio() >= 0.75)


def _heading_match(line: str, candidates: dict[str, list[str]]) -> str | None:
    """The question whose topic this short heading line names ("Tools used for Load Generation",
    "Sales:-"), tolerating misreads. Only for questions not answered yet."""
    words = _words(line)
    if not words or len(line.split()) > 9:
        return None
    colon = bool(_HEADING_END.search(line.strip()))
    best, best_score = None, 0.0
    for number, keys in candidates.items():
        if not keys:
            continue
        hits = sum(1 for k in keys if any(_similar(k, w) for w in words))
        coverage = hits / len(keys)
        precision = sum(1 for w in words if any(_similar(k, w) for k in keys)) / len(words)
        if coverage >= 0.6 and precision >= 0.6 and (colon or hits >= 2) and coverage > best_score:
            best, best_score = number, coverage
    return best


def split_answers(pages: list[tuple[int, str]], question_numbers: list[str],
                  question_texts: dict[str, str] | None = None) -> Segmentation:
    keywords = {n: _words(t) for n, t in (question_texts or {}).items() if n in question_numbers}
    order = {n: i for i, n in enumerate(question_numbers)}
    wanted = {_key(n): n for n in question_numbers}
    mains = {re.match(r"\d+", n).group(0) for n in question_numbers if re.match(r"\d+", n)}
    answers: dict[str, Segment] = {}
    unassigned: list[Segment] = []
    preamble: list[str] = []
    current: Segment | None = None
    highest = 0

    header_lines: set[str] = set()
    for page_no, text in pages:
        at_page_top = True
        for line in text.splitlines():
            norm = _norm_line(line)
            if at_page_top and current is not None and (not norm or norm in header_lines or _HEADER.search(line)):
                # the printed header repeated on later pages (school, test name, Name / Roll No, Page x of y)
                if norm:
                    preamble.append(line)
                continue
            at_page_top = False
            match = _MARKER.match(line)
            accepted = False
            if match:
                num, sub = match.group("num"), (match.group("sub") or "").lower()
                prefixed = bool(_PREFIXED.match(line))
                key = _key(num + sub)
                plain_number = not prefixed and match.group("sep").strip() == ""
                # "Q3"/"Ans 3" markers are always accepted (students may answer out of order).
                # An unprefixed number only starts a question when it moves forward: a list
                # "1. absorb water 2. anchor the plant" inside answer 4 is part of answer 4.
                same_q_new_part = bool(sub) and int(num) == highest and current is not None \
                    and current.number != wanted.get(key, num + sub)
                if plain_number:
                    plausible = int(num) == highest + 1 or (highest == 0 and num in mains)
                else:
                    plausible = prefixed or int(num) > highest or same_q_new_part
                # a heading "Lead generation:-" then "1. Lead generation is ..." is one answer
                repeat_of_heading = current is not None and not current.text and wanted.get(key) == current.number
                plausible = plausible or repeat_of_heading
                if plausible and (key in wanted or num in mains or prefixed):
                    accepted = True
                    if key not in wanted and num in wanted and not sub:
                        key = num
                    number = wanted.get(key) or wanted.get(_key(num)) or (num + sub)
                    rest = line[match.end():].strip()
                    if number in answers:
                        current = answers[number]  # the student came back to this question
                        current.text += ("\n" if current.text else "") + rest
                    else:
                        current = Segment(number=number, text=rest, pages=[])
                        if _key(number) in wanted:
                            answers[number] = current
                        else:
                            unassigned.append(current)
                    highest = max(highest, int(num))
                    if page_no not in current.pages:
                        current.pages.append(page_no)
            if not accepted and keywords and not match:
                # a written topic heading instead of a number: only for unanswered questions after
                # the current one (or any unanswered one when the heading ends with ":")
                here = order.get(current.number, -1) if current else -1
                colon = bool(_HEADING_END.search(line.strip()))
                open_qs = {n: k for n, k in keywords.items() if n not in answers and (colon or order[n] > here)}
                hit = _heading_match(line, open_qs)
                if hit:
                    current = Segment(number=hit, text="", pages=[page_no])
                    answers[hit] = current
                    m = re.match(r"\d+", hit)
                    highest = max(highest, int(m.group(0))) if m else highest
                    accepted = True
            if accepted:
                continue
            if current is None:
                preamble.append(line)
                if norm:
                    header_lines.add(norm)
            else:
                current.text += ("\n" if current.text else "") + line
                if line.strip() and page_no not in current.pages:
                    current.pages.append(page_no)

    for seg in [*answers.values(), *unassigned]:
        seg.text = seg.text.strip()
    missing = [n for n in question_numbers if n not in answers]
    return Segmentation(answers=answers, unassigned=unassigned, missing=missing, preamble="\n".join(preamble).strip())
