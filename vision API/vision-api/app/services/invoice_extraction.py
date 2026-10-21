"""Invoice image/PDF -> PaddleOCR-VL -> structured ``Invoice`` (Phase 1: rule-based field mapping).

Per page, PaddleOCR-VL runs two tasks:

* ``Table Recognition:`` on the whole page. Invoices are mostly one big grid, so this yields
  the header boxes (invoice number, date, ...), the line items, the totals and the tax
  summary as OTSL cells (``<fcel>`` text, ``<ecel>`` empty, ``<lcel>``/``<ucel>``/``<xcel>``
  merged, ``<nl>`` end of row).
* ``OCR:`` on the top of the first page, for the seller and buyer blocks, which are free
  text rather than table cells, and on the bottom of the last page, for the signature line
  ("for <seller's full name> ... Authorised Signatory"). Whole-page OCR is slower and tends
  to loop on long tables; the table task covers the rest.
* Band reading (when the reasoning model is configured, see ``invoice_bands``): the item
  table is also read in overlapping horizontal bands at full resolution, each with the
  column captions pasted on top. Rows are merged with the whole-page reading by line number,
  keeping the version whose quantity x rate matches its amount.
* Zoomed crops (also with the reasoning model, see ``invoice_reread``): the seller, buyer,
  invoice-details and totals blocks are read enlarged, then fields still flagged are boxed
  and read again at up to ``ZoomOptions.max_scale``; they act as a second opinion only.

Photos are straightened first (``invoice_bands.deskew``).

The mapping is deliberately conservative: a value that is not found stays ``None`` with
status ``missing``; anything derived rather than read is marked ``check``. Phase 2 replaces
the rules with a local reasoning model; the output schema stays the same.
"""

import asyncio
import contextvars
import datetime as dt
import difflib
import io
import json
import re
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from PIL import Image

from app.models.base import ModelError, VisionModel
from app.schemas.invoice import ExtractionUsage, Extracted, Invoice, InvoiceExtraction, LineItem
from app.services.invoice_bands import TableLocation, band_images, band_regions, deskew
from app.services.invoice_progress import ProgressFn, silent
from app.services.invoice_normalize import format_state, normalize_invoice_number, normalize_unit
from app.services.invoice_validation import gstin_check_char, is_valid_gstin, validate_invoice  # noqa: F401 (re-exported)

if TYPE_CHECKING:
    from app.services.invoice_fallback import FallbackReader
    from app.core.config import Settings
    from app.services.invoice_llm import LlmFieldMapper

logger = logging.getLogger(__name__)

OCR_TASK = "OCR:"
TABLE_TASK = "Table Recognition:"
HEADER_FRACTION = 0.35  # seller/buyer blocks sit in the top third of GST invoices
MAX_HEADER_FRACTION = 0.6  # with a located item table the header reaches down to its caption row
FOOTER_FRACTION = 0.15  # signature line ("for <seller>") at the bottom of the last page
HEADER_MAX_TOKENS = 1024
FOOTER_MAX_TOKENS = 256
TABLE_MAX_TOKENS = 4096
BAND_MAX_TOKENS = 2048

# ---------------------------------------------------------------------------
# Parsing helpers (pure functions, unit-tested)
# ---------------------------------------------------------------------------

Grid = list[list[str | None]]  # None = cell merged into a neighbour

_OTSL_CELL = re.compile(r"<(fcel|ecel|lcel|ucel|xcel)>([^<]*)")


def parse_otsl(text: str) -> Grid:
    """OTSL table markup -> rows of cells (text, ``""`` for empty, ``None`` for merged)."""
    grid: Grid = []
    for raw_row in text.split("<nl>"):
        cells: list[str | None] = []
        for kind, content in _OTSL_CELL.findall(raw_row):
            if kind == "fcel":
                cells.append(" ".join(content.replace("\\n", " ").split()))  # some outputs spell line breaks "\n"
            elif kind == "ecel":
                cells.append("")
            else:
                cells.append(None)
        if cells:
            grid.append(cells)
    return grid


_AMOUNT = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d+)?")


def parse_amount(text: str | None) -> Decimal | None:
    """First number in ``text``: ``"₹ 49,969.00 E & OE"`` -> ``Decimal("49969.00")``."""
    if not text:
        return None
    match = _AMOUNT.search(text)
    if not match:
        return None
    try:
        return Decimal(match.group().replace(",", ""))
    except InvalidOperation:
        return None


def amounts_in(text: str) -> list[Decimal]:
    """All numbers in ``text`` that are not percentages."""
    values = []
    for match in _AMOUNT.finditer(text):
        if text[match.end() :].lstrip().startswith("%"):
            continue
        values.append(Decimal(match.group().replace(",", "")))
    return values


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date(text: str | None) -> dt.date | None:
    """Day-first dates as printed on Indian invoices: 16-Jul-26, 16/07/2026, 16.07.26, 16 July 2026, 2026-07-16."""
    if not text:
        return None
    text = text.strip()
    iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if iso:
        year, month, day = (int(g) for g in iso.groups())
    else:
        named = re.search(r"\b(\d{1,2})[\s\-/.]*([A-Za-z]{3,9})[\s\-/.,]*(\d{2,4})\b", text)
        numeric = re.search(r"\b(\d{1,2})[\-/.](\d{1,2})[\-/.](\d{2,4})\b", text)
        if named and named.group(2)[:3].lower() in _MONTHS:
            day, month, year = int(named.group(1)), _MONTHS[named.group(2)[:3].lower()], int(named.group(3))
        elif numeric:
            day, month, year = (int(g) for g in numeric.groups())
        else:
            return None
        if year < 100:
            year += 2000
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


# --- GSTIN -------------------------------------------------------------------

# gstin_check_char / is_valid_gstin live in invoice_validation (which this module imports).
# Expected character class per position: 2-digit state, PAN (5 letters, 4 digits, 1 letter),
# entity number, "Z", check character.
_GSTIN_SHAPE = "DDLLLLLDDDDLAZA"
_TO_DIGIT = str.maketrans({"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6"})
_TO_LETTER = str.maketrans({"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G"})


def repair_gstin(candidate: str) -> str:
    """Fix OCR look-alikes (S/5, I/1, O/0, ...) using the position's expected character class."""
    fixed = []
    for char, shape in zip(candidate.upper(), _GSTIN_SHAPE, strict=True):
        if shape == "D":
            char = char.translate(_TO_DIGIT)
        elif shape == "L":
            char = char.translate(_TO_LETTER)
        elif shape == "Z":
            char = "Z" if char in "Z2" else char
        fixed.append(char)
    return "".join(fixed)


_LOOK_ALIKES = {"I": "1", "1": "I", "O": "0", "0": "O", "S": "5", "5": "S", "B": "8", "8": "B", "Z": "2", "2": "Z", "G": "6", "6": "G", "L": "1"}


def _swap_look_alike(candidate: str) -> str | None:
    """Positions that may hold a digit or a letter (13th, 15th) cannot be fixed by shape; try
    one look-alike swap there and accept it only if exactly one variant passes the checksum."""
    valid = {
        candidate[:i] + _LOOK_ALIKES[candidate[i]] + candidate[i + 1 :]
        for i in (12, 14)
        if candidate[i] in _LOOK_ALIKES and is_valid_gstin(candidate[:i] + _LOOK_ALIKES[candidate[i]] + candidate[i + 1 :])
    }
    return valid.pop() if len(valid) == 1 else None


def find_gstin(line: str) -> Extracted[str] | None:
    """A GSTIN in ``line``: ``ok`` if its checksum is valid (possibly after repairing look-alikes)."""
    # A checksum-valid token anywhere on the line beats an unverifiable one (e.g. a slice of an
    # IRN hash that merely looks like a GSTIN).
    unverified: Extracted[str] | None = None
    for token in re.findall(r"(?<![A-Za-z0-9])[A-Za-z0-9]{15}(?![A-Za-z0-9])", line) or re.findall(r"[A-Za-z0-9]{15}", line):
        token = token.upper()
        if is_valid_gstin(token):
            return Extracted.found(token, line)
        repaired = repair_gstin(token)
        if not is_valid_gstin(repaired):
            repaired = _swap_look_alike(repaired) or repaired
        if is_valid_gstin(repaired):
            return Extracted(value=repaired, status="ok", source_text=line, note=f"corrected OCR look-alikes in {token}")
        if unverified is None and re.fullmatch(r"\d{2}[A-Z]{5}[A-Z0-9]{8}", repaired):
            unverified = Extracted.check(repaired, "GSTIN checksum does not match; verify every character", line)
    return unverified


# --- Labels ------------------------------------------------------------------

_LABELS = (
    "invoice no", "invoice number", "invoice date", "dated", "date", "delivery note", "mode/terms of payment",
    "terms of payment", "payment terms", "reference no", "other references", "buyer's order no", "buyers order no",
    "buyer (bill to)", "buyer", "bill to", "ship to", "consignee", "dispatch doc no", "delivery note date",
    "dispatched through", "destination", "terms of delivery", "state name", "state code", "e-mail", "email",
    "gstin", "place of supply", "tax invoice", "duplicate for", "original for", "triplicate for",
    "hsn/sac", "description of goods", "declaration", "pan no", "pan/it no",
    "billing address", "billed to", "bill to party", "ship to party", "shipped to", "details of receiver",
    "bill no", "bill date", "invoice details", "goods dispatch to", "details of consignee",
)
_LABEL_RE = re.compile(r"^(?:" + "|".join(re.escape(label) for label in sorted(_LABELS, key=len, reverse=True)) + r")(?![a-z0-9])")


def _norm(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").split()).strip(" :.-()")


def is_label(text: str | None) -> bool:
    """A printed field caption ("Invoice No.", "Buyer (Bill to)", ...) rather than a value."""
    return bool(text) and bool(_LABEL_RE.match(_norm(text)))  # type: ignore[arg-type]


def value_for_label(grid: Grid, *labels: str) -> tuple[str, str] | None:
    """Value of a header box: the cell below the label (same column), else to its right.

    Returns ``(value, label text)``; ``None`` if the label is absent or its box is empty.
    """
    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            if not cell or not any(_norm(cell).startswith(label) for label in labels):
                continue
            if inline := _inline_value(cell, labels):
                return inline, cell
            below = grid[r + 1][c] if r + 1 < len(grid) and c < len(grid[r + 1]) else None
            if below and not is_label(below):
                return below, cell
            right = next((x for x in row[c + 1 :] if x is not None), None)
            if right and not is_label(right):
                return right, cell
            return None
    return None


_ANY_LABEL = re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(label) for label in sorted(_LABELS, key=len, reverse=True)) + r")(?![a-z0-9])", re.I)


def _inline_value(cell: str, labels: tuple[str, ...]) -> str | None:
    """A value printed in the label's own cell: "Invoice No. KDC/833/26-27 Delivery Note" -> "KDC/833/26-27".

    The cell holds the label, its value and often the next box's label; the value ends where
    the next printed label starts.
    """
    for label in sorted(labels, key=len, reverse=True):
        match = re.match(r"\s*" + r"\s*".join(re.escape(part) for part in label.split()) + r"[\s.:()\-]*", cell, re.I)
        if match:
            rest = cell[match.end() :]
            following = _ANY_LABEL.search(rest)
            value = rest[: following.start() if following else len(rest)].strip(" :.-")
            return value or None
    return None


def strip_repetition(text: str) -> tuple[str, bool]:
    """Cut a degenerate tail (a few tokens repeated until the length limit) that small models emit."""
    tokens = list(re.finditer(r"\S+", text))
    if len(tokens) < 30:
        return text, False
    looping = {m.group() for m in tokens[-30:]}
    if len(looping) > 3:
        return text, False
    start = len(tokens) - 1
    while start > 0 and tokens[start - 1].group() in looping:
        start -= 1
    return text[: tokens[start].start()].rstrip(), True


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------

_CONTACT = re.compile(r"^(ph|phone|tel|mob|mobile|contact)\b", re.I)
_ADDRESS_LIKE = re.compile(r"\b\d{3}\s?\d{3}\b|@|^(ph|phone|tel|mob)\b", re.I)  # PIN code, e-mail, phone
# "Buyer (Bill to) ESSA GARMENTS ..." : a party caption with the name on the same line.
_PARTY_CAPTION = re.compile(
    r"^\s*(buyer\s*\(bill to\)|consignee\s*\(ship to\)|bill(?:ing|ed)?\s*(?:to|address)(?:\s*party)?"
    r"|ship(?:ped)?\s*to(?:\s*party)?|details of receiver|buyer|consignee)[\s.,:|]*(.*)$",
    re.I,
)
# "To," above the addressee (freight bills, letters): a buyer caption of its own.
_TO_CAPTION = re.compile(r"^\s*to\s*[,.:]*\s*$", re.I)
# Header values printed between the caption and the party ("Bill No. 1120", "From TIRUPPUR").
_HEADER_VALUE = re.compile(r"^\s*(?:(?:bill|invoice)\s*(?:no|date)|from)\b", re.I)
_SIGNATURE = re.compile(r"^\s*for\s+(?!transporter|recipient|supplier|the\s)(.*[A-Za-z]{3}.*?)\s*$", re.I)


