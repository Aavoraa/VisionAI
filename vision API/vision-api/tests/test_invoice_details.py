"""Purchase-entry details: e-invoice, transport, order, bank and contact fields, derived states and rates."""

import datetime as dt
from decimal import Decimal

from app.schemas.invoice import Extracted, Invoice
from app.services.invoice_details import extract_details

SELLER = "27AQVPS2006D1ZR"
BUYER = "33AADCE6591N1Z7"

HEADER = """Tax Invoice (DUPLICATE FOR TRANSPORTER) e-Invoice
IRN : e0d97bec251a374a9af1713dd49261acbcbc6a-
ee09733f241a5344e7bd042732
Ack No. : 122633615104491
Ack Date : 15-Jul-26
KRISHNA DUPATTA CENTRE
Shop No F-63, 1st Floor Manish Market
Contact : 9322305066
E-Mail : krishnadupattacentre63@gmail.com
GOLDEN TRANSPORT SERVICE
e-Way Bill No. : 252245502683
DUE DATE : 15/09/2026
"""
TABLE = (
    "<fcel>Reference No. & Date.\n833 dt. 15-Jul-26<fcel>Other References<nl>"
    "<fcel>Dispatched through<fcel>Destination<nl>"
    "<fcel>Dispatched through<nl><fcel>Golden Transports Terms of Delivery<nl>"
    "<fcel>Destination<nl><fcel>Booking Tirupur<nl>"
    "<fcel>Name of Broker<nl><fcel>Matoshree Agency<nl>"
    "<fcel>L.R. No. 255248<fcel>L.R. Date : 03/07/2026<nl>"
    "<fcel>Amount Chargeable (in words)<nl><fcel>INR Forty Thousand Five Hundred Sixty Two Only<nl>"
    "<fcel>Tax Amount (in words) : INR One Thousand Nine Hundred Thirty One and Fifty Paise Only<nl>"
)
FOOTER = """Company's Bank Details
Bank Name : HDFC Bank Ltd A/c. No. 11197610000090
Branch & IFS Code : Bhavani Shankar Road, Dadar West & HDFCO001119
for KRISHNA DUPATTA CENTRE
"""


def invoice(**values: object) -> Invoice:
    return Invoice(**{k: Extracted.found(v, str(v)) for k, v in values.items()})


def details():
    inv = invoice(vendor_gstin=SELLER, buyer_gstin=BUYER, subtotal=Decimal("38630.00"), igst=Decimal("1931.50"),
                  total_amount=Decimal("40562.00"))
    return extract_details(HEADER, [TABLE], FOOTER, inv)


def test_e_invoice_block() -> None:
    d = details()
    assert d.irn.value == "e0d97bec251a374a9af1713dd49261acbcbc6aee09733f241a5344e7bd042732"  # joined across the wrap
    assert (d.ack_no.value, d.ack_date.value) == ("122633615104491", dt.date(2026, 7, 15))
    assert d.eway_bill.value == "252245502683"
    assert d.due_date.value == dt.date(2026, 9, 15)


def test_order_and_transport() -> None:
    d = details()
    assert (d.reference_no.value, d.reference_date.value) == ("833", dt.date(2026, 7, 15))
    assert d.transporter.value == "Golden Transports"  # not "FOR TRANSPORTER", not the company name line
    assert d.destination.value == "Tirupur"
    assert d.broker.value == "Matoshree Agency"
    assert (d.lr_no.value, d.lr_date.value) == ("255248", dt.date(2026, 7, 3))


def test_bank_block() -> None:
    d = details()
    assert d.bank_name.value == "HDFC Bank Ltd"
    assert d.bank_account.value == "11197610000090"
    assert d.bank_ifsc.value == "HDFC0001119" and "5th character" in (d.bank_ifsc.note or "")
    assert d.bank_branch.value == "Bhavani Shankar Road, Dadar West"


def test_contacts_and_derived_values() -> None:
    d = details()
    assert (d.vendor_phone.value, d.vendor_email.value) == ("9322305066", "krishnadupattacentre63@gmail.com")
    assert d.vendor_pan.value == "AQVPS2006D" and "GSTIN" in (d.vendor_pan.note or "")
    assert (d.vendor_state.value, d.vendor_state_code.value) == ("Maharashtra", "27")
    assert (d.buyer_state.value, d.buyer_state_code.value) == ("Tamil Nadu", "33")
    assert d.igst_rate.value == 5 and d.cgst_rate.value is None
    assert d.tax_total.value == Decimal("1931.50")
    assert d.gross_amount.value == Decimal("38630.00")
    assert d.amount_in_words.value == "INR Forty Thousand Five Hundred Sixty Two Only"  # not the tax line
    assert d.amount_in_words.status == "ok"


def test_half_rates_and_discount_base() -> None:
    inv = invoice(subtotal=Decimal("47589.30"), cgst=Decimal("1189.83"), sgst=Decimal("1189.83"))
    d = extract_details("", [], "", inv)
    assert (d.cgst_rate.value, d.sgst_rate.value) == (Decimal("2.5"), Decimal("2.5"))
    discounted = invoice(subtotal=Decimal("119470.00"), discount=Decimal("5973.50"), igst=Decimal("5674.83"))
    assert extract_details("", [], "", discounted).igst_rate.value == 5  # rate on the amount after discount


def test_discount_read_as_the_subtotal_does_not_crash() -> None:
    # Mamta Arts page 1: the discount was misread as 119,470.00, the same as the subtotal
    inv = invoice(subtotal=Decimal("119470.00"), discount=Decimal("119470.00"), igst=Decimal("674.83"))
    assert extract_details("", [], "", inv).igst_rate.status == "check"  # not a GST rate, but no ZeroDivision


def test_nothing_is_invented() -> None:
    d = extract_details("Tax Invoice\nDispatched through\ne-Invoice\nOUR BANK DETAILS\n", [], "", Invoice())
    assert all(field.value is None for _, field in d)


def test_layout_variants() -> None:
    header = "Invoice No. e-Way Bill No. Dated\nBF-2453/26-27 242245499788 17-Jul-26\nBank Name: HDFC Bank A/c No.: 50200026700301\n"
    footer = "Payment within 0 Days.Due Date: 15/07/2026\nIFSC Code :\nICIC0001565\n"
    d = extract_details(header, [], footer, Invoice(), bands=["<fcel>Amount Chargeable (in words): INR Sixty Two Only<nl>"])
    assert d.eway_bill.value == "242245499788" and d.eway_bill.status == "check"  # under a row of captions
    assert (d.bank_name.value, d.bank_account.value) == ("HDFC Bank", "50200026700301")
    assert d.due_date.value == dt.date(2026, 7, 15)  # caption right after a full stop
    assert d.bank_ifsc.value == "ICIC0001565"  # on the line below its caption


def test_package_counts_are_not_order_numbers() -> None:
    d = extract_details("Buyer's Order No.\n1 parcel\nDispatch Doc No.\n", [], "", Invoice())
    assert d.order_no.value is None
