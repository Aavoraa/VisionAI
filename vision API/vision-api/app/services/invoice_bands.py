"""Band reading: the item table is read in horizontal bands at full resolution.

A whole-page table read shrinks the page to the model's input size, so on long tables (30+
rows) or phone photos digits blur and rows drift out of alignment. Instead:

1. ``deskew`` straightens the photo (small rotations only), so bands can be cut between rows.
2. A vision model locates the item table's column-caption row (and its "Total" row).
3. ``plan_bands`` cuts the rows below the caption into overlapping bands of about
   ``BAND_FRACTION`` of the page height; every cut is placed in the white gap between two
   text lines (or on a ruled line), never through a row.
4. ``band_images`` pastes the caption row on top of every band, so the model always knows
   which column is which, and each band is read with PaddleOCR-VL's table task.

Rows seen twice (in the overlap) are merged by line number in ``invoice_extraction``.

Zoomed crops (see ``invoice_reread``) use the same idea for the rest of the page: the vision
model boxes the seller, buyer, invoice-details and totals blocks (or one field's value), and
``zoom_crop`` cuts that box out with a margin and enlarges it until it fills the OCR model's
pixel budget, so small print is read with more pixels per character.

Everything here is plain image arithmetic (NumPy + Pillow): no model calls.
"""

import io
import json
import re
from dataclasses import dataclass

import numpy as np
from PIL import Image

BAND_FRACTION = 0.15  # band height as a share of the page height (about 8-12 item rows)
OVERLAP_FRACTION = 0.03  # rows repeated between neighbouring bands (about 1-2 rows)
MIN_BAND_FRACTION = 0.05
MAX_SKEW_DEGREES = 4.0
MIN_SKEW_DEGREES = 0.3  # smaller angles are left alone (resampling blurs more than it helps)
# PaddleOCR-VL-1.6 shrinks larger images to this many pixels (mmproj image_max_pixels), so
# enlarging a crop beyond it gains nothing.
OCR_MAX_PIXELS = 1_003_520


def _to_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _ink(gray: np.ndarray) -> np.ndarray:
    """Boolean mask of dark (printed) pixels; the threshold adapts to the photo's exposure."""
    paper = float(np.percentile(gray, 90))
    return gray < paper * 0.72


def _sharpness(mask: Image.Image) -> float:
    rows = np.asarray(mask, dtype=np.float32).sum(axis=1)
    return float(np.square(np.diff(rows)).sum())


def skew_angle(image: Image.Image) -> float:
    """Rotation (degrees, Pillow's counter-clockwise convention) that makes text lines level.

    Text lines give the sharpest horizontal ink profile when they are level: try angles in
    ``±MAX_SKEW_DEGREES`` (coarse, then fine) and keep the sharpest.
    """
    small = image.convert("L")
    small.thumbnail((900, 900))
    mask = Image.fromarray((_ink(np.asarray(small)) * 255).astype(np.uint8))

    def score(angle: float) -> float:
        return _sharpness(mask.rotate(angle, resample=Image.Resampling.NEAREST, fillcolor=0))

    level = score(0.0)
    coarse = max(np.arange(-MAX_SKEW_DEGREES, MAX_SKEW_DEGREES + 0.01, 0.5), key=score)
    fine = max(np.arange(coarse - 0.5, coarse + 0.51, 0.1), key=score)
    if score(fine) <= level * 1.05:  # not clearly better than leaving it (blank or non-text page)
        return 0.0
    return round(float(fine), 1)


def deskew(page: bytes) -> tuple[bytes, float]:
    """The page straightened (PNG) and the angle applied; unchanged if already level."""
    with Image.open(io.BytesIO(page)) as opened:
        image = opened.convert("RGB")
    angle = skew_angle(image)
    if abs(angle) < MIN_SKEW_DEGREES:
        return page, 0.0
    rotated = image.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(255, 255, 255))
    return _to_png(rotated), angle


# ---------------------------------------------------------------------------
# Locating the table (answer of the vision model)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableLocation:
    """Vertical positions as fractions of the page height (0 = top)."""

    caption_top: float
    caption_bottom: float
    total_bottom: float | None  # bottom of the table's "Total" row, if found


LOCATE_PROMPT = (
    "Locate the column-caption row of the line-items table (the row with captions such as "
    "Sl No, Description, HSN, Quantity, Rate, Amount) and the grand-total row of that table. "
    'Answer as JSON: [{"label": "header_row", "bbox_2d": [x1, y1, x2, y2]}, '
    '{"label": "total_row", "bbox_2d": [x1, y1, x2, y2]}]'
)