def parse_signature(footer_text: str) -> str | None:
    """Seller name from the signature line: "for SHALIHA RETAIL VENTURES A/C - (26-27)"."""
    for line in footer_text.splitlines():
        if match := _SIGNATURE.match(line):
            return match.group(1).strip()
    return None


@dataclass
class _Party:
    name: str | None
    address: list[str]
    gstin: Extracted[str]
    state: str | None
    role: str | None  # "buyer" / "consignee" when the caption above the block says so


def _cell_text(table: str) -> str:
    """Table markup as plain lines (one per cell line), for the party parser."""
    text = re.sub(r"<(?:fcel|ecel|lcel|ucel|xcel|nl)>", "\n", table.replace("\\n", "\n"))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


_INVOCATION = re.compile(r"^\W*!!|\bnama[hk]\b|^\W*(?:shri|sri|shree)\s+\w+\s+(?:nama|ji)\b", re.I)


def _captioned_buyer(header_text: str, gstin: Extracted[str]) -> "_Party | None":
    """A buyer block that has a caption but no GSTIN line in the text ("Billing Address" and the
    name/address below it); its GSTIN comes from elsewhere on the invoice."""
    lines = [line.strip() for line in header_text.splitlines()]
    for index, line in enumerate(lines):
        caption = _PARTY_CAPTION.match(line)
        if not caption or re.search(r"consignee|ship", caption.group(1), re.I):
            continue
        block: list[str] = [caption.group(2).strip()] if re.search(r"[A-Za-z]{2}", caption.group(2)) else []
        for following in lines[index + 1 :]:
            if not following or is_label(following) or _HEADER_VALUE.match(following) or len(block) >= 5:
                break
            if not re.match(r"^\s*(?:phone|ph|mob|mobile|e-?mail|contact)\b", following, re.I):
                block.append(re.sub(r"^(?:name|address)\s*:\s*", "", following, flags=re.I))
        if block and not _ADDRESS_LIKE.search(block[0]):
            return _Party(name=block[0], address=block[1:], gstin=gstin, state=None, role="buyer")
    return None


def parse_parties(header_text: str) -> list[_Party]:
    """Seller/buyer blocks from header OCR, anchored on their GSTIN lines."""
    lines = [line.strip() for line in header_text.splitlines()]
    parties: list[_Party] = []
    previous_end = -1
    for index, line in enumerate(lines):
        gstin = find_gstin(line) if ("GSTIN" in line.upper() or "UIN" in line.upper() or re.search(r"\b\d{2}[A-Z]{5}", line)) else None
        if gstin is None:
            continue
        block: list[str] = []
        caption = ""
        for back in range(index - 1, previous_end, -1):
            candidate = lines[back]
            if not candidate:
                continue
            if _TO_CAPTION.match(candidate):
                caption = "bill to"
                break
            if _HEADER_VALUE.match(candidate) or re.match(r"^\s*(?:e-?mail|contact|ph|phone|mob(?:ile)?)\b", candidate, re.I):
                continue  # header values and contact lines inside the block
            if is_label(candidate):
                caption = _norm(candidate)
                party_caption = _PARTY_CAPTION.match(candidate)
                rest = party_caption.group(2).strip(" |:") if party_caption else ""
                if rest and re.search(r"[A-Za-z]{2}", rest) and not _HEADER_VALUE.match(rest) and not is_label(rest) and not _PARTY_CAPTION.match(rest):
                    block.insert(0, party_caption.group(2).strip())
                break
            if parse_date(candidate):  # e.g. "CR/134/26-27 16-Jul-26": header values, not the party
                break
            block.insert(0, re.sub(r"^(?:name|address)\s*:\s*", "", candidate, flags=re.I))
        state = None
        for after in lines[index + 1 : index + 4]:
            if _norm(after).startswith("state name"):
                state = after.split(":", 1)[-1].strip()
        # "STATE : TAMIL NADU-33" inside the block (layouts that print it above the GSTIN)
        for line in block:
            if re.match(r"^state\s*(?:name)?\s*[:\-]", line, re.I):
                state = state or line.split(":", 1)[-1].strip()
        block = [line for line in block if not re.match(r"^state\s*(?:name|code)?\s*[:\-/]", line, re.I)]
        previous_end = index
        block = [re.sub(r"^(?:city|town)\s*:\s*", "", line, flags=re.I) for line in block if not _INVOCATION.search(line)]
        # A name never looks like an address line; if the top line does, the name was not read.
        name = block[0] if block and not _ADDRESS_LIKE.search(block[0]) else None
        address = [line for line in (block[1:] if name else block) if not _CONTACT.match(line) and line != name]
        role = (
            "consignee" if re.search(r"consignee|ship", caption)
            else "buyer" if re.search(r"buyer|bill|receiver", caption)
            else None
        )
        parties.append(_Party(name=name, address=address, gstin=gstin, state=state, role=role))
    return parties


def _party_fields(party: _Party | None) -> tuple[Extracted[str], Extracted[str], Extracted[str]]:
    if party is None:
        return Extracted(), Extracted(), Extracted()
    name = Extracted.found(party.name, party.name) if party.name else Extracted()
    address_text = ", ".join(party.address)
    address = Extracted.found(address_text, "\n".join(party.address)) if party.address else Extracted()
    return name, address, party.gstin


def _decimal_field(text: str | None, source: str | None = None) -> Extracted[Decimal]:
    value = parse_amount(text)
    return Extracted.found(value, source or text) if value is not None else Extracted()


def _quantity_number(text: str) -> Decimal:
    """ "7.0000" / "7,0000" (a comma before exactly 4 digits is Tally's decimal mark as OCR'd) / "1,200"."""
    if re.fullmatch(r"\d+,\d{4}", text):
        return Decimal(text.replace(",", "."))
    return Decimal(text.replace(",", ""))


_ITEM_START = re.compile(r"^(\d{1,4})\s+(\S.*)$")
_QTY_UNIT = re.compile(r"^([\d.,]+)\s*([A-Za-z][A-Za-z.]*)$")
_PERCENT = re.compile(r"^([\d.]+)\s*%$")
_NUMBER = re.compile(r"^[-+]?[\d,]+(?:\.\d+)?$")


def _item_values(cells: list[str], percent_is_discount: bool) -> dict[str, Extracted] | None:  # type: ignore[type-arg]
    """Numeric columns of an item row (HSN, quantity + unit, rate, %, amount); None if it has none."""
    fields: dict[str, Extracted] = {}  # type: ignore[type-arg]
    numbers: list[tuple[Decimal, str]] = []
    for cell in cells:
        if "hsn_sac" not in fields and re.fullmatch(r"\d{4,8}", cell):
            fields["hsn_sac"] = Extracted.found(cell, cell)
        elif "quantity" not in fields and (qty := _QTY_UNIT.match(cell)):
            fields["quantity"] = Extracted.found(_quantity_number(qty.group(1)), cell)
            fields["unit"] = Extracted.found(normalize_unit(qty.group(2)), cell)
        elif pct := _PERCENT.match(cell):
            key = "discount_percent" if percent_is_discount else "tax_rate"
            fields.setdefault(key, Extracted.found(Decimal(pct.group(1)), cell))
        elif _NUMBER.match(cell):
            numbers.append((Decimal(cell.replace(",", "")), cell))
    if not numbers:
        return None
    if len(numbers) >= 2:
        fields["rate"] = Extracted.found(numbers[0][0], numbers[0][1])
    fields["taxable_value"] = Extracted.found(numbers[-1][0], numbers[-1][1])
    return fields


ITEMS_MISALIGNED = "item rows look misaligned (numbers found on the column-caption row); review every item"
_CAPTION_ROW = ("no", "sl no", "s no", "s.no", "sr no", "si no", "isi no", "sl", "sr", "sl.no", "sr.no", "sno")
_LINE_NUMBER = re.compile(r"^\d{1,4}\.?$")


# Column captions -> item field (first match wins; the amount is the LAST amount-like column).
_COLUMNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("line_number", re.compile(r"^(?:sr|sl|si|isi|s)\.?\s*(?:no)?\.?$|^no\.?$", re.I)),
    ("design", re.compile(r"^(?:design|d\.?\s*no)", re.I)),
    ("description", re.compile(r"descr|particular|product|goods|^item", re.I)),
    ("hsn_sac", re.compile(r"hsn", re.I)),
    ("quantity", re.compile(r"^(?:qty|quantity|pcs)\b", re.I)),
    ("unit", re.compile(r"^(?:per|unit|uom)\b", re.I)),
    ("rate", re.compile(r"^rate\b", re.I)),
    ("discount_percent", re.compile(r"^disc(?:ount)?\.?\s*(?:%|\(%\))?$", re.I)),
)
_AMOUNT_COLUMN = re.compile(r"amount|taxable|value", re.I)


def _column_map(row: list[str | None]) -> dict[str, int] | None:
    """Item field -> column index, if ``row`` is the item table's caption row."""
    found: dict[str, int] = {}
    for index, cell in enumerate(row):
        if not cell:
            continue
        text = _norm(cell)
        for key, pattern in _COLUMNS:
            if key not in found and pattern.search(text):
                found[key] = index
                break
        else:
            if _AMOUNT_COLUMN.search(text):
                found["taxable_value"] = index  # the last one: "Net Amount" after "Gross Amount"
    needed = {"quantity", "taxable_value"}
    return found if needed <= found.keys() and len(found) >= 4 else None


def _column_items(grid: Grid) -> list[LineItem]:
    """Line items read by column caption (layouts whose cells the pattern reader cannot place)."""
    columns: dict[str, int] | None = None
    items: list[LineItem] = []
    for row in grid:
        if columns is None:
            columns = _column_map(row)
            continue
        if any(c and _norm(c).startswith("total") for c in row[:3]):
            break  # the item table ends at its "Total" row; below are tax summaries

        def cell(key: str) -> str | None:
            index = columns.get(key)  # type: ignore[union-attr]
            return row[index] if index is not None and index < len(row) and row[index] else None

        text_columns = sorted(k for k in ("design", "description") if k in columns)  # type: ignore[operator]
        description = [c for c in (cell(k) for k in sorted(text_columns, key=lambda k: columns[k])) if c]  # type: ignore[index]
        first = cell("line_number") or next((c for c in row if c), None)
        description = [c for c in description if c != first]  # caption row "Sl No. Description" in one cell
        if not first:
            continue
        if _LINE_NUMBER.match(first):
            number = int(first.rstrip("."))
            if items and not 0 < number - (items[-1].line_number.value or 0) <= 10:
                continue  # an HSN code or amount in the first column, not the next line number
        elif start := _ITEM_START.match(first):
            number = int(start.group(1))
            description.insert(0, start.group(2).strip())
        else:
            continue
        values: dict[str, Extracted] = {}  # type: ignore[type-arg]
        quantity_text = cell("quantity")
        if quantity_text and (qty := _QTY_UNIT.match(quantity_text)):
            values["quantity"] = Extracted.found(_quantity_number(qty.group(1)), quantity_text)
            values["unit"] = Extracted.found(normalize_unit(qty.group(2)), quantity_text)
        elif quantity_text and (amount := parse_amount(quantity_text)) is not None:
            values["quantity"] = Extracted.found(amount, quantity_text)
        if (unit := cell("unit")) and "unit" not in values and re.fullmatch(r"[A-Za-z.]{2,6}", unit):
            values["unit"] = Extracted.found(normalize_unit(unit), unit)
        for key in ("rate", "taxable_value"):
            if (text := cell(key)) and (amount := parse_amount(text)) is not None:
                values[key] = Extracted.found(amount, text)
        if (text := cell("discount_percent")) and (amount := parse_amount(text.rstrip("% "))) is not None:
            values["discount_percent"] = Extracted.found(amount, text)
        if (hsn := cell("hsn_sac")) and re.fullmatch(r"\d{4,8}", hsn):
            values["hsn_sac"] = Extracted.found(hsn, hsn)
        if "quantity" not in values or not ({"taxable_value", "rate"} & values.keys()):
            continue
        items.append(LineItem(
            line_number=Extracted.found(number, first),
            description=Extracted.found(" ".join(description), " | ".join(description)) if description else Extracted(),
            **values,
        ))
    return items


# --- Transport / freight bills ---------------------------------------------------
# One row per consignment: "Sr | Mr No | Dlvr Dt | Lr No | Inv No | Chrg Wt | Pkgs Consignor/
# Consignee | Freight | Hamali | Dlvr chrg | Amount". The consignor cell spans several printed
# lines, and table recognition often puts its lines (and sometimes the amounts beside them) on
# the wrong row: one row too high, two consignments in one cell, the rest in none. What it does
# keep is the ORDER of the numbered rows, of the consignments and of the amount groups, so the
# three are read as separate sequences and paired in order.

