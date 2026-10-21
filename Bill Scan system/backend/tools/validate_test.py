# backend/tools/validate_test.py
"""
Self-contained checks for the reconciliation layer.

Run from backend/:  python -m tools.validate_test
No pytest, no fixtures: this is meant to be runnable on the machine that runs
the scanner, by someone who is not a Python developer.
"""

import sys
from copy import deepcopy
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.extraction.validate import apply_validation, validate_bill  # noqa: E402

TODAY = date(2026, 8, 24)

good = {
    "vendor_name": "ABC Store",
    "bill_date": "2026-08-24",
    "line_items": [
        {"description": "Milk", "quantity": 2, "rate": 30, "amount": 60},
        {"description": "Bread", "quantity": 1, "rate": 40, "amount": 40},
    ],
    "subtotal": 100,
    "tax_amount": 0,
    "round_off": 0,
    "grand_total": 100,
}

failures = []


def check(label, condition):
    if condition:
        print(f"  pass  {label}")
    else:
        print(f"  FAIL  {label}")
        failures.append(label)


def paths(result):
    return {f.field_path for f in result.flags}


print("a correct bill passes")
r = validate_bill(good, today=TODAY)
check("is_valid", r.is_valid)
check("no flags at all", not r.flags)

print("a misread digit in the grand total is caught")
bad = {**good, "grand_total": 1000}
r = validate_bill(bad, today=TODAY)
check("not valid", not r.is_valid)
check("grand_total flagged", "grand_total" in paths(r))
check("confidence capped", r.confidence_override == "low")

print("a misread line amount is caught")
bad = deepcopy(good)
bad["line_items"][0]["amount"] = 90  # 2 x 30 is 60, not 90
r = validate_bill(bad, today=TODAY)
check("line flagged", "line_items[0].amount" in paths(r))

print("a wrong subtotal does not cascade into the grand total")
# The lines and the grand total agree; only the printed subtotal is misread.
bad = {**good, "subtotal": 10}
r = validate_bill(bad, today=TODAY)
check("subtotal flagged", "subtotal" in paths(r))
check("grand_total NOT flagged", "grand_total" not in paths(r))
check("correction offered", r.corrections.get("subtotal") == 100)

print("a bill with no tax_amount key does not crash")
no_tax = {k: v for k, v in good.items() if k != "tax_amount"}
r = validate_bill(no_tax, today=TODAY)
check("still valid", r.is_valid)

print("a GST split that disagrees with the tax total warns")
taxed = {
    **good,
    "cgst": 9, "sgst": 9, "tax_amount": 20,
    "grand_total": 118,
}
r = validate_bill(taxed, today=TODAY)
check("tax_amount flagged", "tax_amount" in paths(r))
check("warning, not error", r.is_valid)
check("grand total ties out against the corrected tax",
      "grand_total" not in paths(r))

print("validate_bill does not mutate its input")
before = deepcopy(good)
validate_bill({**good, "subtotal": 10}, today=TODAY)
validate_bill(good, today=TODAY)
check("input untouched", good == before)

print("a future-dated bill warns")
r = validate_bill({**good, "bill_date": "2027-01-01"}, today=TODAY)
check("bill_date flagged", "bill_date" in paths(r))
check("warning only", r.is_valid)

print("an unparseable date is reported with its raw text")
r = validate_bill(
    {**good, "bill_date": None, "bill_date_raw": "2Ц/O8/2G"}, today=TODAY
)
check("bill_date flagged", "bill_date" in paths(r))
check("raw text in the message",
      any("2Ц/O8/2G" in f.message for f in r.flags))

print("an image the extractor rejected fails immediately")
r = validate_bill({"error": "not_a_bill", "error_reason": "It is a cat"})
check("not valid", not r.is_valid)
check("reason surfaced", any("cat" in f.message for f in r.flags))

print("an empty bill fails without crashing")
r = validate_bill({})
check("not valid", not r.is_valid)
check("line_items flagged", "line_items" in paths(r))

print("apply_validation attaches the review-screen keys")
out = apply_validation({**good, "grand_total": 1000})
check("_validation_ok present", out["_validation_ok"] is False)
check("_effective_confidence lowered", out["_effective_confidence"] == "low")
check("flags are plain dicts", isinstance(out["_validation_flags"][0], dict))

print("apply_validation(autocorrect=True) writes the suggested subtotal")
out = apply_validation({**good, "subtotal": 10}, autocorrect=True)
check("subtotal corrected", out["subtotal"] == 100)
check("flag kept so a human still sees it", len(out["_validation_flags"]) == 1)

print()
if failures:
    print(f"{len(failures)} check(s) failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("validator works")
