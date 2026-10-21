"""Phase 3: invoice validation."""

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

from app.schemas.invoice import Extracted, Invoice
from app.services.invoice_extraction import map_invoice
from app.services.invoice_validation import validate_invoice, words_to_amount
from test_invoice_extraction import FOOTER, HEADER, TABLE

TODAY = dt.date(2026, 9, 26)
WORDS = "Amount Chargeable (in words) INR Two Thousand Seventy Nine and Twenty paise Only"


def fixture_invoice() -> tuple[Invoice, str]:
    """Inter-state (Karnataka 29 -> Maharashtra 27) invoice: 2 lines, IGST 5%, round-off 0.20."""
    invoice, _ = map_invoice(HEADER, [TABLE], FOOTER)
    return invoice, "\n".join([HEADER, TABLE, FOOTER, WORDS])


def results(invoice: Invoice, document: str = "") -> tuple[Invoice, dict[str, Any]]:
    checked, report = validate_invoice(invoice, document, today=TODAY)
    return checked, {c.id: c for c in report.checks} | {"_status": report.status}


def value(field_value: str | int | None) -> Extracted[Any]:
    return Extracted.found(Decimal(str(field_value))) if field_value is not None else Extracted()


@pytest.mark.parametrize(
    ("words", "amount"),
    [
        ("INR Forty Nine Thousand Nine Hundred Sixty Nine Only", "49969"),
        ("INR Two Thousand Three Hundred Seventy Nine and Sixty Six paise Only", "2379.66"),
        ("Rupees One Crore Twenty Lakh Five Only", "12000005"),
        ("INR Nine Hundred Ninety-Nine Only", "999"),
        ("INR One Hundred and One Only", "101"),
    ],
)
def test_words_to_amount(words: str, amount: str) -> None:
    assert words_to_amount(words) == Decimal(amount)


@pytest.mark.parametrize("words", ["INR Forty Nine Thousand Banana Only", "INR and fifty paise Only", "Only", "INR Ten and Hundred paise Only"])
def test_unreadable_words_give_no_value(words: str) -> None:
    assert words_to_amount(words) is None


def test_a_consistent_invoice_passes_every_check() -> None:
    invoice, document = fixture_invoice()
    checked, checks = results(invoice, document)
    assert checks["_status"] == "passed"
    assert {k: c.status for k, c in checks.items() if k != "_status"} == {
        "required_fields": "pass", "invoice_date": "pass", "vendor_gstin": "pass", "buyer_gstin": "pass",
        "line_numbers": "pass", "line_arithmetic": "pass", "items_sum": "pass", "quantity_sum": "pass",
        "tax_type": "pass", "cgst_sgst": "skip", "tax_rate": "pass", "round_off": "pass", "grand_total": "pass",
        "amount_in_words": "pass", "tax_in_words": "skip",
    }
    assert "Different states (29 -> 27): IGST as expected" in checks["tax_type"].message
    assert checked == invoice  # nothing flagged


def test_grand_total_mismatch_flags_the_amounts() -> None:
    invoice, document = fixture_invoice()
    checked, checks = results(invoice.model_copy(update={"total_amount": value("2097.20")}), document)
    assert checks["_status"] == "failed" and checks["grand_total"].status == "fail"
    assert "= 2,079.20, total reads 2,097.20" in checks["grand_total"].message
    assert checked.total_amount.status == "check" and "total reads" in (checked.total_amount.note or "")
    assert checks["amount_in_words"].status == "fail"  # the words still say 2,079.20


def test_line_arithmetic_flags_the_wrong_line() -> None:
    invoice, document = fixture_invoice()
    first = invoice.items[0].model_copy(update={"taxable_value": value("990.00")})  # 2 x 500 - 10% = 900
    checked, checks = results(invoice.model_copy(update={"items": [first, invoice.items[1]]}), document)
    assert checks["line_arithmetic"].status == "fail"
    assert "line 1: 2 × 500.00 − 10% = 900.00, read 990.00" in checks["line_arithmetic"].message
    assert checked.items[0].taxable_value.status == "check" and checked.items[1].taxable_value.status == "ok"
    assert checks["items_sum"].status == "fail"  # 990 + 1080 != 1980