FREIGHT_REALIGNED = "freight bill: the consignor and amount columns were read off their rows and re-aligned; verify"
_FREIGHT_CAPTIONS = {
    "lr": re.compile(r"^(?:l\.?\s*r|g\.?\s*r|c\.?\s*n|lorry|docket)\.?\s*(?:no)?\.?$", re.I),
    "invoice": re.compile(r"^inv(?:oice)?\.?\s*(?:no)?\.?$", re.I),
    "weight": re.compile(r"\b(?:wt|weight)\b", re.I),
}
_CONSIGNMENT = re.compile(r"(?:^|\s)(\d{1,3})\s+(?=[A-Za-z])")  # "<packages> <consignor>" ("1 B FASHION")


def _freight_columns(row: list[str | None]) -> dict[str, int] | None:
    """Caption row of a freight bill -> column index of consignor, LR, invoice, weight."""
    texts = [_norm(c) if c else "" for c in row]
    consignor = next((i for i, t in enumerate(texts) if "consi" in t), None)  # consignor / consignee
    if consignor is None or not any("freight" in t for t in texts[consignor:]):
        return None
    columns = {"consignor": consignor}
    for key, pattern in _FREIGHT_CAPTIONS.items():
        if (index := next((i for i, t in enumerate(texts[:consignor]) if pattern.search(t)), None)) is not None:
            columns[key] = index
    return columns


def _money_tokens(cells: list[str | None]) -> list[tuple[Decimal, str]]:
    """Amounts in ``cells`` ("0 395" holds two), ignoring tick marks next to them ("965✓")."""
    out = []
    for cell in cells:
        for token in (cell or "").split():
            if (amount := parse_amount(re.sub(r"[^\d.,-]", "", token))) is not None:
                out.append((amount, token))
    return out


def _consignments(text: str) -> list[tuple[int, str]]:
    """(packages, consignor) per "<packages> <name>" start; text before the first is the tail of
    a consignment that began above (e.g. in the previous band) and is dropped."""
    starts = list(_CONSIGNMENT.finditer(text))
    return [
        (int(m.group(1)), text[m.end() : starts[i + 1].start() if i + 1 < len(starts) else len(text)].strip())
        for i, m in enumerate(starts)
    ]


def _consignee_suffix(names: list[str]) -> list[str]:
    """The consignee printed after every consignor (the bill's own customer): the longest
    word suffix (2+ words) that most names end with."""
    words = [n.split() for n in names if n.strip()]
    best: list[str] = []
    for w in words:
        for size in range(2, len(w)):
            suffix = w[-size:]
            if sum(v[-size:] == suffix for v in words) * 2 > len(words) and size > len(best):
                best = suffix
    return best


def _strip_suffix(name: str, suffix: list[str]) -> str:
    """``name`` without trailing copies of ``suffix``, its clipped start ("ESSA GARMENTS PVT") or
    its stray end ("LTD" on a row of its own, joined after the full copy)."""
    words = name.split()
    whole = len(suffix)
    for size in range(whole - 1, 0, -1):  # "... PVT LTD" + "LTD": the stray end goes first
        if len(words) > whole + size and words[-size:] == suffix[-size:] and words[-size - whole : -size] == suffix:
            words = words[:-size]
            break
    for size in range(whole, 1, -1):
        if len(words) > size and words[-size:] == suffix[:size]:
            words = words[:-size]
            break
    return " ".join(words)


def freight_items(grid: Grid) -> tuple[list[LineItem], list[str]] | None:
    """Items of a transport bill's consignment table (None: ``grid`` has no such table).

    Quantity = packages, amount = the Amount column (freight + hamali + delivery when it was not
    read); the rate stays empty (freight is per consignment, not per package). LR number,
    invoice number, charged weight and non-zero hamali / delivery charges go into the description.
    Rows whose consignor or amounts had to be taken from another row are ``check``.
    """
    start = next((i for i, row in enumerate(grid) if _freight_columns(row)), None)
    if start is None:
        return None
    columns = _freight_columns(grid[start])
    assert columns is not None
    body: list[tuple[int, list[str | None]]] = []
    for index, row in enumerate(grid[start + 1 :], start + 1):
        if any(c and _norm(c).startswith("total") for c in row):
            break
        body.append((index, row))
    numbered = [
        (i, row) for i, row in body
        if row and row[0] and _LINE_NUMBER.match(row[0].strip()) and sum(bool(c) for c in row[1:6]) >= 2
    ]
    if not numbered:
        return [], []
    # A caption the recognizer dropped ("Mr. No") shifts every column after it: compare where
    # the dates are in the numbered rows with the date caption (row widths vary: "0 395" can
    # come out as one cell or two).
    offset = 0
    caption_date = next((i for i, c in enumerate(grid[start]) if c and re.search(r"\bdt\b|date", _norm(c))), None)
    date_columns = [i for _, row in numbered for i, c in enumerate(row) if c and _WHOLE_DATE.fullmatch(c.strip())]
    if caption_date is not None and date_columns:
        offset = max(0, max(set(date_columns), key=date_columns.count) - caption_date)
    k = columns["consignor"] + offset

    text_stream: list[str] = []
    groups: list[tuple[int, list[tuple[Decimal, str]]]] = []  # (row index, amounts) per row
    for index, row in body:
        consignor = (row[k] or "").strip() if k < len(row) else ""
        wordy = bool(re.search(r"[A-Za-z]{2}", consignor))
        if wordy:
            text_stream.append(consignor)
        amounts = _money_tokens(([] if wordy else [consignor]) + list(row[k + 1 :]))
        if amounts:
            groups.append((index, amounts))
    consignments = _consignments(" ".join(text_stream))
    suffix = _consignee_suffix([name for _, name in consignments])

    rows = [i for i, _ in numbered]
    by_row = dict(groups)
    aligned = bool(groups) and groups[0][0] >= rows[0] and all(i in by_row for i in rows)
    paired = [(i, by_row[i]) for i in rows] if aligned else groups[: len(rows)]
    names_complete = len(consignments) == len(rows)

    items: list[LineItem] = []
    for position, (index, row) in enumerate(numbered):
        if position >= len(paired):
            break  # no amounts left for this row: another reading of the table may have them
        group_row, amounts = paired[position]
        packages, name = consignments[position] if position < len(consignments) else (None, "")
        name = _strip_suffix(name, suffix) if suffix else name

        def cell(key: str) -> str | None:
            column = columns.get(key)  # type: ignore[union-attr]
            if column is None:
                return None
            column += offset if column else 0
            return ((row[column] or "").strip(" ✓✔") or None) if column < len(row) else None  # tick marks

        charges = [a for a, _ in amounts]
        amount = charges[3] if len(charges) >= 4 else sum(charges[:3], Decimal(0))
        details = [f"{label} {value}" for label, value in (("LR", cell("lr")), ("Inv", cell("invoice")), ("Chrg Wt", cell("weight"))) if value]
        details += [f"{label} {charge}" for label, charge in zip(("Hamali", "Dlvr chrg"), charges[1:3], strict=False) if charge]
        description = f"{name} ({', '.join(details)})".strip() if details else name
        source = " | ".join(c for c in row[:k] if c)
        values: dict[str, Extracted] = {  # type: ignore[type-arg]
            "taxable_value": Extracted.found(amount, amounts[3][1] if len(amounts) >= 4 else " + ".join(s for _, s in amounts[:3])),
        }
        if packages is not None:
            values["quantity"] = Extracted.found(Decimal(packages), f"{packages} {name}")
            values["unit"] = Extracted.found("pkgs", f"{packages} {name}")
        number = int(str(row[0]).strip().rstrip("."))
        if group_row != index or not names_complete:  # paired by order, not by row: a reviewer confirms it
            line, desc = Extracted.check(number, FREIGHT_REALIGNED, source), Extracted.check(description, FREIGHT_REALIGNED, name)
        else:
            line, desc = Extracted.found(number, source), Extracted.found(description, name)
        items.append(LineItem(line_number=line, description=desc, **values))
    realigned = any(item.line_number.status == "check" for item in items)
    return items, ([FREIGHT_REALIGNED] if realigned else [])


# --- Size-matrix (garment) invoices -------------------------------------------------
# One block per style: "Sr | HSN nnnn / style name | Size 16 18 20 ... | Qty 1 1 1 ... |
# Rate 1555 1555 ... | Mrp 2490 2490 ...", with the style's Qty | Gross | Disc % | Disc Amt |
# GST % | Net on the right. Each style becomes one line: design = the style code, size = its
# sizes ("16,18,20,22"), rate = gross / qty and MRP = the average of the size MRPs. Table
# recognition keeps the numbers of a sub-row in order but not their columns, so when the size
# row lists more sizes than the style has pieces, the first ones are shown and marked ``check``.

SIZES_GUESSED = "the size row lists {printed}; which {count} sizes these pieces are is not certain: check"
_STYLE_NAME = re.compile(r"^([A-Za-z0-9/]+)\s*-\s*(.+)$")  # "5125 - MAROON PARTY FROCK"
_MATRIX_LABEL = re.compile(r"^(size|qty|rate|mrp)\b\.?\s*(.*)$", re.I)
_HSN_HEADING = re.compile(r"^hsn\s*:?\s*(\d{4,8})$", re.I)


@dataclass
class _Style:
    """One reading of one style block."""

    number: int
    hsn: str | None
    sizes: list[str] | None
    names: list[str]
    qtys: list[Decimal] | None = None
    rates: list[Decimal] | None = None
    mrps: list[Decimal] | None = None
    totals: tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal] | None = None  # qty, gross, disc %, disc, GST %, net
    source: str = ""


def _matrix_totals(numbers: list[Decimal | None]) -> tuple[int, tuple[Decimal, ...]] | None:
    """Position and values of "qty gross disc% disc GST% net" at the end of a sub-row, found by
    their arithmetic (gross x disc% = disc, gross - disc = net) rather than by column."""
    for i in range(len(numbers) - 6, -1, -1):
        window = numbers[i : i + 6]
        if any(n is None for n in window):
            continue
        qty, gross, pct, disc, gst, net = window  # type: ignore[misc]
        if (qty > 0 and qty == qty.to_integral_value() and gross > 0 and gst in _GST_RATES | {Decimal(0)}
                and abs(gross * pct / 100 - disc) <= 1 and abs(gross - disc - net) <= 1):
            return i, tuple(window)  # type: ignore[arg-type]
    return None


def _increasing(tokens: list[str]) -> list[str]:
    """Sizes up to the style's totals read into the same row: numeric sizes run upwards
    (16 18 20 ...), so a smaller number or an amount (6360, 7500.00) ends them; letter sizes
    (S M L XL) end at the first amount."""
    out: list[str] = []
    for token in tokens:
        if re.fullmatch(r"\d{1,3}", token):
            if out and (not re.fullmatch(r"\d{1,3}", out[-1]) or int(token) <= int(out[-1])):
                break  # a number after letter sizes, or one that does not run upwards: the totals
        elif re.fullmatch(r"[\d.,]+", token):
            break
        out.append(token)
    return out


def _matrix_caption(grid: Grid) -> int | None:
    """Index of the last caption row ("Size" with "Qty" and "Gross"/"Net", maybe over two rows)."""
    for i in range(len(grid)):
        rows = grid[i : i + 2]
        for j in range(len(rows)):
            text = " ".join(_norm(c) for row in rows[: j + 1] for c in row if c)
            if re.search(r"\bsize", text) and re.search(r"\bqty\b", text) and re.search(r"\bgross\b|\bnet\b", text):
                return i + j
    return None


def _matrix_styles(grid: Grid) -> list[_Style] | None:
    start = _matrix_caption(grid)
    if start is None:
        return None
    styles: list[_Style] = []
    current: _Style | None = None
    last_sizes: list[str] | None = None
    last_hsn: str | None = None
    for row in grid[start + 1 :]:
        cells = [c.strip() for c in row if c and c.strip()]
        if not cells:
            continue
        if re.search(r"\btotal\b|\bremarks\b", " ".join(cells), re.I):
            break
        first = (row[0] or "").strip()
        has_size_row = any(_MATRIX_LABEL.match(c) and _MATRIX_LABEL.match(c).group(1).lower() == "size" for c in cells)  # type: ignore[union-attr]
        numbered = bool(_LINE_NUMBER.match(first)) and len(cells) > 1
        if numbered:
            current = _Style(number=int(first.rstrip(".")), hsn=last_hsn, sizes=last_sizes, names=[])
            styles.append(current)
        rest = cells[1:] if numbered else cells
        # A size row after the style's own sub-rows is the next style's heading (its number is
        # on a later row in this reading): it only updates the sizes / HSN that follow.
        heading = not numbered and has_size_row and current is not None and bool(current.rates or current.qtys)
        target = None if heading else current
        label: str | None = None
        values: list[str] = []
        for cell in rest:
            if hsn := _HSN_HEADING.match(cell):
                last_hsn = hsn.group(1)
                if target is not None:
                    target.hsn = last_hsn
                continue
            if found := _MATRIX_LABEL.match(cell):
                label = found.group(1).lower()
                cell = found.group(2)
                if not cell:
                    continue
            if label is None:
                if target is not None and re.search(r"[A-Za-z]{2}", cell):
                    target.names.append(cell)
                continue
            values += cell.replace("✓", " ").split()
        if label is None:
            continue
        numbers = [parse_amount(t) if re.fullmatch(r"[\d.,]+", t) else None for t in values]
        if totals := _matrix_totals(numbers):
            position, window = totals
            if target is not None:
                target.totals = window  # type: ignore[assignment]
            values, numbers = values[:position], numbers[:position]
        if label == "size":
            last_sizes = _increasing(values)
            if target is not None:
                target.sizes = last_sizes
        elif target is not None and all(n is not None for n in numbers):
            setattr(target, {"qty": "qtys", "rate": "rates", "mrp": "mrps"}[label], numbers)
        if target is not None:
            target.source = f"{target.source} | {' '.join(cells)}".strip(" |")
    return styles


