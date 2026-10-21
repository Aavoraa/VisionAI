# backend/app/extraction/vision_api.py
"""
Vision API caller: reads the bill with our own local Vision AI service
(PaddleOCR-VL + Qwen3-VL on the GPU) instead of the Claude API.

Same contract as claude_vision.extract_bill: one image in, a dict shaped like
BILL_SCHEMA out, plus a usage dict. Reconciliation stays in validate.py.
"""

from __future__ import annotations

import logging
import re

import httpx

from ..config import settings
from .claude_vision import ExtractionError, prepare_image
from .prompt import CATEGORIES

log = logging.getLogger(__name__)

# Our API scores confidence 0-1; Bill Scan stores high / medium / low. The
# cut-offs are the API's own routing thresholds (approved / needs review).
HIGH, MEDIUM = 0.90, 0.70


def _value(field):
    """The value of one {value, status, ...} field, or None."""
    if isinstance(field, dict):
        return field.get("value")
    return None


def _num(field) -> float | None:
    v = _value(field)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _str(field) -> str | None:
    v = _value(field)
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _confidence(score: float | None) -> str:
    if score is None:
        return "low"
    if score >= HIGH:
        return "high"
    if score >= MEDIUM:
        return "medium"
    return "low"


def _line(item: dict) -> dict | None:
    """One API line item as a Bill Scan line, or None when it has no amount."""
    qty = _num(item.get("quantity"))
    rate = _num(item.get("rate"))
    amount = _num(item.get("taxable_value"))
    if amount is None:
        amount = _num(item.get("total"))
    if amount is None and qty is not None and rate is not None:
        amount = round(qty * rate, 2)
    if amount is None:
        return None
    if qty is None or qty == 0:
        qty = 1.0
    if rate is None:
        rate = round(amount / qty, 2)

    description = _str(item.get("description")) or ""
    design, size = _str(item.get("design")), _str(item.get("size"))
    extra = " ".join(p for p in (design, f"({size})" if size else None) if p)
    if extra and extra not in description:
        description = f"{description} {extra}".strip()

    return {
        "description": description or "Item",
        "quantity": qty,
        "unit": _str(item.get("unit")),
        "rate": rate,
        "amount": amount,
        "tax_percent": _num(item.get("tax_rate")),
    }


def vendor_key_for_hint(name: str | None) -> str | None:
    """Vendor name with case, punctuation and spacing removed (as bills.vendor_key)."""
    if not name:
        return None
    return re.sub(r"[^a-z0-9]+", "", name.lower()) or None


def _matching_profile(vendor: str | None, profiles: list[dict] | None) -> dict | None:
    key = vendor_key_for_hint(vendor)
    if not key:
        return None
    for p in profiles or []:
        if vendor_key_for_hint(p.get("vendor_name")) == key:
            return p
    return None


def _payment_method(raw_text: str) -> str:
    text = raw_text.upper()
    if re.search(r"\bUPI\b|PHONEPE|GPAY|GOOGLE PAY|PAYTM", text):
        return "upi"
    if re.search(r"\b(CARD|VISA|MASTERCARD|RUPAY)\b", text):
        return "card"
    if re.search(r"\bCASH\b", text):
        return "cash"
    return "unknown"