def test_dropped_rows_break_the_line_sequence() -> None:
    invoice, document = fixture_invoice()
    renumbered = invoice.items[1].model_copy(update={"line_number": Extracted.found(3)})
    checked, checks = results(invoice.model_copy(update={"items": [invoice.items[0], renumbered]}), document)
    assert checks["line_numbers"].status == "fail" and "1->3" in checks["line_numbers"].message
    assert checked.items[1].line_number.status == "check"


def test_partial_documents_skip_the_sums() -> None:
    invoice, document = fixture_invoice()
    items = [i.model_copy(update={"line_number": Extracted.found(i.line_number.value + 34)}) for i in invoice.items]
    _, checks = results(invoice.model_copy(update={"items": items}), document)
    assert checks["items_sum"].status == checks["quantity_sum"].status == "skip"
    assert "starts at line 35" in checks["items_sum"].message
    assert checks["line_numbers"].status == "pass"


def test_intra_state_rules() -> None:
    # Seen on a real invoice: SGST read as the total tax (2,379.66) instead of 1,189.83.
    invoice = Invoice(
        vendor_gstin=Extracted.found("33AHIPA5408M2ZG"),
        buyer_gstin=Extracted.found("33AADCE6591N1Z7"),
        subtotal=value("47589.30"), cgst=value("1189.83"), sgst=value("2379.66"),
        round_off=value("0.04"), total_amount=value("49969.00"),
    )
    checked, checks = results(invoice)
    assert checks["tax_type"].status == "pass"
    assert checks["cgst_sgst"].status == "fail" and checks["grand_total"].status == "fail"
    assert checked.sgst.status == "check" and checked.cgst.status == "check"
    fixed, checks = results(invoice.model_copy(update={"sgst": value("1189.83")}))
    assert checks["cgst_sgst"].status == checks["grand_total"].status == checks["tax_rate"].status == "pass"
    wrong_type, checks = results(fixed.model_copy(update={"cgst": Extracted(), "sgst": Extracted(), "igst": value("2379.66")}))
    assert checks["tax_type"].status == "fail" and wrong_type.igst.status == "check"


def test_formats_and_presence() -> None:
    invoice, document = fixture_invoice()
    broken = invoice.model_copy(update={
        "invoice_number": Extracted(),
        "invoice_date": Extracted.found(dt.date(2016, 3, 1)),
        "buyer_gstin": Extracted.check("27PQRST6789K1ZX", "GSTIN checksum does not match; verify every character"),
        "round_off": value("2.50"),
    })
    checked, checks = results(broken, document)
    assert checks["required_fields"].status == "fail" and "invoice_number" in checks["required_fields"].message
    assert checks["invoice_date"].status == "fail" and checked.invoice_date.status == "check"
    assert checks["buyer_gstin"].status == "fail"
    assert checks["round_off"].status == "fail"


@pytest.mark.parametrize("gstin", ["IGCHENNAI@GMAIL.COM", "7DE9E32D8EA6DE0CCFF6A1CCF4237B8B9", "33AHIPA5408M2ZX"])
def test_a_gstin_is_judged_by_its_value_not_its_note(gstin: str) -> None:
    # A reasoning-model value carries no "checksum" note; it must still fail when it is not a
    # GSTIN (an e-mail address, a hash from another slip) or its check character is wrong.
    invoice, document = fixture_invoice()
    broken = invoice.model_copy(update={"vendor_gstin": Extracted.check(gstin, "found only by tiny-llm; verify", gstin)})
    _, checks = results(broken, document)
    assert checks["vendor_gstin"].status == "fail"


def test_nothing_to_check_is_skipped_not_passed() -> None:
    _, checks = results(Invoice())
    assert checks["required_fields"].status == "fail"
    assert all(c.status == "skip" for k, c in checks.items() if k not in ("_status", "required_fields"))


def test_extractor_attaches_the_report() -> None:
    import asyncio

    from app.services.invoice_extraction import InvoiceExtractor
    from test_invoice_extraction import FakePaddle, page_png

    result = asyncio.run(InvoiceExtractor(FakePaddle()).extract([page_png()]))
    assert result.validation is not None and result.validation.status == "passed"
    assert any(c.id == "grand_total" and c.status == "pass" for c in result.validation.checks)
