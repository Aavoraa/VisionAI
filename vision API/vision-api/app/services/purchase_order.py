"""Purchase orders: a printed form ("PURCHASE ORDER FORM") filled in by hand.

The buyer's own name, address and GSTIN are printed at the top; the supplier is written on the
"Company Name" line, with Brand, Item, Place, Transport, Agent, Purchaser and Discount beside it.
Each line is a colour with its sizes, the pieces per size under them ("x" = none) and the
line's total ("55.pc"); there are no rates, taxes or totals. Handwriting is read less reliably
than print, so every line is checked: its size counts must add up to its written total.

Readers here are pure (OCR text in, values out) so saved results can be re-mapped.
"""

import re
from decimal import Decimal
from typing import Any

from app.schemas.invoice import Extracted, InvoiceDetails, LineItem

PO_REQUIRED = ("invoice_number", "invoice_date", "vendor_name", "buyer_name")
SIZES_DO_NOT_ADD_UP = "the size counts add up to {got}, the written total is {total}: check the handwriting"
COUNTS_UNREAD = "the pieces per size could not be read; check the handwriting"
_PO = re.compile(r"purchase\s+order", re.I)
_SIZE = re.compile(r"^(?:\d?X{1,3}[SL]|[SML]|XS|\d{2,3}|1)$", re.I)  # L XL 2XL 3XL S M XS 28 30 ("1" = a misread L)
_TOTAL = re.compile(r"(\d{1,5})\s*[.\-]?\s*p\s*c[s]?\b", re.I)  # "55.pc", "5-PC", "20 Pcs"
_COUNT = re.compile(r"^(?:\d{1,4}|[xX]|[sS])$")
_LABELS = ("company name", "brand name", "item", "place", "transport", "agent", "purchaser", "discount")
_LATEX = re.compile(r"\\\(|\\\)|\\underline\{|\\text\{|[{}]")


def is_purchase_order(text: str) -> bool:
    return bool(_PO.search(text))


def _clean(text: str) -> str:
    return " ".join(_LATEX.sub(" ", text).replace("\\", " ").split()).strip(" .:-")


def _cells(row: list[str | None]) -> list[str]:
    return [c.strip() for c in row if c and c.strip()]


def _parse_item_row(tokens: list[str]) -> tuple[int, str, list[str]] | None:
    """"1) Sandal L XL 2XL 3XL" -> (1, "Sandal", ["L", "XL", "2XL", "3XL"])."""
    if not tokens or not (serial := re.fullmatch(r"(\d{1,2})[).]?", tokens[0])):
        return None
    sizes: list[str] = []
    rest = tokens[1:]
    while rest and _SIZE.match(rest[-1]):
        sizes.insert(0, rest.pop().upper())
    name = " ".join(rest)
    if len(sizes) < 2 or not re.search(r"[A-Za-z]{2}", name):
        return None
    # "1" before "XL" is a handwritten L; a lone numeric size elsewhere stays (waist sizes).
    sizes = ["L" if s == "1" and i + 1 < len(sizes) and sizes[i + 1] == "XL" else s for i, s in enumerate(sizes)]
    return int(serial.group(1)), name, sizes


def _parse_count_row(text: str) -> tuple[list[int], int | None] | None:
    """"10 10 20 15 55.pc" -> ([10, 10, 20, 15], 55); "x" is none, a handwritten "S" a 5."""
    total = None
    if match := _TOTAL.search(text):
        total = int(match.group(1))
        text = text[: match.start()]
    tokens = text.split()
    if not tokens or not all(_COUNT.match(t) for t in tokens):
        return None
    counts = [0 if t in "xX" else 5 if t in "sS" else int(t) for t in tokens]
    return counts, total