def _best_name(names: list[str]) -> str:
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return max(counts, key=lambda n: (counts[n], len(n))) if counts else ""


def size_matrix_items(grids: list[Grid]) -> tuple[list[LineItem], list[str]] | None:
    """Line items of a size-matrix invoice from every reading of its table (None: not one)."""
    readings = [styles for grid in grids if (styles := _matrix_styles(grid))]
    if not readings:
        return None
    by_number: dict[int, list[_Style]] = {}
    for styles in readings:
        for style in styles:
            by_number.setdefault(style.number, []).append(style)

    items: list[LineItem] = []
    warnings: list[str] = []
    hsn: str | None = None
    for number in sorted(by_number):
        versions = by_number[number]
        totals = next((v.totals for v in versions if v.totals), None)
        if totals is None:
            warnings.append(f"item {number}: its quantity, gross and net amounts could not be read")
            continue
        qty, gross, pct, _, gst, net = totals
        printed_name = _best_name([" ".join(v.names) for v in versions if v.names])
        hsn = next((v.hsn for v in versions if v.hsn), None) or hsn  # a heading covers the styles below it
        source = next(v.source for v in versions if v.totals)
        design, name = None, printed_name
        if parts := _STYLE_NAME.match(printed_name):
            design, name = parts.group(1), parts.group(2).strip().title()

        # Sizes bought: as many as the pieces' per-size readings (one per size), else the qty.
        count = next((len(v.rates) for v in versions if v.rates and sum(v.rates) == gross), None)             or next((len(v.mrps) for v in versions if v.mrps and len(v.mrps) == qty), None) or int(qty)
        printed_sizes = max((v.sizes for v in versions if v.sizes), key=len, default=None)
        exact = next((v.sizes for v in versions if v.sizes and len(v.sizes) == count), None)
        if exact:
            size = Extracted.found(",".join(exact), " ".join(exact))
        elif printed_sizes and len(printed_sizes) > count:
            note = SIZES_GUESSED.format(printed=" ".join(printed_sizes), count=count)
            size = Extracted.check(",".join(printed_sizes[:count]), note, " ".join(printed_sizes))
            warnings.append(f"item {number} ({name}): {note}")
        else:
            size = Extracted()
        mrps = next((v.mrps for v in versions if v.mrps and len(v.mrps) == count), None)
        rates = next((v.rates for v in versions if v.rates and sum(v.rates) == gross), None)
        rate_note = f"gross {gross} / qty {qty}" + (f" (size rates {' '.join(str(r) for r in rates)})" if rates else "")
        items.append(LineItem(
            line_number=Extracted.found(len(items) + 1, source),
            description=Extracted.found(name, printed_name) if name else Extracted(),
            design=Extracted.found(design, printed_name) if design else Extracted(),
            size=size,
            hsn_sac=Extracted.found(hsn, f"HSN {hsn}") if hsn else Extracted(),
            quantity=Extracted.found(qty, source),
            unit=Extracted.found("pcs", source),
            rate=Extracted.found((gross / qty).quantize(Decimal("0.01")), rate_note),
            mrp=(Extracted.found((sum(mrps) / len(mrps)).quantize(Decimal("0.01")), f"average of {' '.join(str(m) for m in mrps)}")
                 if mrps else Extracted()),
            discount_percent=Extracted.found(pct, source),
            tax_rate=Extracted.found(gst, source),
            taxable_value=Extracted.found(net, source),
        ))
    return (items, warnings) if items else None


def parse_items(grid: Grid) -> tuple[list[LineItem], list[str]]:
    """Line items: rows that start with "<line no> <description>" and carry numeric cells.

    Also detects a known table-recognition failure: item numbers attached to the column
    caption row ("No.") mean the rows below are shifted (and often one row is dropped).
    That cannot be repaired without guessing, so every item is marked ``check`` instead.
    Transport bills (a consignor / freight table) are read by ``freight_items`` instead.
    """
    if (freight := freight_items(grid)) is not None:
        return freight
    header = next((row for row in grid if any(c and "disc" in c.lower() for c in row)), None)
    percent_is_discount = header is not None
    items: list[LineItem] = []
    misaligned = False
    for row in grid:
        cells = [c for c in row if c is not None and c != ""]
        if len(cells) < 4:
            continue
        start = _ITEM_START.match(cells[0])
        rest = cells[1:]
        if not start and _LINE_NUMBER.match(cells[0]) and re.search(r"[A-Za-z]{2}", cells[1]) and not _QTY_UNIT.match(cells[1]):
            # Line number and description in separate cells: "1 | Dupatta-540752 | 54075290 | ..."
            start = _ITEM_START.match(f"{cells[0].rstrip('.')} {cells[1]}")
            rest = cells[2:]
        if not start:
            if not items and _norm(cells[0]) in _CAPTION_ROW and _item_values(cells[1:], percent_is_discount):
                misaligned = True
            continue
        values = _item_values(rest, percent_is_discount)
        if values is None:
            continue
        items.append(LineItem(
            line_number=Extracted.found(int(start.group(1)), cells[0]),
            description=Extracted.found(start.group(2).strip(), cells[0]),
            **values,
        ))
    if by_column := _column_items(grid):
        items = merge_items(by_column, items)  # the caption-guided reading wins ties
    if not misaligned:
        return items, []
    flagged = [
        item.model_copy(update={
            "line_number": Extracted.check(item.line_number.value, ITEMS_MISALIGNED, item.line_number.source_text),
            "description": Extracted.check(item.description.value, ITEMS_MISALIGNED, item.description.source_text),
        })
        for item in items
    ]
    return flagged, [ITEMS_MISALIGNED]


def _item_score(item: LineItem) -> int:
    """How trustworthy a reading of an item row is: filled columns, and above all arithmetic."""
    score = sum(getattr(item, key).value is not None for key in ("hsn_sac", "quantity", "rate", "taxable_value"))
    quantity, rate, amount = item.quantity.value, item.rate.value, item.taxable_value.value
    if quantity is not None and rate is not None and amount is not None:
        discount = item.discount_percent.value or Decimal(0)
        if abs(quantity * rate * (100 - discount) / 100 - amount) <= max(Decimal(1), abs(amount) * Decimal("0.005")):
            score += 4
    if item.line_number.status == "check":
        score -= 3
    return score


def merge_items(*sources: list[LineItem]) -> list[LineItem]:
    """One row per line number, from several readings of the same table (best score wins;
    on a tie the earlier source). Rows cut at a band edge lose to their complete copy."""
    best: dict[int, tuple[int, LineItem]] = {}
    for items in sources:
        for item in items:
            number = item.line_number.value
            if number is None:
                continue
            score = _item_score(item)
            if number not in best or score > best[number][0]:
                best[number] = (score, item)
    merged: list[LineItem] = []
    for number in sorted(best):
        item = best[number][1]
        # Columns the winner lacks, from another reading of the same row (same amount).
        same_row = [other for items in sources for other in items
                    if other.line_number.value == number and other.taxable_value.value == item.taxable_value.value]
        gaps = {key: next(o for o in same_row if getattr(o, key).value is not None).__getattribute__(key)
                for key in LineItem.model_fields
                if getattr(item, key).value is None and any(getattr(o, key).value is not None for o in same_row)}
        merged.append(item.model_copy(update=gaps) if gaps else item)
    return merged


def _tax_summary(grid: Grid) -> dict[str, Extracted[Decimal]]:
    """Totals from the HSN-wise tax summary ("Taxable Value | CGST | SGST/UTGST | IGST | Total Tax").

    Reads each column from the per-HSN rows (summed) and from the "Total" row. They must agree;
    if not, the per-HSN sum is used and marked ``check`` (a shifted "Total" row was seen in
    practice: its cells moved one column right).
    """
    out: dict[str, Extracted[Decimal]] = {}
    for h, header in enumerate(grid):
        if not any(c and "taxable value" in c.lower() for c in header):
            continue
        spans: dict[str, tuple[int, int]] = {}
        for c, cell in enumerate(header):
            if not cell:
                continue
            end = c + 1
            while end < len(header) and header[end] is None:
                end += 1
            key = cell.lower()
            for name, needle in (("subtotal", "taxable value"), ("cgst", "cgst"), ("sgst", "sgst"), ("igst", "igst"), ("tax", "total tax")):
                if needle in key and name not in spans:
                    spans[name] = (c, end)
        rows = grid[h + 1 :]
        total_row = next((row for row in rows if row and row[0] and _norm(row[0]).startswith("total")), None)
        data_rows = [row for row in rows[: rows.index(total_row) if total_row else len(rows)]
                     if row and row[0] and re.fullmatch(r"\d{4,8}", row[0])]

        def column(row: list[str | None], start: int, end: int) -> tuple[Decimal | None, str]:
            cells = " ".join(c for c in row[start:end] if c)
            numbers = amounts_in(cells)
            return (numbers[-1] if numbers else None), cells

        for name, (start, end) in spans.items():
            per_hsn = [column(row, start, end) for row in data_rows]
            per_hsn_sum = sum((v for v, _ in per_hsn if v is not None), Decimal(0)) if per_hsn and all(v is not None for v, _ in per_hsn) else None
            total, total_text = column(total_row, start, end) if total_row else (None, "")
            if total is not None and (per_hsn_sum is None or per_hsn_sum == total):
                out[name] = Extracted.found(total, total_text)
            elif per_hsn_sum is not None and total is not None:
                out[name] = Extracted.check(
                    per_hsn_sum, f"tax summary disagrees: HSN rows give {per_hsn_sum}, the Total row {total}",
                    " | ".join(text for _, text in per_hsn),
                )
            elif per_hsn_sum is not None:
                out[name] = Extracted.found(per_hsn_sum, " | ".join(text for _, text in per_hsn))
        break
    return out


_WHOLE_DATE = re.compile(r"\d{1,2}[-/.](?:\d{1,2}|[A-Za-z]{3,9})[-/.]\d{2,4}")


def _invoice_number_in_text(text: str) -> tuple[str, str] | None:
    """(number, source) after an "Invoice No." / "Bill No." caption in free text.

    The caption is often followed by other captions on the same line ("Invoice No. : Dated")
    with the values on the next line, so the first token containing a digit wins.
    """
    for match in re.finditer(r"\b(?:invoice|bill)\s*(?:no|number)\b\.?", text, re.I):
        tail = re.sub(r"\s*/\s*", "/", text[match.end() : match.end() + 120])
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9/\-]*", tail)[:8]:
            if not re.search(r"\d", token) or _WHOLE_DATE.fullmatch(token):
                continue
            if re.fullmatch(r"\d{10,}", token):
                continue  # e-way bill / acknowledgement numbers printed in neighbouring boxes
            return token, f"{match.group()} {token}"
    return None


def date_in_text(text: str) -> tuple[dt.date, str] | None:
    """(date, source) after a "Date" / "Dated" / "Invoice Date" caption in free text."""
    for match in re.finditer(r"^.*?(?:invoice|bill)?[ ]*dat(?:e|ed)[ ]*[:.-]*[ ]*(.+)$", text, re.I | re.M):
        if parsed := parse_date(match.group(1)):
            return parsed, match.group(0).strip()
    return None


