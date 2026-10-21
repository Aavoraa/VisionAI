"""Handwritten purchase orders: lines per colour, size counts checked against the written total."""

from decimal import Decimal

from app.schemas.invoice import InvoiceDetails
from app.services.invoice_extraction import map_invoice, parse_otsl
from app.services.invoice_validation import validate_invoice
from app.services.purchase_order import purchase_order_details, purchase_order_fields, purchase_order_items

HEADER = """ESSA
PURCHASE ORDER FORM
ESSA GARMENTS PRIVATE LIMITED
21, Venkatesaiya Colony, Kangeyam Road, TIRUPUR - 641 604.
GSTIN: 33AADCE6591N1Z7
Date: 10.7.2025
Company Name: H.R.m Fabric
Transport:
Brand Name: Agent Direct"""
# The whole-page table as PaddleOCR-VL read the photo: "1" for a handwritten L, "X" for none,
# a handwritten 5 read as "S", the line total as "55.PC".
TABLE = (
    "<fcel>No.<fcel>ESSA GARMENTS PRIVATE LIMITED<lcel><lcel><nl>"
    "<fcel>Company Name<fcel>H.R.M. Fabric<fcel>Transport<lcel><nl>"
    "<fcel>Brand Name<ecel><fcel>Agent<fcel>Direct<nl>"
    "<fcel>Item<fcel>Ladies Pattiyala Pant<fcel>Purchaser<fcel>Hakkeem<nl>"
    "<fcel>Place<fcel>Coimbatore<fcel>Discount<lcel><nl>"
    "<fcel>S.No.<fcel>Particulars<fcel>Size<lcel><fcel>Quantity<fcel>Rate<nl>"
    "<ecel><fcel>1) Sandal 1 XL 2XL 3XL<ecel><lcel><ecel><ecel><nl>"
    "<ecel><fcel>10 10 20<fcel>15<lcel><fcel>55.PC<ecel><nl>"
    "<fcel>4) Brown 1 XL 2XL 3XL<ecel><ecel><lcel><ecel><ecel><nl>"
    "<ecel><fcel>X 2 5<fcel>5<lcel><fcel>12.PC<ecel><nl>"
    "<fcel>5) Gray 1 XL 2XL 3XL<ecel><ecel><lcel><ecel><ecel><nl>"
    "<ecel><fcel>S 5 S<fcel>5<lcel><fcel>20.PC<ecel><nl>"
    "<fcel>7) Navy 1 XL 2XL 3XL<ecel><ecel><lcel><ecel><ecel><nl>"
    "<ecel><fcel>X 5 S<fcel>5<lcel><fcel>17.PC<ecel><nl>"  # misread total: 0+5+5+5 = 15
)


def test_each_colour_is_a_line_with_its_sizes_and_pieces() -> None:
    items, warnings = purchase_order_items([parse_otsl(TABLE)], "Ladies Pattiyala Pant")
    lines = [(i.line_number.value, i.description.value, i.size.value, i.quantity.value, i.unit.value) for i in items]
    assert lines[:3] == [
        (1, "Ladies Pattiyala Pant - Sandal", "L-10, XL-10, 2XL-20, 3XL-15", Decimal(55), "pcs"),
        (4, "Ladies Pattiyala Pant - Brown", "XL-2, 2XL-5, 3XL-5", Decimal(12), "pcs"),  # "X" = none
        (5, "Ladies Pattiyala Pant - Gray", "L-5, XL-5, 2XL-5, 3XL-5", Decimal(20), "pcs"),  # "S" = 5
    ]
    assert items[0].size.status == "ok"
    navy = items[3]  # its counts add up to 15, the written total reads 17
    assert navy.size.status == navy.quantity.status == "check" and "17" in (navy.size.note or "")
    assert len(warnings) == 1 and "line 7" in warnings[0]


def test_the_supplier_is_the_company_name_and_the_buyer_the_printed_firm() -> None:
    updates, labelled = purchase_order_fields(HEADER, [parse_otsl(TABLE)])
    assert updates["vendor_name"].value == "H.R.M. Fabric" and updates["vendor_name"].status == "check"
    assert updates["vendor_gstin"].value is None
    assert (updates["buyer_name"].value, updates["buyer_gstin"].value) == ("ESSA GARMENTS PRIVATE LIMITED", "33AADCE6591N1Z7")
    assert updates["total_amount"].value is None  # never a line total
    details = purchase_order_details(InvoiceDetails(), labelled)
    assert (details.agent.value, details.destination.value, details.transporter.value) == ("Direct", "Coimbatore", None)


def test_an_order_is_validated_as_an_order() -> None:
    invoice, _ = map_invoice(HEADER, [TABLE])
    document = "\n".join([HEADER, TABLE])
    _, report = validate_invoice(invoice, document)
    checks = {c.id: c for c in report.checks}
    assert checks["required_fields"].message == "Not found: invoice_number"  # no supplier GSTIN or total required
    assert checks["size_counts"].status == "fail" and "line 7" in checks["size_counts"].message
    assert checks["grand_total"].status == "skip"