def _lines(grid: list[list[str | None]]) -> dict[int, dict[str, Any]]:
    """Lines of one reading of the order table, by serial number."""
    lines: dict[int, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for row in grid:
        cells = _cells(row)
        if not cells:
            continue
        tokens = " ".join(cells).split()
        if (item := _parse_item_row(tokens)) is not None:
            number, name, sizes = item
            current = {"number": number, "name": name, "sizes": sizes, "counts": None, "total": None, "source": " ".join(cells)}
            lines.setdefault(number, current)
            continue
        if current is not None and current["counts"] is None and (counts := _parse_count_row(" ".join(cells))) is not None:
            current["counts"], current["total"] = counts
            current["source"] += " | " + " ".join(cells)
    return lines


def _verified(line: dict[str, Any]) -> bool:
    counts, total = line["counts"], line["total"]
    return counts is not None and total is not None and len(counts) == len(line["sizes"]) and sum(counts) == total


def purchase_order_items(grids: list[list[list[str | None]]], item_name: str | None) -> tuple[list[LineItem], list[str]]:
    """One line per colour from every reading of the table (whole page first, then the bands):
    per line the first reading whose size counts add up to its written total, else the first."""
    chosen: dict[int, dict[str, Any]] = {}
    for grid in grids:
        for number, line in _lines(grid).items():
            if number not in chosen or (not _verified(chosen[number]) and _verified(line)):
                chosen[number] = line
    items: list[LineItem] = []
    warnings: list[str] = []
    for number in sorted(chosen):
        line = chosen[number]
        name = line["name"].title() if line["name"].isupper() or line["name"].islower() else line["name"]
        description = f"{item_name} - {name}" if item_name else name
        counts, total = line["counts"], line["total"]
        quantity = total if total is not None else (sum(counts) if counts else None)
        source = line["source"]
        if _verified(line):
            size = Extracted.found(", ".join(f"{s}-{c}" for s, c in zip(line["sizes"], counts, strict=True) if c), source)
            qty = Extracted.found(Decimal(quantity), source)
        else:
            if counts is None:
                note = COUNTS_UNREAD
            else:
                note = SIZES_DO_NOT_ADD_UP.format(got=sum(counts), total=total if total is not None else "not read")
            pairs = zip(line["sizes"], counts, strict=False) if counts else ()
            size = Extracted.check(", ".join(f"{s}-{c}" for s, c in pairs if c) or ", ".join(line["sizes"]), note, source)
            qty = Extracted.check(Decimal(quantity), note, source) if quantity is not None else Extracted.check(None, note, source)
            warnings.append(f"line {number} ({name}): {note}")
        items.append(LineItem(
            line_number=Extracted.found(number, source),
            description=Extracted.found(description, source),
            size=size,
            quantity=qty,
            unit=Extracted.found("pcs", source),
        ))
    return items, warnings


def _labelled(header_text: str, grids: list[list[list[str | None]]]) -> dict[str, str]:
    """The form's handwritten values by label (table cells first: "Company Name | H.R.M. Fabric")."""
    found: dict[str, str] = {}
    for grid in grids:
        for row in grid:
            cells = _cells(row)
            for i, cell in enumerate(cells[:-1]):
                label = _clean(cell).lower()
                if label in _LABELS and label not in found:
                    value = _clean(cells[i + 1])
                    if value and value.lower() not in _LABELS:
                        found[label] = value
    for line in header_text.splitlines():
        for label in _LABELS:
            if label in found:
                continue
            if match := re.match(rf"\s*{label}\s*[:.]*\s*(.+)$", _clean(line), re.I):
                value = match.group(1)
                # "Brand Name: Agent Direct": the next label's caption, not this one's value.
                if value and not any(value.lower().startswith(other) for other in _LABELS):
                    found[label] = value
    return found


def purchase_order_fields(header_text: str, grids: list[list[list[str | None]]]) -> tuple[dict[str, Extracted[Any]], dict[str, str]]:
    """(invoice field updates, labelled values): the supplier is the "Company Name", the buyer the
    printed company (whose GSTIN is the only one printed), the order number the "No."."""
    labelled = _labelled(header_text, grids)
    text = "\n".join([header_text, *(" ".join(_cells(r)) for g in grids for r in g)])
    updates: dict[str, Extracted[Any]] = {}
    if supplier := labelled.get("company name"):
        updates["vendor_name"] = Extracted.check(supplier, "handwritten on the Company Name line; verify the spelling", supplier)
    else:
        updates["vendor_name"] = Extracted()
    updates["vendor_gstin"] = Extracted()
    updates["vendor_address"] = Extracted()
    buyer = next((_clean(line) for line in header_text.splitlines()
                  if re.search(r"\b(private limited|pvt\.? ?ltd|limited|ltd|llp)\b", line, re.I)
                  and not re.search(r"company name|for\s", line, re.I)), None)
    if buyer:
        updates["buyer_name"] = Extracted.found(buyer, buyer)
    if gstin := re.search(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b", text):
        updates["buyer_gstin"] = Extracted.found(gstin.group(0), gstin.group(0))
    if number := re.search(r"\bNo\.?\s*[:\-]?\s*(\d{2,8})\b", text) or re.search(r"^\s*(\d{2,8})\s*$", header_text, re.M):
        updates["invoice_number"] = Extracted.found(number.group(1), number.group(0).strip())
    for name in ("total_amount", "subtotal", "cgst", "sgst", "igst", "discount", "round_off", "place_of_supply", "delivery_note", "payment_terms"):
        updates[name] = Extracted()  # an order has no totals or taxes; never take a line total for them
    return updates, labelled


def purchase_order_details(details: InvoiceDetails, labelled: dict[str, str]) -> InvoiceDetails:
    """Agent, transport and place of the order into the purchase-entry details."""
    updates: dict[str, Extracted[Any]] = {}
    for label, field in (("agent", "agent"), ("transport", "transporter"), ("place", "destination")):
        value = labelled.get(label)
        # Only the form's own line: an empty "Transport ....." line is left empty, never filled
        # from the next caption by the invoice readers.
        updates[field] = Extracted.found(value, f"{label.title()}: {value}") if value else Extracted()
    return details.model_copy(update=updates)


def size_counts(size: str | None) -> list[int]:
    """Counts in a size value "L-10, XL-10" (empty when it has none)."""
    return [int(n) for n in re.findall(r"-(\d+)", size or "")]