def map_invoice(
    header_text: str, tables: list[str], footer_text: str = "", bands: list[str] | None = None
) -> tuple[Invoice, list[str]]:
    """Build an ``Invoice`` from header OCR text, per-page table markup and footer OCR text.

    ``bands`` are band readings of the item table (see ``invoice_bands``), merged with the
    items of the whole-page tables row by row.
    """
    warnings: list[str] = []
    grids = [parse_otsl(t) for t in tables]
    first = grids[0] if grids else []
    everything = "\n".join([header_text, *tables, footer_text])
    fields: dict[str, object] = {}

    # --- Header boxes (first page) -------------------------------------------
    if (found := value_for_label(first, "invoice no", "invoice number")) and re.search(r"\d", found[0]):
        joined = re.sub(r"\s*/\s*", "/", found[0])  # "758 / 26-27" -> "758/26-27"
        fields["invoice_number"] = Extracted.found(joined.split()[0], f"{found[1]}: {found[0]}")
    elif in_text := _invoice_number_in_text(header_text):
        fields["invoice_number"] = Extracted.found(*in_text)
    if found := value_for_label(first, "dated", "invoice date", "date"):
        if parsed := parse_date(found[0]):
            fields["invoice_date"] = Extracted.found(parsed, f"{found[1]}: {found[0]}")
        else:
            fields["invoice_date"] = Extracted.check(None, f"could not read the date {found[0]!r}", found[0])
    if (fields.get("invoice_date") is None or fields["invoice_date"].value is None) and (dated := date_in_text(header_text)):  # type: ignore[attr-defined]
        fields["invoice_date"] = Extracted.found(*dated)
    for key, labels in (
        ("delivery_note", ("delivery note",)),
        ("payment_terms", ("mode/terms of payment", "terms of payment", "payment terms")),
        ("place_of_supply", ("place of supply",)),
    ):
        if (found := value_for_label(first, *labels)) and not (key == "delivery_note" and "date" in _norm(found[1])):
            value = format_state(found[0]) if key == "place_of_supply" else found[0]
            fields[key] = Extracted.found(value, f"{found[1]}: {found[0]}")

    # --- Parties (header OCR) ----------------------------------------------------
    # The seller block comes first on GST invoices; the buyer is the block captioned
    # "Buyer"/"Bill to", else the next uncaptioned one. "Consignee (Ship to)" is skipped.
    parties = parse_parties(header_text)
    vendor = next((p for p in parties if p.role is None), None)
    buyer = next((p for p in parties if p.role == "buyer"), None) or next(
        (p for p in parties if p.role is None and p is not vendor), None
    )
    extra: list[_Party] = []
    if (vendor is None or buyer is None) and tables:
        # The header crop can end above a party's GSTIN line (layouts with tall header blocks);
        # the whole-page table of page 1 holds the same blocks as cell text.
        known = {p.gstin.value for p in (vendor, buyer) if p}
        extra = [p for p in parse_parties(_cell_text(tables[0])) if p.gstin.value not in known]
        vendor = vendor or next((p for p in extra if p.role is None), None)
        buyer = buyer or next((p for p in extra if p.role == "buyer"), None) or next(
            (p for p in extra if p.role is None and p is not vendor), None
        )
    if buyer is None:
        consignee = next((p for p in [*parties, *extra] if p.role == "consignee"), None)
        if consignee is not None:  # "Bill to" and "Ship to" side by side: only one block was read
            buyer = consignee
            warnings.append("no 'Bill to' block read; the buyer is taken from the consignee (ship to) block")
    if buyer is None:
        seller_gstin = vendor.gstin.value if vendor else None
        other = next((g for line in everything.splitlines() if (g := find_gstin(line)) and g.status == "ok" and g.value != seller_gstin), None)
        buyer = _captioned_buyer(header_text, other or Extracted())
    if buyer is not None and buyer.name is None:
        # Side-by-side "Bill to | Ship to" blocks: the named copy may carry the other caption.
        donor = next((p for p in [*parties, *extra] if p.name and p.gstin.value == buyer.gstin.value), None)
        if donor is not None:
            buyer = _Party(name=donor.name, address=donor.address, gstin=buyer.gstin, state=buyer.state or donor.state, role=buyer.role)
    if vendor is None and buyer is None and parties:
        warnings.append("GSTINs found but the seller and buyer blocks could not be told apart")
    fields["vendor_name"], fields["vendor_address"], fields["vendor_gstin"] = _party_fields(vendor)
    fields["buyer_name"], fields["buyer_address"], fields["buyer_gstin"] = _party_fields(buyer)
    # The signature line carries the seller's full legal name; the header block is often
    # clipped at the page edge (photos) or abbreviated, so the signature wins.
    if signature := parse_signature(footer_text):
        fields["vendor_name"] = Extracted.found(signature, f"for {signature}")
        # The header block is the name (possibly clipped, e.g. by the photo's edge) followed by
        # the address. The address starts after the line that matches the signature; if no line
        # does, the name was clipped off entirely and every line is address.
        if vendor:
            lines = ([vendor.name] if vendor.name else []) + vendor.address
            name_at = next((i for i, line in enumerate(lines) if _similar(line, signature)), None)
            address = lines[name_at + 1 :] if name_at is not None else lines
            fields["vendor_address"] = (
                Extracted.found(", ".join(address), "\n".join(address)) if address else Extracted()
            )
    if "place_of_supply" not in fields and (match := re.search(r"place\s+of\s+supply\s*[:\-]?\s*([A-Za-z][A-Za-z ]+(?:\(\d{2}\))?)", everything, re.I)):
        fields["place_of_supply"] = Extracted.found(format_state(match.group(1).strip()), match.group(0).strip())
    if "place_of_supply" not in fields and buyer and buyer.state:
        fields["place_of_supply"] = Extracted.check(
            format_state(buyer.state), "no 'Place of Supply' printed; taken from the buyer's state", buyer.state
        )
    buyer_gstin = fields.get("buyer_gstin")
    if "place_of_supply" not in fields and isinstance(buyer_gstin, Extracted) and buyer_gstin.status == "ok" and buyer_gstin.value:
        fields["place_of_supply"] = Extracted.check(
            format_state(buyer_gstin.value[:2]), "no 'Place of Supply' printed; from the buyer's GSTIN state code", buyer_gstin.value
        )

    if "INR" in everything or "₹" in everything or "Rs." in everything:
        fields["currency"] = Extracted.found("INR", "INR/₹ on the invoice")
    elif fields.get("vendor_gstin") and fields["vendor_gstin"].value:  # type: ignore[attr-defined]
        fields["currency"] = Extracted.check("INR", "not printed; an Indian GST invoice (GSTIN) is billed in INR")

    # --- Items (all pages) ---------------------------------------------------------
    items: list[LineItem] = []
    item_warnings: list[str] = []
    from app.services.purchase_order import is_purchase_order, purchase_order_fields, purchase_order_items

    order = is_purchase_order("\n".join([header_text, *tables]))
    order_fields: dict[str, Extracted[Any]] = {}
    if order:
        # A handwritten purchase order: its own reader for the lines and the form's labels.
        all_grids = [*grids, *(parse_otsl(band) for band in bands or [])]
        order_fields, labelled = purchase_order_fields(header_text, all_grids)
        items, item_warnings = purchase_order_items(all_grids, labelled.get("item"))
    elif matrix := size_matrix_items([*grids, *(parse_otsl(band) for band in bands or [])]):
        items, item_warnings = matrix  # garment size matrix: every reading at once, one line per size
    else:
        for grid in grids:
            page_items, page_warnings = parse_items(grid)
            items += page_items
            item_warnings += page_warnings
        band_items = [item for band in bands or [] for item in parse_items(parse_otsl(band))[0]]
        if band_items:
            items = merge_items(band_items, items)
            item_warnings = [note for note in (ITEMS_MISALIGNED, FREIGHT_REALIGNED) if any(i.line_number.note == note for i in items)]
    warnings += item_warnings
    if not items:
        warnings.append("no line items found")

    # --- Totals (last page with a summary) ---------------------------------------------
    for grid in reversed(grids):
        summary = _tax_summary(grid)
        if summary:
            for key in ("subtotal", "cgst", "sgst", "igst"):
                if key in summary:
                    fields[key] = summary[key]
            break
    band_grids = [parse_otsl(band) for band in bands or []]
    for key, value in _column_summary([*grids, *band_grids]).items():
        fields.setdefault(key, value)
    labelled = _labelled_amounts([*grids, *band_grids])
    plain_total = labelled.pop("plain_total", None)
    for key, value in labelled.items():
        fields.setdefault(key, value)
    for grid in reversed(grids):
        for row in grid:
            cells = [c for c in row if c]
            if not cells:
                continue
            label = _norm(cells[0])
            if label.startswith("total") and "total_amount" not in fields:
                for cell in cells[1:]:
                    if (qty := _QTY_UNIT.match(cell)) and "total_quantity" not in fields:
                        fields["total_quantity"] = Extracted.found(_quantity_number(qty.group(1)), cell)
                money = [c for c in cells[1:] if not _QTY_UNIT.match(c) and amounts_in(c)]
                if money:
                    fields["total_amount"] = Extracted.found(amounts_in(money[-1])[0], money[-1])
            if re.search(r"round\s*off|\broff\b", " ".join(cells), re.I) and "round_off" not in fields:
                small = [n for n in amounts_in(" ".join(cells[1:])) if abs(n) < 5]
                if small:
                    fields["round_off"] = Extracted.found(small[-1], " ".join(cells))
        if "total_amount" in fields:
            break

    if "total_amount" not in fields:
        for text in (footer_text, header_text):
            if match := re.search(r"(?:net|grand)\s*(?:total|amount)[^\d\n]{0,25}([\d,]+\.\d{2})", text, re.I):
                fields["total_amount"] = Extracted.found(parse_amount(match.group(1)), match.group(0))
                break
    if plain_total is not None:
        fields.setdefault("total_amount", plain_total)
    _totals_from_items(fields, items, everything)
    _derive_round_off(fields)

    number = fields.get("invoice_number")
    if isinstance(number, Extracted) and number.value:
        fixed, changed = normalize_invoice_number(number.value, everything)
        if changed:
            fields["invoice_number"] = number.model_copy(
                update={"value": fixed, "note": f"year separator normalised from {number.value!r}"}
            )
    fields.update(order_fields)
    return Invoice(items=items, **fields), warnings  # type: ignore[arg-type]


_GRAND_TOTAL = re.compile(r"^(?:net|grand)\s*(?:total|amount)|amount after tax", re.I)
_SUBTOTAL = re.compile(r"^(?:taxable\s*(?:amount|amt|value)|sub\s*total|total taxable)", re.I)
_TAX_LABEL = re.compile(r"\b(igst|cgst|sgst|utgst)\b", re.I)
_GST_RATES = {Decimal(r) for r in ("0.25", "3", "5", "12", "18", "28", "1.5", "2.5", "6", "9", "14")}


def _column_summary(grids: list[Grid]) -> dict[str, Extracted[Decimal]]:
    """A tax summary laid out as a table: captions in one row ("HsnCode | Qty | Taxable Amt |
    GST % | IGST | Net Amount"), the amounts under them in the next. Needs both the taxable and
    the net caption, so an item table's "Net Amount" column is not taken for it."""
    for grid in grids:
        for row, below in zip(grid, grid[1:], strict=False):
            captions = {i: _norm(c) for i, c in enumerate(row) if c and len(c) <= 30}
            taxable = next((i for i, t in captions.items() if _SUBTOTAL.search(t)), None)
            net = next((i for i, t in captions.items() if _GRAND_TOTAL.search(t)), None)
            if taxable is None or net is None or net < taxable:
                continue

            def value(index: int) -> Decimal | None:
                return parse_amount(below[index]) if index < len(below) and below[index] else None

            source = " | ".join(c for c in [*row, *below] if c)
            out: dict[str, Extracted[Decimal]] = {}
            for key, index in (("subtotal", taxable), ("total_amount", net)):
                if (amount := value(index)) is not None:
                    out[key] = Extracted.found(amount, source)
            for index, text in captions.items():
                if taxable < index < net and text in ("igst", "cgst", "sgst", "utgst") and (amount := value(index)) is not None:
                    out.setdefault("sgst" if text == "utgst" else text, Extracted.found(amount, source))
            if "subtotal" in out and "total_amount" in out and out["total_amount"].value >= out["subtotal"].value:  # type: ignore[operator]
                return out
    return {}