def parse_location(text: str) -> TableLocation | None:
    """Parse the model's boxes (Qwen-VL convention: coordinates 0-1000); None if implausible."""
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        return None
    try:
        boxes = json.loads(match.group())
    except ValueError:
        return None
    found: dict[str, list[float]] = {}
    for box in boxes if isinstance(boxes, list) else []:
        coords = box.get("bbox_2d") if isinstance(box, dict) else None
        if isinstance(coords, list) and len(coords) == 4 and all(isinstance(v, int | float) for v in coords):
            found.setdefault(str(box.get("label", "")), [float(v) for v in coords])
    header = found.get("header_row")
    if header is None or not (0 <= header[1] < header[3] <= 1000) or header[3] - header[1] > 120:
        return None  # no caption row, or a "row" taller than 12% of the page: not a row
    total = found.get("total_row")
    total_bottom = total[3] / 1000 if total and header[3] < total[1] <= total[3] <= 1000 else None
    return TableLocation(header[1] / 1000, header[3] / 1000, total_bottom)


# ---------------------------------------------------------------------------
# Cutting bands
# ---------------------------------------------------------------------------


def cut_candidates(gray: np.ndarray) -> np.ndarray:
    """Rows where a cut does not slice text: white gaps between lines, or ruled lines.

    Vertical column rules put some ink in every row of a table, so "white" means close to
    the least-inked row of the region rather than zero.
    """
    ink = _ink(gray).sum(axis=1).astype(np.float64)
    width = gray.shape[1]
    floor = np.percentile(ink, 5)
    gap = ink <= floor + max(2.0, 0.01 * width)
    ruled = ink >= 0.6 * width
    return gap | ruled


