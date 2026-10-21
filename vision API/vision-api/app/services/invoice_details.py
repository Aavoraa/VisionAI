"""Additional invoice details for purchase entry: e-invoice, transport, order, bank, contacts.

These are read from the same OCR text as the core fields (no extra model calls) and kept in
``InvoiceExtraction.details``, apart from the core ``Invoice``: they do not take part in
validation, confidence or routing. The same rules apply: a value that is not found stays
``None``; a value derived rather than read (state from the GSTIN, PAN from the GSTIN, tax rate
from amounts) says so in its note.

A labelled value is taken from the caption's own line ("Ack No. : 1226...") or, when that line
has nothing after the caption, from the next line ("Dispatched through" / "Golden Transports",
as Tally prints its boxes). The value ends where the next printed caption starts.
"""

import re
from collections.abc import Callable
from decimal import Decimal

from app.schemas.invoice import Extracted, Invoice, InvoiceDetails
from app.services.invoice_extraction import _LABELS, _cell_text, parse_date
from app.services.invoice_normalize import GST_STATE_CODES
from app.services.invoice_validation import GST_RATES, words_to_amount

# Captions of the detail fields (regex fragments, matched at the start of a line or after " ").
CAPTIONS: dict[str, tuple[str, ...]] = {
    "ack_no": (r"ack\.?\s*no",),
    "ack_date": (r"ack\.?\s*(?:date|dt)",),
    "eway_bill": (r"e[-_ ]?way\s*bill(?:\s*no)?", r"eway\s*no", r"way\s*bill\s*no"),
    "lr_no": (r"l\.?\s*r\.?\s*(?:no|number)",),
    "lr_date": (r"l\.?\s*r\.?\s*date",),
    "transporter": (r"dispatched\s+through", r"transport(?:er)?\s*name", r"transporter", r"transport"),
    "destination": (r"destination", r"booking"),
    "agent": (r"agent(?!\s*mob)", r"agency\s*name"),
    "broker": (r"broker\s*name", r"name\s+of\s+broker", r"broker"),
    "order_no": (r"buyer'?s\s*order\s*no", r"order\s*no", r"p\.?\s*o\.?\s*no"),
    "order_date": (r"order\s*date", r"p\.?\s*o\.?\s*date"),
    "reference_no": (r"reference\s*no\.?(?:\s*&\s*date)?",),
    "challan_no": (r"challan\s*no", r"d\.?\s*c\.?\s*no", r"dispatch\s*doc\s*no"),
    "due_date": (r"due\s*date",),
    "bank_name": (r"bank\s*name", r"our\s*bank"),
    "bank_account": (r"a/?c\.?\s*no", r"account\s*no", r"ac\s*no"),
    "bank_branch": (r"branch\s*name", r"branch(?!\s*&)"),
    "phone": (r"tel", r"phone", r"contact", r"mob(?:ile)?(?:\s*no)?"),
}

# Where a value ends: a core label anywhere ("Terms of Delivery"), or a detail caption that is
# followed by a separator ("LR Number :"); "GOLDEN TRANSPORT SERVICE" is a name, not a caption.
_EXTRA_CAPTIONS = (r"terms\s+of\s+delivery", r"parcel", r"weight", r"freight", r"state", r"gstin", r"e-?mail",
                   r"ifs\w*\s*code", r"swift", r"a/c\s*holder", r"nos\s+of\s+parcels", r"remark", r"marking",
                   r"place\s+of\s+supply", r"invoice\s*(?:no|date)", r"dated", r"booking", r"description\s+of\s+goods",
                   r"a/c\.?\s*no")
_ALL_CAPTIONS = re.compile(
    r"(?:(?<=\s)|^)(?<!for\s)(?:(?:" + "|".join(re.escape(label) for label in _LABELS) + "|" + "|".join(_EXTRA_CAPTIONS)
    + r")\b|(?:" + "|".join(c for group in CAPTIONS.values() for c in group) + r")\b\.?\s*[:\-]|(?:"
    + "|".join(c for group in CAPTIONS.values() for c in group) + r")\b\.?\s*$)",
    re.I,
)
_JUNK = re.compile(r"^(?:e-?invoice|tax invoice|details|original|duplicate|triplicate|copy|no|nil|na|n/a)\b", re.I)

_DIGITS = re.compile(r"\d")
_PACKAGES = re.compile(r"^\s*\d+\s*(?:parcels?|box(?:es)?|bundles?|bales?|pcs|nos|bags?)\b", re.I)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9/\-.]*")
_DATE_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}[-/.](?:\d{1,2}|[A-Za-z]{3,9})[-/.]\d{2,4}")
_IFSC = re.compile(r"\b([A-Z]{4})([0O])([A-Z0-9]{6})\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w/])\+?\d[\d \-]{7,14}\d(?![\w/])")
_WORDS = re.compile(r"((?:INR|Rupees|Rs\.?)?\s*[A-Za-z][A-Za-z ,\-]+?\bonly)\b", re.I)