def _labelled_amounts(grids: list[Grid]) -> dict[str, Extracted[Decimal]]:
    """Amounts printed right of a caption: "Grand Total | 85951.00", "IGST 5% | 2049.00",
    "Taxable Amount | 81858.30". For layouts without a Tally tax summary; the first match wins."""
    out: dict[str, Extracted[Decimal]] = {}
    for grid in grids:
        for row in grid:
            cells = [c for c in row if c]
            for index, cell in enumerate(cells):
                if len(cell) > 40:
                    continue
                following = cells[index + 1 :]
                amounts = [a for c in following if not _PERCENT.match(c) for a in amounts_in(c)
                           if abs(a) >= 1 and a not in _GST_RATES]
                source = " | ".join([cell, *following])
                label = _norm(cell)
                # A single-value caption's amount is in the very next cell (not further right, where the
                # next caption's value is: "Taxable Amt | GST % | IGST | 1804.95").
                value_cell = next((c for c in following if not _QTY_UNIT.match(c) and not _PERCENT.match(c)), "")
                if re.fullmatch(r"round\s*off|roff", label) and "round_off" not in out:
                    # "Round Off | (-)0.25": the sign is printed as "(-)" or "-".
                    small = amounts_in(value_cell)
                    if small and abs(small[0]) < 5:
                        sign = -1 if re.match(r"\s*\(?-", value_cell) else 1
                        out["round_off"] = Extracted.found(sign * abs(small[0]), source)
                if not amounts:
                    continue
                adjacent = [a for a in amounts_in(value_cell) if abs(a) >= 1]
                if _GRAND_TOTAL.search(label):
                    if adjacent:
                        out.setdefault("total_amount", Extracted.found(adjacent[-1], source))
                elif label == "total":  # a plain "Total :" caption mid-row; used only if nothing better
                    money = [a for c in following if not _QTY_UNIT.match(c) for a in amounts_in(c) if abs(a) >= 1]
                    if money:
                        out.setdefault("plain_total", Extracted.found(money[-1], source))
                elif _SUBTOTAL.search(label):
                    if adjacent:
                        out.setdefault("subtotal", Extracted.found(adjacent[0], source))
                elif _TAX_LABEL.search(cell) and "taxable" not in label and "total" not in label:
                    # One cell may stack several captions ("Discount on Sale IGST on Sale") over as
                    # many amounts, preceded by the items total: the last amounts pair up in order.
                    captions = [
                        "round_off" if re.match(r"r", m.group(1), re.I) else m.group(1).lower()
                        for m in re.finditer(r"\b(discount|igst|cgst|sgst|utgst|round\s*off|roff)\b", cell, re.I)
                    ]
                    signed = [a for c in following if not _PERCENT.match(c) for a in amounts_in(c) if a not in _GST_RATES]
                    # More amounts than captions: the extra ones come first (the items total). Fewer:
                    # the later captions are blank (e.g. no round off), so pair from the first.
                    paired = signed[len(signed) - len(captions) :] if len(signed) >= len(captions) else signed
                    if paired:
                        for name, amount in zip(captions, paired, strict=False):
                            if name == "round_off":
                                continue  # read by the round-off rule (its sign needs the totals)
                            key = "sgst" if name == "utgst" else name
                            out.setdefault(key, Extracted.found(abs(amount), source))
    return out


def _totals_from_items(fields: dict[str, object], items: list[LineItem], document: str) -> None:
    """Subtotal / total quantity not captioned in a way the rules know: the sum of the items,
    when that exact figure is printed on the invoice (so it was read, not just computed)."""
    printed = {a for a in amounts_in(document.replace("\n", " "))}
    amounts = [i.taxable_value.value for i in items]
    if "subtotal" not in fields and len(items) >= 2 and all(a is not None for a in amounts):
        total = sum(amounts, Decimal(0))  # type: ignore[arg-type]
        if total in printed:
            fields["subtotal"] = Extracted.found(total, f"{total} (sum of the items, printed on the invoice)")
    quantities = [i.quantity.value for i in items]
    if "total_quantity" not in fields and items and all(q is not None for q in quantities):
        total = sum(quantities, Decimal(0))  # type: ignore[arg-type]
        if total in printed:
            fields["total_quantity"] = Extracted.found(total, f"{total} (sum of the quantities, printed on the invoice)")


def _derive_round_off(fields: dict[str, object]) -> None:
    """Round off not readable (e.g. merged into the tax amount's cell)? total - subtotal - taxes
    gives it when that is a few paise; marked ``check`` since it was computed, not read."""
    def amount(key: str) -> Decimal | None:
        field = fields.get(key)
        return field.value if isinstance(field, Extracted) and isinstance(field.value, Decimal) else None

    subtotal, total = amount("subtotal"), amount("total_amount")
    taxes = [amount(key) for key in ("cgst", "sgst", "igst")]
    if subtotal is None or total is None or all(tax is None for tax in taxes):
        return
    difference = total - subtotal - sum((tax for tax in taxes if tax is not None), Decimal(0))
    read = fields.get("round_off")
    if isinstance(read, Extracted) and read.value is not None:
        if read.value and read.value == -difference:  # "ROUND OFF 0.30" printed without its minus sign
            fields["round_off"] = read.model_copy(update={
                "value": difference, "note": f"printed as {read.value}; the total shows it is subtracted"})
        return
    if difference and abs(difference) < 1:
        fields["round_off"] = Extracted.check(
            difference, "not readable on the invoice; computed as total - subtotal - taxes", None
        )


def _similar(a: str, b: str) -> bool:
    a, b = a.upper(), b.upper()
    return a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() >= 0.6


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


_SECTION = re.compile(r"^--- (header OCR|table|items band|region OCR|re-read|fallback|footer OCR) \(page (\d+)[^\n]*---\n", re.M)
_ZOOM_SECTION = re.compile(r"^--- (region OCR|re-read) \(page \d+, ([a-z_]+)")


def split_raw_text(raw_text: str) -> tuple[str, list[str], str]:
    """Inverse of the ``raw_text`` layout: (header OCR, per-page tables, footer OCR).

    Lets saved results be re-mapped with the current rules without running OCR again.
    """
    header, tables, footer = "", [], ""
    matches = list(_SECTION.finditer(raw_text))
    for match, following in zip(matches, [*matches[1:], None], strict=True):
        body = raw_text[match.end() : following.start() if following else len(raw_text)].rstrip("\n")
        if match.group(1) == "header OCR":
            header = body
        elif match.group(1) == "table":
            tables.append(body)
        elif match.group(1) == "footer OCR":
            footer = body
    return header, tables, footer


def split_fallback(raw_text: str) -> tuple[str | None, list[dict[str, Any]]]:
    """The fallback model's name and its per-page JSON readings saved in ``raw_text`` (see
    ``invoice_fallback``), so saved results can be re-decided without running the model."""
    matches = list(_SECTION.finditer(raw_text))
    name, readings = None, []
    for match, following in zip(matches, [*matches[1:], None], strict=True):
        if match.group(1) != "fallback":
            continue
        name = name or (re.search(r", ([^)]+)\) ---", match.group(0)) or [None, None])[1]
        body = raw_text[match.end() : following.start() if following else len(raw_text)]
        try:
            readings.append(json.loads(body))
        except ValueError:
            continue
    return name, readings


def split_bands(raw_text: str, reread: bool = True) -> list[str]:
    """The band readings of the item table saved in ``raw_text`` (see ``split_raw_text``);
    ``reread=False`` leaves out the item re-read's shorter bands (to compare without them)."""
    matches = list(_SECTION.finditer(raw_text))
    return [
        raw_text[match.end() : following.start() if following else len(raw_text)].rstrip("\n")
        for match, following in zip(matches, [*matches[1:], None], strict=True)
        if match.group(1) == "items band" and (reread or "re-read band" not in match.group(0))
    ]


def split_zoom(raw_text: str) -> list[tuple[str, str, str]]:
    """The zoomed readings saved in ``raw_text``: (kind, label, text), kind ``region OCR``
    (label: the block) or ``re-read`` (label: the field); see ``invoice_reread``."""
    matches = list(_SECTION.finditer(raw_text))
    out: list[tuple[str, str, str]] = []
    for match, following in zip(matches, [*matches[1:], None], strict=True):
        if zoom := _ZOOM_SECTION.match(match.group(0)):
            body = raw_text[match.end() : following.start() if following else len(raw_text)].rstrip("\n")
            out.append((zoom.group(1), zoom.group(2), body))
    return out


def validate_extraction(result: InvoiceExtraction, invoice: Invoice | None = None) -> InvoiceExtraction:
    """Validate ``invoice`` (default: the result's own) against the result's OCR text."""
    header, tables, footer = split_raw_text(result.raw_text)
    checked, report = validate_invoice(invoice or result.invoice, "\n".join([header, *tables, footer]))
    return result.model_copy(update={"invoice": checked, "validation": report})


class ExtractionUnavailableError(ModelError):
    """The serving model cannot do document extraction (it lacks the OCR/table tasks)."""


def crop_band(page: bytes, fraction: float, *, bottom: bool = False) -> bytes:
    """The top (or bottom) ``fraction`` of a page, as PNG."""
    with Image.open(io.BytesIO(page)) as image:
        width, height = image.size
        band = max(1, round(height * fraction))
        box = (0, height - band, width, height) if bottom else (0, 0, width, band)
        crop = image.convert("RGB").crop(box)
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        return buffer.getvalue()


def is_purchase_order_text(text: str) -> bool:
    from app.services.purchase_order import is_purchase_order

    return is_purchase_order(text)


# The pipeline stage the running task's model calls belong to (per asyncio task, so calls made
# side by side are attributed correctly in the profile).
_STAGE: contextvars.ContextVar[str] = contextvars.ContextVar("invoice_stage", default="start")


@dataclass(frozen=True)
class ZoomOptions:
    """Zoomed crops (``invoice_reread``); they need a reasoning model that can locate boxes."""

    region_crops: bool = True
    reread_max_fields: int = 4
    reread_items: bool = True
    max_scale: float = 2.0
    # Overlap independent calls on the two model servers (see ``read``).
    parallel: bool = True
    # PaddleOCR requests in flight at once (must match the model client's setting): >1 reads the
    # whole pages, footer, header and item bands side by side.
    ocr_concurrency: int = 1
    # Crop only the blocks whose fields are missing, flagged or failing a check.
    targeted_blocks: bool = True
    # The whole document's time budget: zoomed readings are skipped once most of it is used,
    # so they never turn a slow extraction into a timeout.
    budget_seconds: float | None = None

    @classmethod
    def from_settings(cls, settings: "Settings") -> "ZoomOptions":
        return cls(
            region_crops=settings.invoice_region_crops,
            reread_max_fields=settings.invoice_reread_max_fields,
            reread_items=settings.invoice_reread_items,
            max_scale=settings.invoice_reread_scale,
            parallel=settings.invoice_parallel_calls,
            ocr_concurrency=settings.llama_cpp_concurrency,
            targeted_blocks=settings.invoice_targeted_blocks,
            budget_seconds=settings.invoice_timeout_seconds,
        )


REGION_MAX_TOKENS = 512
TOTALS_MAX_TOKENS = 1024
REREAD_MAX_TOKENS = 128
REGION_BUDGET_SHARE = 0.55  # start the block crops only within the first 55% of the budget
REREAD_BUDGET_SHARE = 0.75  # and each field re-read within the first 75%
FALLBACK_BUDGET_SHARE = 0.6  # the hybrid fallback (one slow model call per page) within the first 60%
ITEMS_BUDGET_SHARE = 0.5  # the item re-read (a second pass over the table) within the first half
REREAD_BAND_FRACTION = 0.08  # re-read bands: about half as tall, so cut elsewhere and not shrunk
ITEM_CHECKS = ("line_arithmetic", "items_sum", "line_numbers")


_LABELS_FOR_PEOPLE = {
    "invoice_number": "invoice number", "invoice_date": "invoice date", "vendor_name": "seller name",
    "vendor_gstin": "seller GSTIN", "buyer_name": "buyer name", "buyer_gstin": "buyer GSTIN",
    "total_amount": "grand total", "subtotal": "taxable total", "place_of_supply": "place of supply",
}


def _label(name: str) -> str:
    return _LABELS_FOR_PEOPLE.get(name, name.replace("_", " "))


def _summary(invoice: Invoice) -> str:
    """One line on what the rules made of the first readings."""
    parts = []
    if invoice.invoice_number.value:
        parts.append(f"invoice {invoice.invoice_number.value}")
    if invoice.invoice_date.value:
        parts.append(f"dated {invoice.invoice_date.value:%d-%m-%Y}")
    parts.append(f"{len(invoice.items)} item row(s)")
    if invoice.total_amount.value is not None:
        parts.append(f"total {invoice.total_amount.value:,.2f}")
    return "Rules mapped the readings: " + ", ".join(parts)


def item_faults(invoice: Invoice, document: str) -> list[str]:
    """Names of the failed item checks (line amounts, items adding up, line numbers)."""
    _, report = validate_invoice(invoice, document)
    return [check.name for check in report.checks if check.id in ITEM_CHECKS and check.status == "fail"]