def to_bill(result: dict, profiles: list[dict] | None = None) -> dict:
    """Convert our API's InvoiceExtraction JSON into a BILL_SCHEMA dict."""
    inv = result.get("invoice") or {}
    details = result.get("details") or {}

    lines = [l for l in (_line(i) for i in inv.get("items") or []) if l]
    grand_total = _num(inv.get("total_amount"))

    cgst, sgst, igst = (_num(inv.get(k)) for k in ("cgst", "sgst", "igst"))
    tax_amount = _num(details.get("tax_total"))
    if tax_amount is None:
        parts = [t for t in (cgst, sgst, igst) if t is not None]
        tax_amount = round(sum(parts), 2) if parts else 0.0

    subtotal = _num(inv.get("subtotal"))
    if subtotal is None and lines:
        subtotal = round(sum(l["amount"] for l in lines), 2)

    vendor = _str(inv.get("vendor_name"))
    profile = _matching_profile(vendor, profiles)
    category = (profile or {}).get("category")
    if category not in CATEGORIES:
        category = "miscellaneous"
    payment = (profile or {}).get("payment_method") or _payment_method(result.get("raw_text") or "")

    bill = {
        "error": None,
        "error_reason": None,
        "vendor_name": vendor,
        "vendor_gstin": _str(inv.get("vendor_gstin")) or (profile or {}).get("vendor_gstin"),
        "bill_date": _str(inv.get("invoice_date")),
        "bill_date_raw": None,
        "bill_number": _str(inv.get("invoice_number")),
        "payment_method": payment if payment in ("cash", "upi", "card", "unknown") else "unknown",
        "category": category,
        "confidence": _confidence(result.get("confidence")),
        "line_items": lines,
        "subtotal": subtotal,
        "cgst": cgst,
        "sgst": sgst,
        "igst": igst,
        "tax_amount": tax_amount,
        "round_off": _num(inv.get("round_off")) or 0.0,
        "grand_total": grand_total,
        "reconciliation_warning": False,
    }

    validation = result.get("validation") or {}
    if any(c.get("status") == "fail" for c in validation.get("checks") or []):
        bill["reconciliation_warning"] = True

    if not lines and grand_total is None:
        bill["error"] = "not_a_bill"
        bill["error_reason"] = (
            "No line items or total could be read from this image. "
            "If it is a bill, retake the photo flat and well lit, or enter it manually."
        )
    return bill


def _api_error(resp: httpx.Response) -> ExtractionError:
    try:
        body = resp.json()
        detail = body.get("detail") or (body.get("error") or {}).get("message")
    except ValueError:
        detail = None
    detail = detail if isinstance(detail, str) else resp.text[:200]
    code = resp.status_code
    if code in (401, 403):
        return ExtractionError(
            "The Vision API rejected the key. Put a valid sk_live_ key in "
            "VISION_API_KEY in backend/.env and restart the server.",
            retryable=True,
        )
    if code == 429:
        return ExtractionError(f"Vision API limit reached: {detail}", retryable=True)
    if code == 504:
        return ExtractionError("The Vision API timed out reading this bill. Try again.", retryable=True)
    if code == 503:
        return ExtractionError(
            f"The Vision API models are not running ({detail}). Start them with start-all.bat.",
            retryable=True,
        )
    return ExtractionError(f"Vision API returned {code}: {detail}", retryable=code >= 500)


def extract_bill(
    raw_image: bytes,
    vendor_profiles: list[dict] | None = None,
) -> tuple[dict, dict]:
    """Send one bill image to our Vision API and return (bill_dict, usage_dict)."""
    if not settings.vision_api_key:
        raise ExtractionError(
            "No Vision API key. Set VISION_API_KEY in backend/.env and restart the server.",
            retryable=True,
        )

    # Same normalisation as before (EXIF rotation, flatten), but keep the full
    # resolution: our OCR reads small print better from more pixels.
    import base64

    image_b64, media_type = prepare_image(raw_image, max_edge=None)
    image = base64.standard_b64decode(image_b64)

    url = settings.vision_api_url.rstrip("/") + "/v1/invoices/extract"
    try:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {settings.vision_api_key}"},
            files={"file": ("bill.jpg", image, media_type)},
            timeout=settings.vision_api_timeout,
        )
    except httpx.TimeoutException as exc:
        raise ExtractionError("The Vision API timed out reading this bill. Try again.", retryable=True) from exc
    except httpx.HTTPError as exc:
        raise ExtractionError(
            f"Could not reach the Vision API at {settings.vision_api_url}. Is it running (start-all.bat)?",
            retryable=True,
        ) from exc

    if resp.status_code != 200:
        raise _api_error(resp)

    result = resp.json()
    bill = to_bill(result, vendor_profiles)
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "model": result.get("model"),
        "reasoning_model": result.get("reasoning_model"),
        "vision_invoice_id": result.get("id"),
        "vision_confidence": result.get("confidence"),
        "processing_ms": result.get("processing_ms"),
    }
    log.info(
        "Extracted bill via Vision API: vendor=%r total=%s confidence=%s in %s ms",
        bill.get("vendor_name"), bill.get("grand_total"), result.get("confidence"), result.get("processing_ms"),
    )
    return bill, usage