def _lines(header: str, tables: list[str], footer: str, bands: list[str] | None = None) -> list[str]:
    parts = [header, *(_cell_text(t) for t in tables[:1]), *(_cell_text(b) for b in bands or []), footer]
    return [" ".join(line.split()) for part in parts for line in part.replace("\\n", "\n").splitlines() if line.strip()]


def _after_caption(lines: list[str], captions: tuple[str, ...], accept: Callable[[str], str | None]) -> tuple[str, str] | None:
    """(value, source) for the first caption whose value ``accept`` takes (it returns the cleaned value)."""
    # A separator is ":" / "-", or a full stop directly before a number ("A/c. No. 1119...").
    pattern = re.compile(r"(?:(?<=[\s.,;|])|^)(?<!for\s)(?:" + "|".join(captions) + r")\b(?P<sep>\.?\s*[:\-]+|\.(?=\s*\d))?\.?\s*", re.I)
    for index, line in enumerate(lines):
        for match in pattern.finditer(line):
            if match.start() > 0 and not match.group("sep"):
                continue  # mid-line words such as "GOLDEN TRANSPORT" are not captions without a ":"
            rest = line[match.end() :]
            following = _ALL_CAPTIONS.search(rest)
            value = rest[: following.start() if following else len(rest)].strip(" :-|,")
            if value:
                if (clean := accept(value)) is not None:
                    return clean, line
                continue
            if following is None and index + 1 < len(lines) and not _ALL_CAPTIONS.match(lines[index + 1]):
                below = lines[index + 1]
                if _PACKAGES.match(below):
                    continue  # "1 parcel": a package count in the box below, not this caption's value
                below = below[: m.start()] if (m := _ALL_CAPTIONS.search(below)) else below
                if (clean := accept(below.strip(" :-|,"))) is not None:
                    return clean, f"{line} / {lines[index + 1]}"
    return None


def _text(value: str) -> str | None:
    """Words, not a lone number or a date (names of transporters, agents, banks, places)."""
    value = re.sub(r"\s+", " ", value).strip(" .:-")
    value = re.sub(r"\s+dt\.?\s*\S+$", "", value)  # "Matoshree Agency dt. 16-Jul-26"
    if len(value) < 3 or not re.search(r"[A-Za-z]{2}", value) or _DATE_TEXT.fullmatch(value) or _JUNK.match(value):
        return None
    return value[:80]


def _identifier(value: str) -> str | None:
    """The first token with a digit (numbers such as LR, order or challan numbers)."""
    if _PACKAGES.match(value):
        return None  # a package count ("1 parcel"), printed in the box below the caption
    for token in _TOKEN.findall(value)[:3]:
        token = token.rstrip(".")
        if _DIGITS.search(token) and not _DATE_TEXT.fullmatch(token):
            return token
    return None


def _digits(minimum: int, maximum: int) -> Callable[[str], str | None]:
    def accept(value: str) -> str | None:
        match = re.search(rf"(?<!\d)\d{{{minimum},{maximum}}}(?!\d)", value.replace(" ", ""))
        return match.group() if match else None

    return accept


def _date(value: str) -> str | None:
    match = _DATE_TEXT.search(value)
    return match.group() if match and parse_date(match.group()) else None


def _found(key: str, found: tuple[str, str] | None, convert: Callable[[str], object] = str) -> tuple[str, Extracted]:  # type: ignore[type-arg]
    if found is None:
        return key, Extracted()
    value = convert(found[0])
    return key, Extracted.found(value, found[1]) if value is not None else Extracted()


def _irn(lines: list[str]) -> Extracted[str]:
    """64 hex characters after "IRN", often wrapped onto two lines at a hyphen."""
    text = "\n".join(lines)
    for match in re.finditer(r"\bIRN\b\s*(?:NO)?\s*[:.]?\s*", text, re.I):
        tail = re.sub(r"[\s\-/]", "", text[match.end() : match.end() + 140])
        if hexes := re.match(r"[0-9a-fA-F]{64}", tail):
            return Extracted.found(hexes.group().lower(), text[match.start() : match.end() + 70].replace("\n", " "))
        if hexes := re.match(r"[0-9a-fA-F]{40,63}", tail):
            return Extracted.check(hexes.group().lower(), "IRN shorter than 64 characters: part was not read", tail[:70])
    return Extracted()