class InvoiceExtractor:
    """OCR (PaddleOCR-VL) + rule-based mapping, cross-checked by an optional reasoning model."""

    def __init__(
        self, model: VisionModel, mapper: "LlmFieldMapper | None" = None, zoom: ZoomOptions | None = None,
        progress: "ProgressFn | None" = None, fallback: "FallbackReader | None" = None,
    ) -> None:
        if not model.tasks or OCR_TASK not in model.tasks or TABLE_TASK not in model.tasks:
            raise ExtractionUnavailableError(
                f"Invoice extraction needs PaddleOCR-VL (tasks {OCR_TASK!r} and {TABLE_TASK!r}); "
                f"the serving model is {model.name!r}"
            )
        self._model = model
        self._mapper = mapper
        self._fallback = fallback
        self._zoom = zoom or ZoomOptions()
        self._say: ProgressFn = progress or silent  # live comments (see invoice_progress)
        self._usage = ExtractionUsage()
        self._started = time.perf_counter()
        self._reset_profile()

    # --- Profiling: time per stage and model calls (internal: logged, not returned by the API) ---

    def _reset_profile(self) -> None:
        self._stage = "start"
        self._stage_started = time.perf_counter()
        self._stage_ms: dict[str, float] = {}
        self._model_ms: dict[str, float] = {}
        self._calls: dict[str, int] = {}
        self._stage_calls: dict[str, int] = {}

    def _mark(self, stage: str) -> None:
        """Main flow: close the running stage's wall-clock timer and start ``stage``'s (a stage met
        twice adds up); also names the stage the following model calls belong to."""
        now = time.perf_counter()
        self._stage_ms[self._stage] = self._stage_ms.get(self._stage, 0.0) + (now - self._stage_started) * 1000
        self._stage, self._stage_started = stage, now
        _STAGE.set(stage)

    @staticmethod
    def _enter(stage: str) -> None:
        """Inside a helper that may run concurrently: name the stage of this task's model calls."""
        _STAGE.set(stage)

    def _count(self, model: str) -> None:
        self._calls[model] = self._calls.get(model, 0) + 1
        key = f"{_STAGE.get()}:{model}"
        self._stage_calls[key] = self._stage_calls.get(key, 0) + 1

    def _spent(self, started: float) -> None:
        """Add a finished model call's duration to its stage's model time."""
        stage = _STAGE.get()
        self._model_ms[stage] = self._model_ms.get(stage, 0.0) + (time.perf_counter() - started) * 1000

    @property
    def profile(self) -> dict[str, Any]:
        """For the last document: wall-clock ms per stage of the main flow (``stages_ms``), ms spent
        in model calls per stage (``model_ms``; overlapping calls both count), model calls by model
        and by stage, and the total."""
        return {"stages_ms": {name: round(ms) for name, ms in self._stage_ms.items()},
                "model_ms": {name: round(ms) for name, ms in self._model_ms.items()},
                "calls": dict(self._calls), "calls_by_stage": dict(self._stage_calls),
                "total_ms": round(sum(self._stage_ms.values()))}

    async def _analyze(self, image: bytes, task: str, options: dict[str, object]) -> str:
        self._count("paddleocr")
        started = time.perf_counter()
        try:
            output = await self._model.analyze([image], task, options)
        finally:
            self._spent(started)
        self._usage.input_tokens += output["input_tokens"]
        self._usage.output_tokens += output["output_tokens"]
        return output["text"]

    async def extract(self, pages: list[bytes]) -> InvoiceExtraction:
        """Read the document, then validate it (see ``read`` and ``validate_extraction``)."""
        read = await self.read(pages)
        self._mark("validation")
        result = validate_extraction(read)
        self._mark("done")
        return result

    async def read(self, pages: list[bytes]) -> InvoiceExtraction:
        """OCR, rule mapping and the reasoning-model cross-check; no validation yet."""
        if not pages:
            raise ValueError("at least one page is required")
        started = self._started = time.perf_counter()
        self._usage = ExtractionUsage()
        self._reset_profile()
        warnings: list[str] = []
        say = self._say
        self._mark("deskew")
        say("start", f"Received {len(pages)} page(s); straightening the photo")
        straightened = await asyncio.gather(*(asyncio.to_thread(deskew, page) for page in pages))
        pages = [page for page, _ in straightened]
        for number, (_, angle) in enumerate(straightened, 1):
            say("deskew", f"Page {number}: rotated by {angle:+.1f}° so the text lines are level" if angle
                else f"Page {number} is already level", page=number)

        parallel = self._zoom.parallel
        self._mark("table_detection")
        say("locate", "Asking Qwen3-VL where the item table starts", page=1)
        locate_task = asyncio.ensure_future(self._locate(pages))
        tables: list[str] = []
        footer_text = ""
        side_by_side = parallel and self._zoom.ocr_concurrency > 1
        tables_task: "asyncio.Future[list[str]] | None" = None
        footer_task: "asyncio.Future[str] | None" = None
        if side_by_side:
            # Two PaddleOCR requests in flight: the whole pages and the footer start now, beside
            # the table locator; the header and the item bands join once the table is found.
            tables_task = asyncio.ensure_future(self._read_tables(pages, warnings))
            footer_task = asyncio.ensure_future(self._read_footer(pages))
        elif parallel:
            # The whole-page tables and the footer do not depend on where the item table is:
            # PaddleOCR reads them while Qwen3-VL-2B locates it (two servers share the GPU).
            self._mark("table_ocr")
            tables = await self._read_tables(pages, warnings)
            self._mark("footer_ocr")
            footer_text = await self._read_footer(pages)
            self._mark("table_detection")
        locations, locate_warnings = await locate_task
        for number, location in enumerate(locations, 1):
            if location is None:
                say("locate", f"Page {number}: no item table found; the whole page will be read", level="warn", page=number)
            else:
                say("locate", f"Page {number}: item table caption row found at {location.caption_top:.0%} of the page",
                    page=number, region=(0.0, location.caption_top, 1.0, location.caption_bottom))
        warnings += locate_warnings
        # The header (seller and buyer blocks) ends where the item table starts; without a
        # located table, the top HEADER_FRACTION of the page.
        header_fraction = HEADER_FRACTION
        if locations and locations[0] is not None:
            header_fraction = min(MAX_HEADER_FRACTION, max(HEADER_FRACTION, locations[0].caption_top + 0.01))
        say("header", f"Reading the seller and buyer blocks (top {header_fraction:.0%} of page 1)",
            page=1, region=(0.0, 0.0, 1.0, header_fraction))
        bands_task = None
        if side_by_side:
            self._mark("ocr_side_by_side")
            bands_task = asyncio.ensure_future(self._read_bands(pages, locations))
        else:
            self._mark("header_ocr")
        header_image = await asyncio.to_thread(crop_band, pages[0], header_fraction)
        self._enter("header_ocr")
        header_text, looped = await self._ocr(header_image, HEADER_MAX_TOKENS)
        if tables_task is not None and footer_task is not None:
            tables, footer_text = await tables_task, await footer_task
        for party in parse_parties(header_text)[:3]:
            checked = "checksum OK" if party.gstin.status == "ok" else "checksum does not match"
            say("header", f"Found {party.name or 'a party'}: GSTIN {party.gstin.value} ({checked})",
                level="ok" if party.gstin.status == "ok" else "warn", page=1)
        if looped:
            warnings.append("header OCR repeated itself twice; the repeated tail was removed, review the parties")

        if not parallel:
            self._mark("table_ocr")
            tables = await self._read_tables(pages, warnings)

        # The cross-check reads only the header, whole-page and footer OCR text: in parallel mode
        # Qwen3-VL-2B starts it now and works while PaddleOCR reads the item bands.
        cross_task: "asyncio.Future[tuple[dict[str, str | None], int, int]] | None" = None
        if parallel and self._mapper is not None:
            from app.services.invoice_llm import document_text  # imports this module

            self._enter("qwen2b_crosscheck")
            self._count("qwen2b")
            cross_task = asyncio.ensure_future(self._timed(self._mapper.read_fields(document_text(header_text, tables, footer_text))))

        self._mark("item_band_ocr")
        bands, band_warnings = await (bands_task if bands_task is not None else self._read_bands(pages, locations))
        warnings += band_warnings

        if not parallel:
            self._mark("footer_ocr")
            footer_text = await self._read_footer(pages)

        self._mark("mapping")
        invoice, mapping_warnings = map_invoice(header_text, tables, footer_text, [text for _, _, text in bands])
        say("rules", _summary(invoice))
        self._mark("row_reread")
        reread_bands, reread_warnings = await self._reread_items(
            pages, locations, invoice, "\n".join([header_text, *tables, footer_text])
        )
        warnings += reread_warnings
        if reread_bands:
            # The extra readings join the first ones; merge_items keeps, per line, the reading
            # whose quantity x rate matches its amount, so a re-read row wins only if it adds up.
            invoice, mapping_warnings = map_invoice(
                header_text, tables, footer_text, [text for _, _, text in [*bands, *reread_bands]]
            )
            after = item_faults(invoice, "\n".join([header_text, *tables, footer_text]))
            warnings.append("item rows read again in shorter bands; " + (
                f"still failing: {', '.join(after)}" if after else "the item checks now pass"))
            say("items", "After the re-read: " + (f"still failing: {', '.join(after)}" if after else "every item row adds up"),
                level="warn" if after else "ok")
        reasoning_model = None
        self._mark("qwen2b_crosscheck")
        if self._mapper is not None:
            from app.services.invoice_llm import document_text, merge  # imports this module

            document = document_text(header_text, tables, footer_text)
            say("crosscheck", f"Cross-checking the fields with {self._mapper.name}")
            try:
                if cross_task is None:
                    self._count("qwen2b")
                    cross_task = asyncio.ensure_future(self._timed(self._mapper.read_fields(document)))
                fields, used_in, used_out = await cross_task
                if is_purchase_order_text(document):
                    # The cross-check reads invoice fields (totals, GSTINs); an order has none.
                    fields = {}
            except ModelError as exc:
                logger.warning("reasoning model failed; using rules only", extra={"error": str(exc)})
                mapping_warnings.append(f"reasoning model {self._mapper.name} unavailable ({exc}); rules only")
            else:
                self._usage.input_tokens += used_in
                self._usage.output_tokens += used_out
                invoice, merge_warnings = merge(invoice, fields, document, self._mapper.name)
                mapping_warnings += merge_warnings
                reasoning_model = self._mapper.name
                agreed = [n for n in Invoice.model_fields if n != "items" and "confirmed by" in (getattr(invoice, n).note or "")]
                differ = [n for n in Invoice.model_fields if n != "items" and f"{self._mapper.name} read" in (getattr(invoice, n).note or "")]
                say("crosscheck", f"{self._mapper.name} agrees on {len(agreed)} field(s)"
                    + (f"; reads {', '.join(_label(n) for n in differ)} differently: marked for checking" if differ else ""),
                    level="warn" if differ else "ok")

        self._mark("region_crops")
        zoom_sections, zoom_warnings = await self._read_zoomed(pages, invoice, "\n".join([header_text, *tables, footer_text]))
        warnings += zoom_warnings
        if zoom_sections:
            from app.services.invoice_reread import apply_zoom  # imports this module

            invoice, merge_warnings = apply_zoom(
                invoice, [(kind, label, text) for kind, label, _, text in zoom_sections],
                "\n".join([header_text, *tables, footer_text]),
            )
            mapping_warnings += merge_warnings
            for warning in merge_warnings:
                say("zoom", warning[0].upper() + warning[1:], level="warn")
            if not merge_warnings:
                say("zoom", "The zoomed readings agree with the first reading", level="ok")
        self._mark("qwen8b_fallback")
        invoice, fallback_sections, fallback_warnings = await self._read_fallback(
            pages, invoice, "\n".join([header_text, *tables, footer_text])
        )
        mapping_warnings += fallback_warnings
        raw_text = "\n\n".join(
            [f"--- header OCR (page 1, top {round(header_fraction * 100)}%) ---\n{header_text}"]
            + [f"--- table (page {n}) ---\n{t}" for n, t in enumerate(tables, 1)]
            + [f"--- items band (page {n}, band {k}) ---\n{t}" for n, k, t in bands]
            + [f"--- items band (page {n}, re-read band {k}) ---\n{t}" for n, k, t in reread_bands]
            + [f"--- {kind} ({where}) ---\n{text}" for kind, _, where, text in zoom_sections]
            + fallback_sections
            + [f"--- footer OCR (page {len(pages)}, bottom {int(FOOTER_FRACTION * 100)}%) ---\n{footer_text}"]
        )
        self._mark("details")
        from app.services.invoice_details import extract_details  # imports this module

        details = extract_details(header_text, tables, footer_text, invoice, [text for _, _, text in [*bands, *reread_bands]])
        if is_purchase_order_text("\n".join([header_text, *tables])):
            from app.services.purchase_order import purchase_order_details, purchase_order_fields

            _, labelled = purchase_order_fields(header_text, [parse_otsl(t) for t in tables])
            details = purchase_order_details(details, labelled)
        found = [name.replace("_", " ") for name in ("irn", "ack_no", "eway_bill", "lr_no", "transporter", "bank_ifsc", "amount_in_words")
                 if getattr(details, name).value is not None]
        say("details", "Purchase-entry details found: " + (", ".join(found) if found else "none of IRN, e-way bill, LR, bank"))
        return InvoiceExtraction(
            invoice=invoice,
            details=details,
            model=self._model.name,
            reasoning_model=reasoning_model,
            pages=len(pages),
            raw_text=raw_text,
            warnings=warnings + mapping_warnings,
            usage=self._usage,
            processing_ms=round((time.perf_counter() - started) * 1000),
        )

    async def _read_fallback(
        self, pages: list[bytes], invoice: Invoice, document: str
    ) -> tuple[Invoice, list[str], list[str]]:
        """The hybrid step (see ``invoice_fallback``): when the item or total checks fail or cannot
        run, the larger vision model reads each page; its lines / totals are used where they make
        more checks pass. Returns (invoice, raw-text sections, warnings)."""
        from app.services.invoice_fallback import apply_fallback, fallback_scope, needs_fallback, totals_show_item_fault

        if self._fallback is None or is_purchase_order_text(document):
            return invoice, [], []
        scope = fallback_scope(invoice, document)
        if scope is None:
            return invoice, [], []
        faults = needs_fallback(invoice, document)
        budget = self._zoom.budget_seconds
        if budget is not None and time.perf_counter() - self._started > budget * FALLBACK_BUDGET_SHARE:
            return invoice, [], [f"checks not passing ({', '.join(faults)}), but too little time is left for {self._fallback.name}"]
        name = self._fallback.name
        readings: list[dict[str, Any]] = []
        sections: list[str] = []

        async def read(number: int, what: str) -> dict[str, Any]:
            self._count("qwen8b")
            started = time.perf_counter()
            try:
                reading, used_in, used_out = await self._fallback.read_page(pages[number - 1], what)  # type: ignore[union-attr]
            finally:
                self._spent(started)
            self._usage.input_tokens += used_in
            self._usage.output_tokens += used_out
            sections.append(f"--- fallback (page {number}, {name}) ---\n{json.dumps(reading, ensure_ascii=False)}")
            return reading

        try:
            if scope == "totals":
                # Cheapest first: only the last page's totals block (a few dozen tokens).
                self._say("fallback", f"Totals cannot be reconciled ({', '.join(faults)}): asking {name} for the totals block", level="warn")
                totals = await read(len(pages), "totals")
                if totals_show_item_fault(invoice, totals, document):
                    self._say("fallback", "With those totals the items do not add up: asking for the item table too", level="warn")
                    scope, sections = "items", []
                else:
                    readings = [{"items": [], **totals}]
            if scope == "items":
                self._say("fallback", f"Item checks not passing ({', '.join(faults)}): asking {name} to read the page(s)", level="warn")
                readings = [await read(number, "items") for number in range(1, len(pages) + 1)]
        except ModelError as exc:
            logger.warning("fallback model failed", extra={"error": str(exc)})
            return invoice, sections, [f"{name} could not read the page ({exc}); the first reading is kept"]
        invoice, warnings, adopted = apply_fallback(invoice, readings, document, name)
        for warning in warnings:
            self._say("fallback", warning[0].upper() + warning[1:], level="ok" if adopted else "warn")
        return invoice, sections, warnings

    async def _timed(self, call: Any) -> Any:
        """Await a reasoning-model call, adding its duration to the current stage's model time."""
        started = time.perf_counter()
        try:
            return await call
        finally:
            self._spent(started)

    async def _read_tables(self, pages: list[bytes], warnings: list[str]) -> list[str]:
        """Every whole page read as a table (``Table Recognition:``)."""
        self._enter("table_ocr")
        tables: list[str] = []
        for number, page in enumerate(pages, 1):
            self._say("table", f"Reading page {number} as a table (boxes, items, totals)", page=number, region=(0.0, 0.0, 1.0, 1.0))
            text, cut = strip_repetition(await self._analyze(page, TABLE_TASK, {"max_tokens": TABLE_MAX_TOKENS}))
            self._say("table", f"Page {number}: {len(parse_otsl(text))} table rows read", page=number)
            if cut:
                warnings.append(f"page {number}: table output repeated itself; the repeated tail was removed")
            tables.append(text)
        return tables

    async def _read_footer(self, pages: list[bytes]) -> str:
        """The signature line and bank details: bottom of the last page, ``OCR:``."""
        self._say("footer", "Reading the signature line and bank details (bottom of the last page)",
                  page=len(pages), region=(0.0, 1.0 - FOOTER_FRACTION, 1.0, 1.0))
        self._enter("footer_ocr")
        footer_image = await asyncio.to_thread(crop_band, pages[-1], FOOTER_FRACTION, bottom=True)
        footer_text, _ = await self._ocr(footer_image, FOOTER_MAX_TOKENS)
        if signature := parse_signature(footer_text):
            self._say("footer", f"Signature line names the seller: {signature}", level="ok", page=len(pages))
        return footer_text

    async def _locate(self, pages: list[bytes]) -> tuple[list["TableLocation | None"], list[str]]:
        """Where each page's item table is (via the reasoning model); empty without one."""
        locate = getattr(self._mapper, "locate_table", None)
        if locate is None:
            return [], []
        self._enter("table_detection")
        locations: list[TableLocation | None] = []
        for page in pages:
            try:
                self._count("qwen2b")
                started = time.perf_counter()
                try:
                    location, used_in, used_out = await locate(page)
                finally:
                    self._spent(started)
            except ModelError as exc:
                logger.warning("table locator failed; whole-page reading only", extra={"error": str(exc)})
                return [], [f"item table could not be located ({exc}); read as whole pages only"]
            self._usage.input_tokens += used_in
            self._usage.output_tokens += used_out
            locations.append(location)
        return locations, []

    async def _read_bands(
        self, pages: list[bytes], locations: list["TableLocation | None"]
    ) -> tuple[list[tuple[int, int, str]], list[str]]:
        """Band readings of each located item table: (page, band, table markup) + warnings."""
        self._enter("item_band_ocr")
        bands: list[tuple[int, int, str]] = []
        warnings: list[str] = []
        for number, (page, location) in enumerate(zip(pages, locations, strict=False), 1):
            images = await asyncio.to_thread(band_images, page, location) if location else []
            if not images:
                warnings.append(f"page {number}: item table not located; read as a whole page only")
                continue
            regions = await asyncio.to_thread(band_regions, page, location)
            for index, image in enumerate(images, 1):
                top, bottom = regions[index - 1] if index <= len(regions) else (0.0, 1.0)
                self._say("bands", f"Reading item rows at full resolution: band {index} of {len(images)}",
                          page=number, region=(0.0, top, 1.0, bottom))
                text, cut = strip_repetition(await self._analyze(image, TABLE_TASK, {"max_tokens": BAND_MAX_TOKENS}))
                if cut:
                    warnings.append(f"page {number}, band {index}: table output repeated itself; the tail was removed")
                bands.append((number, index, text))
        return bands, warnings

    async def _reread_items(
        self, pages: list[bytes], locations: list["TableLocation | None"], invoice: Invoice, document: str
    ) -> tuple[list[tuple[int, int, str]], list[str]]:
        """Shorter-band readings of the item tables, when the item checks fail (one pass)."""
        if not self._zoom.reread_items or not any(locations):
            return [], []
        if not item_faults(invoice, document):
            return [], []
        faults = item_faults(invoice, document)
        if not self._within(ITEMS_BUDGET_SHARE):
            return [], ["item re-read skipped: most of the time budget was already used"]
        self._say("items", f"Item check failed ({', '.join(faults)}): reading the item rows again in shorter bands", level="warn")
        bands: list[tuple[int, int, str]] = []
        for number, (page, location) in enumerate(zip(pages, locations, strict=False), 1):
            if location is None:
                continue
            images = await asyncio.to_thread(band_images, page, location, REREAD_BAND_FRACTION)
            regions = await asyncio.to_thread(band_regions, page, location, REREAD_BAND_FRACTION)
            for index, image in enumerate(images, 1):
                top, bottom = regions[index - 1] if index <= len(regions) else (0.0, 1.0)
                self._say("items", f"Re-reading item rows: short band {index} of {len(images)}",
                          page=number, region=(0.0, top, 1.0, bottom))
                text, _ = strip_repetition(await self._analyze(image, TABLE_TASK, {"max_tokens": BAND_MAX_TOKENS}))
                bands.append((number, index, text))
        return bands, []

    def _within(self, share: float) -> bool:
        """Is less than ``share`` of the document's time budget used so far?"""
        budget = self._zoom.budget_seconds
        return budget is None or time.perf_counter() - self._started < budget * share

    async def _located(self, locate: Any, *args: object) -> Any:
        """A locator call (``LlmFieldMapper.locate_*``), counting its tokens."""
        self._count("qwen2b")
        started = time.perf_counter()
        try:
            found, used_in, used_out = await locate(*args)
        finally:
            self._spent(started)
        self._usage.input_tokens += used_in
        self._usage.output_tokens += used_out
        return found

    async def _read_zoomed(
        self, pages: list[bytes], invoice: Invoice, document: str
    ) -> tuple[list[tuple[str, str, str, str]], list[str]]:
        """Zoomed block crops, then re-reads of flagged fields (see ``invoice_reread``).

        Returns sections (kind, label, "page N, label, scale", text) and warnings.
        """
        from app.services.invoice_bands import FIELD_PAD, zoom_crop
        from app.services.invoice_reread import (
            LAST_PAGE_FIELDS, REGION, REGION_FIELDS, REREAD, REREAD_FIELDS, apply_zoom, blocks_needing_zoom, reread_targets,
        )

        zoom = self._zoom
        locate_regions = getattr(self._mapper, "locate_regions", None)
        locate_field = getattr(self._mapper, "locate_field", None)
        sections: list[tuple[str, str, str, str]] = []
        warnings: list[str] = []

        wanted = blocks_needing_zoom(invoice, document) if zoom.targeted_blocks else set(REGION_FIELDS)
        if zoom.region_crops and locate_regions is not None and not wanted:
            self._say("zoom", "Every field in the seller, buyer, invoice and totals blocks checks out: no zoomed block crops", level="ok")
        if zoom.region_crops and locate_regions is not None and wanted:
            located: list[tuple[int, str, Any]] = []
            if not self._within(REGION_BUDGET_SHARE):
                warnings.append("zoomed block crops skipped: most of the time budget was already used")
            else:
                try:
                    first = await self._located(locate_regions, pages[0])
                    located = [(1, label, box) for label, box in first.items()
                               if label in wanted and (label != "totals" or len(pages) == 1)]
                    if len(pages) > 1 and "totals" in wanted and (last := await self._located(locate_regions, pages[-1])).get("totals"):
                        located.append((len(pages), "totals", last["totals"]))
                except ModelError as exc:
                    logger.warning("block locator failed; no zoomed block crops", extra={"error": str(exc)})
                    warnings.append(f"invoice blocks could not be located ({exc}); zoomed block crops skipped")
                else:
                    if not located:
                        warnings.append("the reasoning model found no invoice blocks; zoomed block crops skipped")
            for number, label, box in located:
                self._say("zoom", f"Zooming into the {label.replace('_', ' ')} block", page=number,
                          region=(box.x1, box.y1, box.x2, box.y2))
                image, scale = await asyncio.to_thread(zoom_crop, pages[number - 1], box, max_scale=zoom.max_scale)
                if label == "totals":
                    text, _ = strip_repetition(await self._analyze(image, TABLE_TASK, {"max_tokens": TOTALS_MAX_TOKENS}))
                else:
                    text, _ = await self._ocr(image, REGION_MAX_TOKENS)
                sections.append((REGION, label, f"page {number}, {label}, {scale}x", text))

        self._mark("field_reread")
        if zoom.reread_max_fields > 0 and locate_field is not None:
            trial, _ = apply_zoom(invoice, [(kind, label, text) for kind, label, _, text in sections], document)
            for name in reread_targets(trial, document, zoom.reread_max_fields):
                if not self._within(REREAD_BUDGET_SHARE):
                    warnings.append("field re-reads stopped: most of the time budget was already used")
                    break
                number = len(pages) if name in LAST_PAGE_FIELDS else 1
                try:
                    box = await self._located(locate_field, pages[number - 1], REREAD_FIELDS[name])
                except ModelError as exc:
                    warnings.append(f"fields could not be located for re-reading ({exc}); re-reads stopped")
                    break
                if box is None:
                    self._say("zoom", f"Could not find the {_label(name)} on the page to re-read it", level="warn", page=number)
                    continue
                self._say("zoom", f"Re-reading the {_label(name)} enlarged (its first reading needs checking)", page=number,
                          region=(box.x1, box.y1, box.x2, box.y2))
                image, scale = await asyncio.to_thread(zoom_crop, pages[number - 1], box, max_scale=zoom.max_scale, pad=FIELD_PAD)
                text, _ = await self._ocr(image, REREAD_MAX_TOKENS)
                sections.append((REREAD, name, f"page {number}, {name}, {scale}x", text))
        return sections, warnings

    async def _ocr(self, image: bytes, max_tokens: int) -> tuple[str, bool]:
        """Free-text OCR with anti-repetition sampling; one retry if it still loops.

        Returns the text (loop removed) and whether both attempts looped. Loops are
        intermittent, so a second attempt usually reads the whole block.
        """
        options: dict[str, object] = {"max_tokens": max_tokens, "anti_repetition": True}
        text, looped = strip_repetition(await self._analyze(image, OCR_TASK, options))
        if looped:
            text, looped = strip_repetition(await self._analyze(image, OCR_TASK, options))
        return text, looped
