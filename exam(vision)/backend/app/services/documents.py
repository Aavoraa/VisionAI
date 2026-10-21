# backend/app/services/documents.py
"""Answer-sheet files: validation, PDF -> page images, quality checks, rotation, redaction."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, ImageOps

PDF_MAGIC = b"%PDF"
IMAGE_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png"}
RENDER_DPI = 200
MAX_PAGE_EDGE = 3000


class DocumentError(ValueError):
    pass


@dataclass
class PageImage:
    image: Image.Image
    quality: dict


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def detect_type(data: bytes) -> str:
    """'pdf', 'image' or raise. Decided from the content, never the file name."""
    if data[:4] == PDF_MAGIC:
        return "pdf"
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format in IMAGE_TYPES:
                return "image"
    except Exception:  # noqa: BLE001 - Pillow raises many types
        pass
    raise DocumentError("Unsupported file. Upload a PDF, JPEG or PNG.")


def _normalise(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)  # phone photos carry their rotation in EXIF
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if max(img.size) > MAX_PAGE_EDGE:
        img.thumbnail((MAX_PAGE_EDGE, MAX_PAGE_EDGE), Image.LANCZOS)
    return img.convert("RGB")


def load_pages(data: bytes, max_pages: int) -> list[Image.Image]:
    kind = detect_type(data)
    if kind == "image":
        return [_normalise(Image.open(io.BytesIO(data)))]
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except Exception as exc:  # noqa: BLE001
        raise DocumentError("The PDF could not be opened (damaged or password protected).") from exc
    try:
        if len(pdf) == 0:
            raise DocumentError("The PDF has no pages.")
        if len(pdf) > max_pages:
            raise DocumentError(f"The PDF has {len(pdf)} pages; at most {max_pages} are allowed.")
        pages = []
        for index in range(len(pdf)):
            bitmap = pdf[index].render(scale=RENDER_DPI / 72)
            pages.append(_normalise(bitmap.to_pil()))
        return pages
    finally:
        pdf.close()


def _percentile(hist: list[int], fraction: float) -> int:
    target, seen = sum(hist) * fraction, 0
    for value, count in enumerate(hist):
        seen += count
        if seen >= target:
            return value
    return 255


def quality(img: Image.Image) -> dict:
    """Simple, explainable checks; warnings suggest a rescan. Contrast is measured between the
    darkest ink (1st percentile) and the paper (median), because a clean page is mostly white."""
    grey = img.convert("L")
    small = grey.copy()
    small.thumbnail((1000, 1000))
    hist = small.histogram()
    paper = _percentile(hist, 0.5)
    ink = _percentile(hist, 0.01)
    contrast = paper - ink
    warnings = []
    if min(img.size) < 700:
        warnings.append("low resolution: handwriting may be unreadable")
    if paper < 110:
        warnings.append("image is dark")
    if contrast < 60:
        warnings.append("faint writing or low contrast")
    # Blur is not checked: no simple measure was reliable on test scans (see docs, known limitations).
    return {"paper": paper, "ink": ink, "contrast": contrast, "warnings": warnings}


def to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def rotate(img: Image.Image, degrees: int) -> Image.Image:
    if degrees % 90:
        raise DocumentError("Rotate in steps of 90 degrees.")
    return img.rotate(-degrees, expand=True)  # positive = clockwise


def redact_top(img: Image.Image, percent: float) -> Image.Image:
    """White out the top ``percent`` of the page (name / roll number) before external processing."""
    if percent <= 0:
        return img
    out = img.copy()
    height = int(out.height * min(percent, 50) / 100)
    out.paste((255, 255, 255), (0, 0, out.width, height))
    return out