def _eway_below_captions(lines: list[str]) -> Extracted[str]:
    """A 12-digit number on the line under a caption row that names the e-way bill."""
    for index, line in enumerate(lines[:-1]):
        if re.search(r"e[-_ ]?way\s*bill", line, re.I):
            for candidate in (line, lines[index + 1]):
                numbers = re.findall(r"(?<!\d)\d{12}(?!\d)", candidate)
                if len(numbers) == 1:
                    return Extracted.check(numbers[0], "read from the row below the 'e-Way Bill No.' caption; verify", f"{line} / {lines[index + 1]}")
    return Extracted()


def _ifsc(lines: list[str]) -> Extracted[str]:
    for index, line in enumerate(lines):
        if not re.search(r"ifs|bank|branch|a/c", line, re.I):
            continue
        if re.search(r"ifs\w*\s*code\W*$", line, re.I) and index + 1 < len(lines):
            line = f"{line} {lines[index + 1]}"
        for match in _IFSC.finditer(line.upper()):
            code = f"{match.group(1)}0{match.group(3)}"
            note = None if match.group(2) == "0" else f"read as {match.group()}; the 5th character is always 0"
            return Extracted(value=code, status="ok", source_text=line, note=note)
    return Extracted()


def _first(pattern: re.Pattern[str], lines: list[str]) -> Extracted[str]:
    for line in lines:
        if match := pattern.search(line):
            return Extracted.found(match.group().strip(" ,"), line)
    return Extracted()


def _state(gstin: Extracted[str]) -> tuple[Extracted[str], Extracted[str]]:
    code = gstin.value[:2] if gstin.value else None
    if code not in GST_STATE_CODES:
        return Extracted(), Extracted()
    note = "from the GSTIN (first two digits)"
    return Extracted(value=GST_STATE_CODES[code], status="ok", source_text=gstin.value, note=note), \
        Extracted(value=code, status="ok", source_text=gstin.value, note=note)


_RATES = sorted({*GST_RATES, *(r / 2 for r in GST_RATES)})


def _rate(tax: Extracted[Decimal], subtotal: Extracted[Decimal], discount: Extracted[Decimal]) -> Extracted[Decimal]:
    if tax.value is None or not subtotal.value:
        return Extracted()
    bases = [subtotal.value] + ([subtotal.value - discount.value] if discount.value else [])
    for base in bases:
        if base <= 0:
            continue  # a discount read as large as the subtotal (a misread) leaves no base to divide by
        rate = tax.value * 100 / base
        nearest = min(_RATES, key=lambda r: abs(r - rate))
        if abs(nearest - rate) <= Decimal("0.05"):
            return Extracted(value=nearest, status="ok", source_text=f"{tax.value} / {base}", note="tax amount / taxable total")
    rate = tax.value * 100 / subtotal.value
    return Extracted.check(round(rate, 2), "not a single GST rate (mixed rates or a misread amount)", f"{tax.value} / {subtotal.value}")


def _number_words(words: str) -> str | None:
    """"Amount Chargeable INR Forty Two Only" -> "INR Forty Two Only": from the first number word
    (with a currency word just before it), as long as the rest reads as an amount."""
    tokens = words.split()
    for start, token in enumerate(tokens):
        if words_to_amount(token + " only") is not None and words_to_amount(" ".join(tokens[start:])) is not None:
            if start and re.fullmatch(r"INR|Rupees|Rs\.?", tokens[start - 1], re.I):
                start -= 1
            return " ".join(tokens[start:])
    return None


def _amount_in_words(text: str, total: Decimal | None) -> Extracted[str]:
    candidates = []
    for match in _WORDS.finditer(text):
        words = " ".join(match.group(1).split()).strip(" ,-")
        if re.search(r"\b(?:thousand|hundred|lakh|crore)\b", words, re.I) and not re.search(r"\btax\b", text[max(0, match.start() - 40) : match.start()], re.I):
            if number := _number_words(words):
                candidates.append(number)
    for words in candidates:
        if total is not None and words_to_amount(words) == total:
            return Extracted.found(words, words)
    if candidates:
        return Extracted.check(candidates[0], "does not match the total amount", candidates[0])
    return Extracted()


