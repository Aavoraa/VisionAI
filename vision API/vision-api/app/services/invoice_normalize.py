"""Normalisation of extracted invoice values to one consistent form.

* States: GST state codes (the first two digits of every GSTIN) -> "Tamil Nadu (33)".
* Units: "PCS." / "Pieces" -> "pcs", "Nos" -> "nos", "Kgs" -> "kg", ...
* Invoice numbers: "CR/134/26/27" -> "CR/134/26-27" when the financial year is written with a
  hyphen elsewhere on the same invoice (a common OCR slip on the year separator).
"""

import re

# GST state/UT codes (CBIC). 25 (Daman and Diu) was merged into 26 in 2020 but still appears
# on older documents; 28 is Andhra Pradesh before the 2014 split (now 37).
GST_STATE_CODES: dict[str, str] = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "25": "Daman and Diu", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra",
    "28": "Andhra Pradesh", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
    "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman and Nicobar Islands", "36": "Telangana",
    "37": "Andhra Pradesh", "38": "Ladakh", "97": "Other Territory", "99": "Centre Jurisdiction",
}
_STATE_ALIASES = {"orissa": "21", "pondicherry": "34", "new delhi": "07", "nct of delhi": "07", "tamilnadu": "33"}


def _state_key(name: str) -> str:
    return " ".join(re.sub(r"[^a-z ]", " ", name.lower().replace("&", " and ")).split())


_CODE_BY_NAME = {_state_key(name): code for code, name in GST_STATE_CODES.items() if code not in ("28", "25")}
_CODE_BY_NAME.update(_STATE_ALIASES)


def format_state(text: str) -> str:
    """ "Tamil Nadu, Code : 33" / "TAMIL NADU" / "33" -> "Tamil Nadu (33)"; unknown text is returned trimmed."""
    code_match = re.search(r"(?:code\s*[:\-]?\s*|\(|^\s*)(\d{1,2})\b", text, re.I)
    code = code_match.group(1).zfill(2) if code_match else None
    name_part = re.sub(r"(?:,?\s*(?:state\s*)?code\s*[:\-]?\s*\d{1,2})|\(\d{1,2}\)|^\s*\d{1,2}\s*[-,]?", "", text, flags=re.I)
    name_code = _CODE_BY_NAME.get(_state_key(name_part))
    if code in GST_STATE_CODES and (name_code in (None, code) or not name_part.strip()):
        return f"{GST_STATE_CODES[code]} ({code})"
    if name_code and code is None:
        return f"{GST_STATE_CODES[name_code]} ({name_code})"
    return " ".join(text.split())


_UNITS = {
    "pcs": "pcs", "pc": "pcs", "piece": "pcs", "pieces": "pcs", "pce": "pcs",
    "nos": "nos", "no": "nos", "number": "nos", "numbers": "nos",
    "kg": "kg", "kgs": "kg", "kilogram": "kg", "kilograms": "kg",
    "g": "g", "gm": "g", "gms": "g", "gram": "g", "grams": "g",
    "mtr": "mtr", "mtrs": "mtr", "meter": "mtr", "meters": "mtr", "metre": "mtr", "metres": "mtr", "m": "mtr",
    "ltr": "ltr", "ltrs": "ltr", "litre": "ltr", "litres": "ltr", "liter": "ltr", "l": "ltr",
    "box": "box", "boxes": "box", "set": "set", "sets": "set", "pair": "pair", "pairs": "pair",
    "dozen": "doz", "doz": "doz", "roll": "roll", "rolls": "roll", "bag": "bag", "bags": "bag",
}


def normalize_unit(unit: str) -> str:
    """Canonical unit ("PCS." -> "pcs"); unknown units are lower-cased as printed."""
    key = unit.strip().rstrip(".").lower()
    return _UNITS.get(key, key)


def normalize_invoice_number(number: str, document_text: str) -> tuple[str, bool]:
    """Fix "26/27" -> "26-27" in a trailing financial year when the invoice writes it with a hyphen.

    Returns (number, changed). Only consecutive years are touched ("26/27", not "26/29"), and
    only when "26-27" appears elsewhere in the document, so genuine slashes are left alone.
    """
    match = re.fullmatch(r"(.*\D)(\d{2})/(\d{2})", number)
    if not match or int(match.group(3)) != (int(match.group(2)) + 1) % 100:
        return number, False
    hyphenated = f"{match.group(2)}-{match.group(3)}"
    if hyphenated not in document_text:
        return number, False
    return f"{match.group(1)}{hyphenated}", True
