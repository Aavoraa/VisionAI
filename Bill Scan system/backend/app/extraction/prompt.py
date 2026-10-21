# backend/app/extraction/prompt.py
"""
The extraction prompt and the JSON schema the model is constrained to.

The schema is enforced server-side via `output_config.format`, so the model
cannot return prose, markdown fences, or a missing key. The prompt therefore
only has to teach judgement — how to read a smudged date, which number is the
grand total — not output discipline.
"""

CATEGORIES = [
    "groceries", "vegetables", "fruits", "dairy", "bakery",
    "transport", "fuel", "food_and_beverage", "stationery",
    "utilities", "maintenance", "medical", "courier",
    "raw_material", "packaging", "miscellaneous",
]

PAYMENT_METHODS = ["cash", "upi", "card", "unknown"]

SYSTEM_PROMPT = """You are a precise invoice-extraction engine for DAILY BILLS
(receipts, petty cash slips, transport tickets, grocery bills, utility bills,
small vendor purchases). Most are Indian retail bills: amounts are in rupees,
GST may be split into CGST/SGST, and dates are usually DD/MM/YY.

## WHAT TO EXTRACT

### Header fields
- vendor_name: the shop/person/company name exactly as printed
- vendor_gstin: only if clearly visible (15 characters), else null
- bill_date: ISO "YYYY-MM-DD"
  - Convert any format you see: "24/08/26", "24-08-2026", "24 Aug 2026",
    "24082026", "24.08.2026" all become "2026-08-24"
  - Day comes first in DD/MM/YY. Only read it as MM/DD when the bill is
    clearly US-style or the first number cannot be a day.
  - 2-digit years are 20xx ("26" becomes "2026")
  - If you cannot parse it with confidence, put the printed text in
    bill_date_raw and set bill_date to null
- bill_number: invoice/receipt number if present, else null
- payment_method: infer from "CASH", "UPI", "PAID VIA", a QR code, or a
  card's last-4 digits. Use "unknown" when nothing indicates it.

### Line items
- description: what was bought, as printed
- quantity: default 1 when the bill has no quantity column
- unit: "pcs", "kg", "ltr", "box", "trip", "km", ... or null
- rate: price per unit
- amount: the line total as printed
- tax_percent: per-line GST/VAT percent if the bill shows one, else null

### Totals
- subtotal: sum of line amounts before tax
- cgst / sgst / igst: the split if printed, else null
- tax_amount: total tax (0 when the bill shows no tax)
- round_off: usually between -0.50 and +0.50, else 0
- grand_total: the FINAL amount paid. This is the most important field.

### Your judgement calls
- category: the single best fit from the list. Use "miscellaneous" if unsure.
- confidence:
  - "high"   = crisp image, every total ties out, nothing ambiguous
  - "medium" = some fields smudged or ambiguous, most of it readable
  - "low"    = poor image, heavy handwriting, or major fields missing

## STRICT RULES

1. The input must be a bill, receipt, or invoice. If it is not (a photo of a
   cat, a blank page, a screenshot of an app), set error to "not_a_bill",
   explain in error_reason, and leave the other fields null or empty.
2. Numbers only in numeric fields. "Rs. 150", "150/-", "Rs150" all become 150.
3. Never invent a value. A field you cannot see is null.
4. Per line, quantity times rate must equal amount. When the printed amount
   disagrees with the printed rate, TRUST THE PRINTED AMOUNT and set
   rate = amount / quantity.
5. grand_total is the final amount paid. If both "Total" and "Grand Total"
   appear, use Grand Total. If the bill shows cash tendered and change
   returned, the grand total is the bill amount, not the cash tendered.
6. If line amounts + tax + round_off does not equal grand_total, still report
   every number exactly as printed and set reconciliation_warning to true.
   Do not silently repair the arithmetic: a downstream validator has to see
   the discrepancy so a human can look at it.
7. A digit you are unsure of is worth flagging. Lower the confidence rather
   than guessing a plausible number.
"""


def _num(desc):
    return {"type": ["number", "null"], "description": desc}


def _str(desc):
    return {"type": ["string", "null"], "description": desc}


# Enforced by output_config.format, so the response is always this shape.
# Structured outputs need every key listed in `required` with
# additionalProperties false, so optional fields are nullable instead.
BILL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "error", "error_reason", "vendor_name", "vendor_gstin", "bill_date",
        "bill_date_raw", "bill_number", "payment_method", "category",
        "confidence", "line_items", "subtotal", "cgst", "sgst", "igst",
        "tax_amount", "round_off", "grand_total", "reconciliation_warning",
    ],
    "properties": {
        "error": {
            "type": ["string", "null"],
            "description": "null when extraction succeeded, else not_a_bill "
                           "or unreadable",
        },
        "error_reason": _str("why the image could not be extracted"),
        "vendor_name": _str("shop, person, or company name as printed"),
        "vendor_gstin": _str("15-character GSTIN if clearly visible"),
        "bill_date": _str("ISO date YYYY-MM-DD, or null if unparseable"),
        "bill_date_raw": _str("printed date text when bill_date is null"),
        "bill_number": _str("invoice or receipt number"),
        "payment_method": {"type": "string", "enum": PAYMENT_METHODS},
        "category": {"type": "string", "enum": CATEGORIES},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "description", "quantity", "unit",
                    "rate", "amount", "tax_percent",
                ],
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": "number"},
                    "unit": _str("pcs, kg, ltr, box, trip, km, ..."),
                    "rate": {"type": "number"},
                    "amount": {"type": "number"},
                    "tax_percent": _num("per-line GST or VAT percentage"),
                },
            },
        },
        "subtotal": _num("sum of line amounts before tax"),
        "cgst": _num("central GST amount"),
        "sgst": _num("state GST amount"),
        "igst": _num("integrated GST amount"),
        "tax_amount": _num("total tax"),
        "round_off": _num("rounding adjustment, usually -0.50 to +0.50"),
        "grand_total": _num("final amount paid"),
        "reconciliation_warning": {"type": "boolean"},
    },
}


def build_system_prompt(vendor_hints: str | None = None) -> list:
    """
    System prompt as content blocks so the stable part can be cached.

    Hints learned from bills a human already confirmed for this supplier go in
    a second block, after the cache breakpoint, so adding them never
    invalidates the cached prefix.
    """
    blocks = [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    if vendor_hints:
        blocks.append({"type": "text", "text": vendor_hints})
    return blocks


def build_messages(image_base64: str, mime_type: str = "image/jpeg") -> list:
    """Build the messages payload for the vision call."""
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime_type,
                        "data": image_base64,
                    },
                },
                {"type": "text", "text": "Extract this daily bill."},
            ],
        }
    ]