def extract_details(
    header_text: str, tables: list[str], footer_text: str, invoice: Invoice, bands: list[str] | None = None
) -> InvoiceDetails:
    lines = _lines(header_text, tables, footer_text, bands)
    header_lines = _lines(header_text, [], "")
    fields: dict[str, Extracted] = dict([  # type: ignore[type-arg]
        _found("ack_no", _after_caption(lines, CAPTIONS["ack_no"], _digits(12, 20))),
        _found("ack_date", _after_caption(lines, CAPTIONS["ack_date"], _date), parse_date),
        _found("eway_bill", _after_caption(lines, CAPTIONS["eway_bill"], _digits(12, 12))),
        _found("lr_no", _after_caption(lines, CAPTIONS["lr_no"], _identifier)),
        _found("lr_date", _after_caption(lines, CAPTIONS["lr_date"], _date), parse_date),
        _found("transporter", _after_caption(lines, CAPTIONS["transporter"], _text)),
        _found("destination", _after_caption(lines, CAPTIONS["destination"], _text)),
        _found("agent", _after_caption(lines, CAPTIONS["agent"], _text)),
        _found("broker", _after_caption(lines, CAPTIONS["broker"], _text)),
        _found("order_no", _after_caption(lines, CAPTIONS["order_no"], _identifier)),
        _found("order_date", _after_caption(lines, CAPTIONS["order_date"], _date), parse_date),
        _found("challan_no", _after_caption(lines, CAPTIONS["challan_no"], _identifier)),
        _found("due_date", _after_caption(lines, CAPTIONS["due_date"], _date), parse_date),
        _found("bank_name", _after_caption(lines, CAPTIONS["bank_name"], _text)),
        _found("bank_account", _after_caption(lines, CAPTIONS["bank_account"], _digits(9, 18))),
        _found("vendor_phone", _after_caption(header_lines, CAPTIONS["phone"], lambda v: (m.group().strip() if (m := _PHONE.search(v)) else None))),
    ])
    if reference := _after_caption(lines, CAPTIONS["reference_no"], lambda v: v if _DIGITS.search(v) else None):
        number = re.split(r"\s+dt\.?\s*", reference[0], maxsplit=1, flags=re.I)[0]  # "833 dt. 15-Jul-26"
        date_text = _date(reference[0])
        fields["reference_no"] = Extracted.found(_identifier(number) or number, reference[1])
        if date_text and (parsed := parse_date(date_text)):
            fields["reference_date"] = Extracted.found(parsed, reference[1])
    if branch := _after_caption(lines, CAPTIONS["bank_branch"], _text):
        fields["bank_branch"] = Extracted.found(branch[0].split("&")[0].strip(" ,"), branch[1])
    elif ifs_line := next((line for line in lines if re.search(r"branch\s*&\s*ifs", line, re.I)), None):
        after = re.split(r"branch\s*&\s*ifs\w*\s*code\s*[:.]?", ifs_line, flags=re.I)[-1]
        if text := _text(after.split("&")[0]):
            fields["bank_branch"] = Extracted.found(text, ifs_line)
    if fields["eway_bill"].value is None:
        fields["eway_bill"] = _eway_below_captions(lines)
    fields["irn"] = _irn(lines)
    fields["bank_ifsc"] = _ifsc(lines)
    fields["vendor_email"] = _first(_EMAIL, header_lines)

    if gstin := invoice.vendor_gstin.value:
        fields["vendor_pan"] = Extracted(value=gstin[2:12], status=invoice.vendor_gstin.status, source_text=gstin, note="from the GSTIN (characters 3-12)")
    fields["vendor_state"], fields["vendor_state_code"] = _state(invoice.vendor_gstin)
    fields["buyer_state"], fields["buyer_state_code"] = _state(invoice.buyer_gstin)
    if fields["buyer_state"].value is None and invoice.place_of_supply.value:
        match = re.match(r"(.+?)\s*\((\d{2})\)$", invoice.place_of_supply.value)
        if match:
            fields["buyer_state"] = Extracted.check(match.group(1), "from the place of supply", invoice.place_of_supply.value)
            fields["buyer_state_code"] = Extracted.check(match.group(2), "from the place of supply", invoice.place_of_supply.value)

    for tax in ("cgst", "sgst", "igst"):
        fields[f"{tax}_rate"] = _rate(getattr(invoice, tax), invoice.subtotal, invoice.discount)
    taxes = [getattr(invoice, t).value for t in ("cgst", "sgst", "igst") if getattr(invoice, t).value is not None]
    if taxes:
        fields["tax_total"] = Extracted(value=sum(taxes, Decimal(0)), status="ok", source_text=" + ".join(map(str, taxes)), note="CGST + SGST + IGST")
    if invoice.subtotal.value is not None:
        discount = invoice.discount.value or Decimal(0)
        fields["gross_amount"] = Extracted(
            value=invoice.subtotal.value + discount, status="ok", source_text=str(invoice.subtotal.value),
            note="taxable total + discount" if discount else "taxable total (no invoice discount)",
        )
    everything = "\n".join([header_text, *tables, *(bands or []), footer_text]).replace("\\n", " ")
    plain = " ".join(re.sub(r"<(?:fcel|ecel|lcel|ucel|xcel|nl)>", " ", everything).split())
    fields["amount_in_words"] = _amount_in_words(plain, invoice.total_amount.value)
    return InvoiceDetails(**fields)
