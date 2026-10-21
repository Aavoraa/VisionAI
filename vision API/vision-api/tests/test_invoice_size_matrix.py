"""Garment invoices with a size matrix: one line per size (``size_matrix_items``)."""

from decimal import Decimal

from app.schemas.invoice import Invoice
from app.services.invoice_extraction import SIZES_GUESSED, _column_summary, parse_otsl, size_matrix_items

CAPTION = "<fcel>Sr.<fcel>HSN & Product Desc<fcel>SIZE<fcel>DETAILS<lcel><lcel><lcel><fcel>Qty<fcel>Gross Amount<fcel>Disc %<fcel>Disc Amt<fcel>GST Rate<fcel>Net Amount<nl>"
# Real rows (PaddleOCR-VL): style 1 has 4 pieces under 6 sizes, style 2 inherits the sizes above
# it and its qty row carries a stray "1"; style 3 has lost one of its six rates.
BAND = CAPTION + (
    "<fcel>1<fcel>HSN 620443<fcel>Size<fcel>16 18 20 22 24 26<lcel><lcel><lcel><fcel>4<fcel>6360<fcel>10<fcel>636<fcel>5<fcel>5724<nl>"
    "<ucel><fcel>5125 - MAROON PARTY FROCK<fcel>Qty<fcel>1 1 1 1<lcel><lcel><lcel><ucel><ucel><ucel><ucel><ucel><ucel><nl>"
    "<ucel><ucel><fcel>Rate<fcel>1555 1555 1625 1625<lcel><lcel><lcel><ucel><ucel><ucel><ucel><ucel><ucel><nl>"
    "<ucel><ucel><fcel>Mrp<fcel>2490 2490 2600 2600<lcel><lcel><lcel><ucel><ucel><ucel><ucel><ucel><ucel><nl>"
    "<fcel>2<fcel>5129 - F. BLUE PARTY FROCK<fcel>Qty<fcel>1 1 1 1 1 1 1<lcel><lcel><lcel><fcel>6<fcel>8670<fcel>10<fcel>867<fcel>5<fcel>7803<nl>"
    "<ecel><ecel><fcel>Rate<fcel>1375 1375 1445 1445 1515 1515<lcel><lcel><lcel><ecel><ecel><ecel><ecel><ecel><ecel><nl>"
    "<ecel><ecel><fcel>Mrp<fcel>2200 2200 2310 2310 2425 2425<lcel><lcel><lcel><ecel><ecel><ecel><ecel><ecel><ecel><nl>"
    "<fcel>3<fcel>HSN 620443<fcel>Size<fcel>12 14 16 18 20 22 24 26 28 30 32<lcel><lcel><lcel><fcel>6<fcel>7500<fcel>10<fcel>750<fcel>5<fcel>6750<nl>"
    "<ucel><fcel>9203 - TOMATO<fcel>Qty<fcel>1 1 1 1 1 1<lcel><lcel><lcel><ucel><ucel><ucel><ucel><ucel><ucel><nl>"
    "<ucel><fcel>PARTY FROCK<fcel>Rate<fcel>1180 1180 1250 1320 1320<lcel><lcel><lcel><ucel><ucel><ucel><ucel><ucel><ucel><nl>"
    "<fcel>REMARKS :<lcel><lcel><lcel><lcel><lcel><fcel>TOTAL<lcel><fcel>16<fcel>22530<ecel><fcel>2253<ecel><fcel>20277<nl>"
)


def test_each_style_is_one_line_with_design_sizes_and_average_rate_and_mrp() -> None:
    items, warnings = size_matrix_items([parse_otsl(BAND)])  # type: ignore[misc]
    lines = [(i.description.value, i.design.value, i.size.value, i.hsn_sac.value, i.quantity.value, i.unit.value,
              i.mrp.value, i.discount_percent.value, i.rate.value, i.tax_rate.value, i.taxable_value.value) for i in items]
    assert lines == [
        ("Maroon Party Frock", "5125", "16,18,20,22", "620443", 4, "pcs", Decimal("2545.00"), 10, Decimal("1590.00"), 5, 5724),
        ("F. Blue Party Frock", "5129", "16,18,20,22,24,26", "620443", 6, "pcs", Decimal("2311.67"), 10, Decimal("1445.00"), 5, 7803),
        # All six rates are never read for this style: the rate is still gross / qty; no MRP row.
        ("Tomato Party Frock", "9203", "12,14,16,18,20,22", "620443", 6, "pcs", None, 10, Decimal("1250.00"), 5, 6750),
    ]
    assert [i.line_number.value for i in items] == [1, 2, 3]
    assert items[1].size.status == "ok"  # six pieces under six sizes (inherited from the row above)


def test_sizes_that_cannot_be_placed_are_a_marked_guess() -> None:
    items, warnings = size_matrix_items([parse_otsl(BAND)])  # type: ignore[misc]
    maroon, tomato = items[0], items[2]
    assert maroon.size.status == tomato.size.status == "check"
    assert maroon.size.note == SIZES_GUESSED.format(printed="16 18 20 22 24 26", count=4)
    assert "12 14 16 18 20 22 24 26 28 30 32" in (tomato.size.note or "")
    assert len([w for w in warnings if "is not certain" in w]) == 2


def test_other_tables_are_not_size_matrices() -> None:
    goods = "<fcel>Sl No.<fcel>Description of Goods<fcel>HSN/SAC<fcel>Quantity<fcel>Rate<fcel>Amount<nl><fcel>1<fcel>Shirt<fcel>6205<fcel>2 pcs<fcel>100.00<fcel>200.00<nl>"
    assert size_matrix_items([parse_otsl(goods)]) is None


def test_a_tax_summary_laid_out_as_a_table() -> None:
    summary = parse_otsl(
        "<fcel>-5.<fcel>HsnCode<fcel>Qty<fcel>Taxable Amt<fcel>GST %<fcel>IGST<fcel>Net Amount<lcel><fcel>IGST<fcel>1804.95<nl>"
        "<ecel><fcel>620443<fcel>28<fcel>36099.00<fcel>5.00<fcel>1804.95<fcel>37903.95<lcel><ecel><ecel><nl>"
    )
    found = {key: value.value for key, value in _column_summary([summary]).items()}
    assert found == {"subtotal": Decimal("36099.00"), "total_amount": Decimal("37903.95"), "igst": Decimal("1804.95")}


def test_old_results_without_size_and_mrp_still_load() -> None:
    item = Invoice.model_validate({"items": [{"line_number": {"value": 1, "status": "ok"}}]}).items[0]
    assert item.size.status == "missing" and item.mrp.value is None
