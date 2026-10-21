"""
Checks the Vision API reader offline: the HTTP call is stubbed, so this needs
neither the Vision API nor a key.

    python -m tools.vision_api_test
"""

import io
import os
import sys

os.environ["VISION_API_KEY"] = os.environ.get("VISION_API_KEY") or "sk_live_test"

import httpx  # noqa: E402
from PIL import Image  # noqa: E402

from app.extraction import vision_api  # noqa: E402
from app.extraction.claude_vision import ExtractionError  # noqa: E402
from app.extraction.validate import validate_bill  # noqa: E402

failures = 0


def check(name: str, ok: bool) -> None:
    global failures
    print(f"  {'pass' if ok else 'FAIL'}  {name}")
    if not ok:
        failures += 1


def f(value, status="ok"):
    return {"value": value, "status": status}


API_RESULT = {
    "id": "abc",
    "confidence": 0.93,
    "model": "paddleocr-vl",
    "reasoning_model": "qwen3-vl-2b",
    "processing_ms": 21000,
    "raw_text": "SRI MURUGAN STORES\nPaid by UPI",
    "invoice": {
        "invoice_number": f("B-102"),
        "invoice_date": f("2026-09-30"),
        "vendor_name": f("SRI MURUGAN STORES"),
        "vendor_gstin": f("33ABCDE1234F1Z5"),
        "items": [
            {"description": f("Milk"), "quantity": f("2"), "unit": f("ltr"), "rate": f("30"),
             "taxable_value": f("60"), "tax_rate": f("5")},
            {"description": f("Bread"), "quantity": f(None, "missing"), "rate": f(None, "missing"),
             "total": f("40")},
            {"description": f("Kurta"), "design": f("K-11"), "size": f("S,M,L"), "quantity": f("3"),
             "rate": f("100"), "taxable_value": f("300")},
            {"description": f("Noise row")},
        ],
        "subtotal": f("400"),
        "cgst": f("10"),
        "sgst": f("10"),
        "igst": {"value": None, "status": "missing"},
        "round_off": f("0"),
        "total_amount": f("420"),
    },
    "details": {},
    "validation": {"checks": [{"name": "x", "status": "pass"}]},
}

print("mapping")
bill = vision_api.to_bill(API_RESULT, [])
check("vendor, gstin, number, date", (bill["vendor_name"], bill["vendor_gstin"], bill["bill_number"], bill["bill_date"])
      == ("SRI MURUGAN STORES", "33ABCDE1234F1Z5", "B-102", "2026-09-30"))
check("rows without an amount are dropped", len(bill["line_items"]) == 3)
milk, bread, kurta = bill["line_items"]
check("line uses taxable value as amount", (milk["quantity"], milk["rate"], milk["amount"], milk["tax_percent"])
      == (2.0, 30.0, 60.0, 5.0))
check("missing qty defaults to 1, rate from amount", (bread["quantity"], bread["rate"], bread["amount"]) == (1.0, 40.0, 40.0))
check("design and size kept in description", kurta["description"] == "Kurta K-11 (S,M,L)")
check("tax_amount = cgst + sgst", bill["tax_amount"] == 20.0)
check("grand_total", bill["grand_total"] == 420.0)
check("0.93 is high confidence", bill["confidence"] == "high")
check("UPI found in the text", bill["payment_method"] == "upi")
check("no profile: miscellaneous", bill["category"] == "miscellaneous")
check("schema keys complete", set(bill) >= {"error", "line_items", "subtotal", "grand_total", "reconciliation_warning"})
check("validator finds no errors in a consistent bill", not validate_bill(bill).errors)

profile = [{"vendor_name": "Sri Murugan Stores", "category": "groceries", "payment_method": "cash"}]
bill = vision_api.to_bill(API_RESULT, profile)
check("trained vendor sets category and payment", (bill["category"], bill["payment_method"]) == ("groceries", "cash"))

check("0.75 is medium", vision_api.to_bill({**API_RESULT, "confidence": 0.75})["confidence"] == "medium")
check("0.4 is low", vision_api.to_bill({**API_RESULT, "confidence": 0.4})["confidence"] == "low")

empty = vision_api.to_bill({"invoice": {"items": [], "total_amount": f(None, "missing")}, "confidence": 0.1})
check("nothing read: not_a_bill", empty["error"] == "not_a_bill")

failed = {**API_RESULT, "validation": {"checks": [{"name": "total", "status": "fail"}]}}
check("failed API check sets reconciliation_warning", vision_api.to_bill(failed)["reconciliation_warning"] is True)

print("http")
buf = io.BytesIO()
Image.new("RGB", (40, 30), "white").save(buf, format="PNG")
PNG = buf.getvalue()
sent = {}


def fake_post(status, body=None, exc=None):
    def post(url, headers, files, timeout):
        if exc:
            raise exc
        sent.update(url=url, auth=headers["Authorization"], file=files["file"])
        return httpx.Response(status, json=body or {}, request=httpx.Request("POST", url))
    return post


real_post = vision_api.httpx.post
try:
    vision_api.httpx.post = fake_post(200, API_RESULT)
    data, usage = vision_api.extract_bill(PNG, [])
    check("posts to /v1/invoices/extract", sent["url"].endswith("/v1/invoices/extract"))
    check("sends the key as Bearer", sent["auth"].startswith("Bearer sk_live_"))
    check("uploads a JPEG", sent["file"][2] == "image/jpeg")
    check("returns the mapped bill", data["grand_total"] == 420.0)
    check("usage carries the model and invoice id", (usage["model"], usage["vision_invoice_id"]) == ("paddleocr-vl", "abc"))

    for status, body, word in (
        (401, {"detail": "Invalid API key"}, "rejected the key"),
        (429, {"detail": "Monthly quota exceeded"}, "limit"),
        (503, {"detail": "model down"}, "not running"),
        (504, {"detail": "timeout"}, "timed out"),
    ):
        vision_api.httpx.post = fake_post(status, body)
        try:
            vision_api.extract_bill(PNG)
            check(f"{status} raises", False)
        except ExtractionError as exc:
            check(f"{status} -> clear, retryable message", word in str(exc) and exc.retryable)

    vision_api.httpx.post = fake_post(0, exc=httpx.ConnectError("refused"))
    try:
        vision_api.extract_bill(PNG)
        check("connection refused raises", False)
    except ExtractionError as exc:
        check("connection refused -> says to start the API", "start-all.bat" in str(exc))
finally:
    vision_api.httpx.post = real_post

if failures:
    print(f"\n{failures} check(s) failed")
    sys.exit(1)
print("\nvision api reader works")