def plan_bands(gray: np.ndarray, top: int, bottom: int, band_fraction: float = BAND_FRACTION) -> list[tuple[int, int]]:
    """Overlapping (start, end) row ranges covering ``top..bottom``, cut between text lines."""
    height = gray.shape[0]
    band = max(1, round(band_fraction * height))
    overlap = round(OVERLAP_FRACTION * height)
    if bottom - top <= band * 1.3:
        return [(top, bottom)]
    ok = np.flatnonzero(cut_candidates(gray[top:bottom])) + top

    def nearest(target: int, low: int, high: int) -> int:
        choices = ok[(ok >= low) & (ok <= high)]
        return int(choices[np.argmin(np.abs(choices - target))]) if choices.size else target

    bands: list[tuple[int, int]] = []
    start = top
    while True:
        if bottom - start <= band * 1.3:
            bands.append((start, bottom))
            return bands
        end = nearest(start + band, start + band // 2, start + band + band // 3)
        bands.append((start, end))
        next_start = nearest(end - overlap, end - 2 * overlap, end - 1)
        start = next_start if next_start > start else end


def band_regions(page: bytes, location: TableLocation, band_fraction: float = BAND_FRACTION) -> list[tuple[float, float]]:
    """Where ``band_images`` cuts: (top, bottom) of each band as fractions of the page height."""
    with Image.open(io.BytesIO(page)) as opened:
        gray = np.asarray(opened.convert("L"))
    height = gray.shape[0]
    row = (location.caption_bottom - location.caption_top) * height
    top = min(height, round(location.caption_bottom * height + 0.1 * row))
    bottom = height if location.total_bottom is None else min(height, round((location.total_bottom + 0.02) * height))
    if bottom - top < MIN_BAND_FRACTION * height:
        return []
    return [(start / height, end / height) for start, end in plan_bands(gray, top, bottom, band_fraction)]


def band_images(page: bytes, location: TableLocation, band_fraction: float = BAND_FRACTION) -> list[bytes]:
    """PNG bands of the item table, each with the column-caption row pasted on top.

    ``band_fraction`` sets the band height; the item re-read uses shorter bands (cut in other
    places, and small enough that the model reads them without shrinking).
    """
    with Image.open(io.BytesIO(page)) as opened:
        image = opened.convert("RGB")
    width, height = image.size
    row = (location.caption_bottom - location.caption_top) * height
    caption = (max(0, round(location.caption_top * height - 0.4 * row)),
               min(height, round(location.caption_bottom * height + 0.1 * row)))
    top = caption[1]
    bottom = height
    if location.total_bottom is not None:
        bottom = min(height, round((location.total_bottom + 0.02) * height))
    if bottom - top < MIN_BAND_FRACTION * height:
        return []
    gray = np.asarray(image.convert("L"))
    header = image.crop((0, caption[0], width, caption[1]))
    out: list[bytes] = []
    for start, end in plan_bands(gray, top, bottom, band_fraction):
        body = image.crop((0, start, width, end))
        combined = Image.new("RGB", (width, header.height + body.height), (255, 255, 255))
        combined.paste(header, (0, 0))
        combined.paste(body, (0, header.height))
        out.append(_to_png(combined))
    return out


# ---------------------------------------------------------------------------
# Zoomed crops (blocks and single fields)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Box:
    """A rectangle as fractions of the page (0 = left / top)."""

    x1: float
    y1: float
    x2: float
    y2: float


REGIONS = ("seller", "buyer", "invoice_details", "totals")
REGIONS_PROMPT = (
    "Locate these blocks of the invoice: the seller block (the company issuing the invoice: its "
    "name, address and GSTIN, usually at the top), the buyer block (captioned 'Buyer' or 'Bill to': "
    "name, address and GSTIN), the invoice details block (invoice number and invoice date), and the "
    "totals block (taxable value, taxes and the grand total, below the item table). Leave out a block "
    'that is not printed. Answer as JSON: [{"label": "seller", "bbox_2d": [x1, y1, x2, y2]}, '
    '{"label": "buyer", "bbox_2d": [x1, y1, x2, y2]}, {"label": "invoice_details", "bbox_2d": '
    '[x1, y1, x2, y2]}, {"label": "totals", "bbox_2d": [x1, y1, x2, y2]}]'
)
FIELD_PROMPT = 'Locate {what} on this invoice. Answer as JSON: [{{"label": "value", "bbox_2d": [x1, y1, x2, y2]}}]'
MAX_REGION_AREA = 0.5  # a "block" covering more than half the page is not a block
REGION_PAD = (0.03, 0.02)  # margin added around a block (x, y), as page fractions: boxes are approximate
FIELD_PAD = (0.08, 0.025)  # wider around one value, so its caption ("Invoice No.") is in the crop too


def _boxes(text: str) -> list[tuple[str, list[float]]]:
    """(label, [x1, y1, x2, y2]) pairs from the model's JSON answer (coordinates 0-1000).

    Accepts the asked-for list (``[{"label": ..., "bbox_2d": [...]}, ...]``) and the object
    Qwen sometimes returns instead (``{"seller": {"label": ..., "bbox_2d": [...]}, ...}`` or
    ``{"seller": [x1, y1, x2, y2], ...}``), with or without a ```json fence.
    """
    answer: object = None
    for pattern in (r"\{.*\}", r"\[.*\]"):  # an object may contain lists, so try it first
        if match := re.search(pattern, text, re.S):
            try:
                answer = json.loads(match.group())
                break
            except ValueError:
                continue
    if isinstance(answer, dict) and "bbox_2d" in answer:
        answer = [answer]
    if isinstance(answer, dict):
        answer = [
            {**value, "label": value.get("label", key)} if isinstance(value, dict) else {"label": key, "bbox_2d": value}
            for key, value in answer.items()
        ]
    out: list[tuple[str, list[float]]] = []
    for box in answer if isinstance(answer, list) else []:
        coords = box.get("bbox_2d") if isinstance(box, dict) else None
        if isinstance(coords, list) and len(coords) == 4 and all(isinstance(v, int | float) for v in coords):
            out.append((str(box.get("label", "")), [float(v) for v in coords]))
    return out


def _plausible(coords: list[float], *, max_area: float) -> Box | None:
    x1, y1, x2, y2 = coords
    if not (0 <= x1 < x2 <= 1000 and 0 <= y1 < y2 <= 1000):
        return None
    if (x2 - x1) < 20 or (y2 - y1) < 8 or (x2 - x1) * (y2 - y1) > max_area * 1_000_000:
        return None  # a sliver (a misplaced point) or most of the page (not located)
    return Box(x1 / 1000, y1 / 1000, x2 / 1000, y2 / 1000)


def parse_regions(text: str) -> dict[str, Box]:
    """The located blocks (``REGIONS`` labels only); implausible boxes are left out."""
    found: dict[str, Box] = {}
    for label, coords in _boxes(text):
        if label in REGIONS and label not in found and (box := _plausible(coords, max_area=MAX_REGION_AREA)):
            found[label] = box
    return found


def parse_field_box(text: str) -> Box | None:
    """The first plausible box of a one-field answer (a value is small: at most 20% of the page)."""
    return next((box for _, coords in _boxes(text) if (box := _plausible(coords, max_area=0.2))), None)


def zoom_crop(page: bytes, box: Box, *, max_scale: float, pad: tuple[float, float] = REGION_PAD) -> tuple[bytes, float]:
    """``box`` (plus ``pad``) cut from ``page`` and enlarged to fill ``OCR_MAX_PIXELS``, at most
    ``max_scale`` times; returns the PNG and the scale used (1.0: the crop is already large)."""
    with Image.open(io.BytesIO(page)) as opened:
        image = opened.convert("RGB")
    width, height = image.size
    left, top = max(0, round((box.x1 - pad[0]) * width)), max(0, round((box.y1 - pad[1]) * height))
    right, bottom = min(width, round((box.x2 + pad[0]) * width)), min(height, round((box.y2 + pad[1]) * height))
    crop = image.crop((left, top, max(right, left + 1), max(bottom, top + 1)))
    scale = min(max_scale, (OCR_MAX_PIXELS / (crop.width * crop.height)) ** 0.5)
    if scale < 1.05:
        return _to_png(crop), 1.0
    crop = crop.resize((round(crop.width * scale), round(crop.height * scale)), Image.Resampling.LANCZOS)
    return _to_png(crop), round(scale, 2)
