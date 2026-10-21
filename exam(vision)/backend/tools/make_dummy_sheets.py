"""Render the demo students' answer sheets as handwritten-style scans.

    python -m tools.make_dummy_sheets            # writes ../sample-answer-sheets/

Each student gets a PNG per page plus a PDF of the whole sheet, named with the admission
number (A1001.pdf) so they also work with Bulk upload. ground_truth.json holds the exact
text and the teacher's expected marks, for benchmarking.
"""

from __future__ import annotations

import json
import random
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .demo_data import EXAM, QUESTIONS, STUDENTS

OUT = Path(__file__).resolve().parents[2] / "sample-answer-sheets"
FONTS = Path("C:/Windows/Fonts")
W, H = 1240, 1754  # A4 at 150 dpi
LINE = 52
INK = (25, 40, 110)


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    for candidate in (name, "Inkfree.ttf", "segoepr.ttf", "comic.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(str(FONTS / candidate), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _paper(rng: random.Random) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W, H), (252, 252, 248))
    d = ImageDraw.Draw(img)
    for y in range(230, H - 60, LINE):
        d.line((60, y, W - 60, y), fill=(190, 205, 230), width=1)
    d.line((140, 200, 140, H - 60), fill=(230, 160, 160), width=2)
    return img, d


def _header(d: ImageDraw.ImageDraw, student: dict, hand: ImageFont.FreeTypeFont, page: int, pages: int) -> None:
    printed = _font("arial.ttf", 26)
    small = _font("arial.ttf", 20)
    d.text((W // 2, 40), "Green Valley Public School", font=_font("arialbd.ttf", 30), fill="black", anchor="mm")
    d.text((W // 2, 80), f"{EXAM['name'].replace(' (demo)', '')} · Class {EXAM['class_name']}", font=printed,
           fill="black", anchor="mm")
    d.text((80, 125), "Name:", font=small, fill="black")
    d.text((160, 112), student["name"], font=hand, fill=INK)
    d.text((620, 125), "Roll No:", font=small, fill="black")
    d.text((720, 112), student["roll_no"], font=hand, fill=INK)
    d.text((900, 125), f"Page {page} of {pages}", font=small, fill="black")
    d.line((60, 180, W - 60, 180), fill="black", width=2)


def render(student: dict, seed: int) -> list[Image.Image]:
    rng = random.Random(seed)
    hand = _font(student["font"], 34)
    # lines: (text, is_marker_line)
    lines: list[str] = []
    for q in QUESTIONS:
        ans = student["answers"].get(q["number"])
        if ans is None:
            continue
        first = True
        for para in ans.split("\n"):
            for chunk in textwrap.wrap(para, 52) or [""]:
                lines.append((f"Q{q['number']}. " if first else "      ") + chunk)
                first = False
        lines.append("")
    per_page = (H - 300) // LINE
    pages_needed = student.get("pages", 1)
    if pages_needed > 1:  # split at a question boundary near the middle
        marks = [i for i, ln in enumerate(lines) if ln.startswith("Q")]
        cut = min(marks, key=lambda i: abs(i - len(lines) / 2))
        chunks = [lines[:cut], lines[cut:]]
    else:
        chunks = [lines[i:i + per_page] for i in range(0, len(lines), per_page)] or [[]]
    images = []
    for page_no, chunk in enumerate(chunks, start=1):
        img, d = _paper(rng)
        _header(d, student, hand, page_no, len(chunks))
        y = 230 - 40
        for text in chunk:
            x = 160 + rng.randint(-6, 10)
            d.text((x, y + rng.randint(-3, 3)), text, font=hand, fill=INK)
            y += LINE
        img = img.rotate(rng.uniform(-0.8, 0.8), resample=Image.BICUBIC, fillcolor=(245, 245, 240))
        img = img.filter(ImageFilter.GaussianBlur(0.4))  # a little scanner softness
        images.append(img)
    return images


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    truth = {"exam": EXAM, "questions": [{k: v for k, v in q.items() if k != "rubric"} for q in QUESTIONS],
             "students": []}
    for i, st in enumerate(STUDENTS):
        pages = render(st, seed=100 + i)
        for n, img in enumerate(pages, start=1):
            img.save(OUT / f"{st['admission_no']}_page{n}.png")
        pages[0].save(OUT / f"{st['admission_no']}.pdf", save_all=True, append_images=pages[1:], resolution=150)
        truth["students"].append({k: st[k] for k in ("admission_no", "name", "roll_no", "answers", "expected_marks")}
                                 | {"pages": len(pages)})
        print(f"{st['admission_no']} {st['name']}: {len(pages)} page(s)")
    (OUT / "ground_truth.json").write_text(json.dumps(truth, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "students.csv").write_text("admission_no,name,class_name,section,roll_no\n" + "".join(
        f"{s['admission_no']},{s['name']},{EXAM['class_name']},{EXAM['section']},{s['roll_no']}\n" for s in STUDENTS),
        encoding="utf-8")
    print(f"written to {OUT}")


if __name__ == "__main__":
    main()
