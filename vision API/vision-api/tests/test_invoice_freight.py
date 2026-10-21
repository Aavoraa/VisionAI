"""Transport / freight bills: one row per consignment (LR), read by ``freight_items``."""

from decimal import Decimal

from app.services.invoice_extraction import FREIGHT_REALIGNED, freight_items, merge_items, parse_items, parse_otsl

CAPTION = (
    "<fcel>Sr. No<fcel>Mr. No<fcel>Dlvr. Dt<fcel>Lr. No<fcel>Inv. No<fcel>Chrg. Wt"
    "<fcel>Pkgs Consinor/ Consignee<fcel>FreightHamali Dlvr.chrg Amount<lcel><lcel><nl>"
)
# Table recognition paired each consignor cell (and its amounts) with the row above; the last
# row's right half caught the totals.
SHIFTED = CAPTION + (
    "<ecel><ecel><ecel><ecel><ecel><ecel><fcel>1 LABISA ESSA GARMENTS PVT LTD<fcel>395<fcel>0<fcel>0<nl>"
    "<fcel>1<fcel>32763<fcel>24-07-2026<fcel>BWD-12074<fcel>518<fcel>50<fcel>2 CITY GIRL ESSA GARMENTS PVT LTD<fcel>890<fcel>0<fcel>0<nl>"
    "<fcel>2<fcel>32768<fcel>24-07-2026<fcel>MBJ-17525<fcel>758/26-27<fcel>100<fcel>1 KRISHNA DUPATTA CENTER ESSA GARMENTS PVT LTD<fcel>455<fcel>20<fcel>0<nl>"
    "<fcel>3<fcel>32766<fcel>24-07-2026<fcel>MBJ-17540<fcel>KDC/833/26-27<fcel>50<fcel>LTD<fcel>1760<fcel>1760<fcel>1760<nl>"
    "<fcel>780.000<lcel><lcel><lcel><lcel><fcel>14<fcel>Total :<fcel>1760.00<lcel><lcel><nl>"
)
# A zoomed band of the same table, aligned, with two consignments in one cell and a Freight /
# Hamali / Dlvr / Amount block (a tick mark next to one amount).
BAND = CAPTION + (
    "<fcel>2<fcel>32768<fcel>24-07-2026<fcel>MBJ-17525<fcel>758/26-27<fcel>100<fcel>2 CITY GIRL ESSA GARMENTS PVT LTD\n"
    "1 KRISHNA DUPATTA CENTER ESSA GARMENTS PVT LTD<fcel>890✓<fcel>0<fcel>0<fcel>890<nl>"
    "<fcel>3<fcel>32766<fcel>24-07-2026<fcel>MBJ-17540<fcel>KDC/833/26-27<fcel>50<ecel><fcel>455<fcel>20<fcel>0<fcel>475<nl>"
)


def rows(items):  # type: ignore[no-untyped-def]
    return [(i.line_number.value, i.description.value, i.quantity.value, i.taxable_value.value) for i in items]


def test_a_shifted_consignment_table_is_realigned_and_flagged() -> None:
    items, warnings = parse_items(parse_otsl(SHIFTED))
    assert rows(items) == [
        (1, "LABISA (LR BWD-12074, Inv 518, Chrg Wt 50)", Decimal(1), Decimal(395)),
        (2, "CITY GIRL (LR MBJ-17525, Inv 758/26-27, Chrg Wt 100)", Decimal(2), Decimal(890)),
        (3, "KRISHNA DUPATTA CENTER (LR MBJ-17540, Inv KDC/833/26-27, Chrg Wt 50, Hamali 20)", Decimal(1), Decimal(475)),
    ]
    assert warnings == [FREIGHT_REALIGNED]
    assert all(i.line_number.status == i.description.status == "check" for i in items)
    assert all(i.rate.value is None and i.unit.value == "pkgs" for i in items)  # freight is per consignment


def test_an_aligned_band_gives_a_second_consignment_to_the_empty_row_below() -> None:
    items, warnings = freight_items(parse_otsl(BAND))  # type: ignore[misc]
    assert rows(items) == [
        (2, "CITY GIRL (LR MBJ-17525, Inv 758/26-27, Chrg Wt 100)", Decimal(2), Decimal(890)),
        (3, "KRISHNA DUPATTA CENTER (LR MBJ-17540, Inv KDC/833/26-27, Chrg Wt 50, Hamali 20)", Decimal(1), Decimal(475)),
    ]
    assert warnings == [] and all(i.line_number.status == "ok" for i in items)


def test_aligned_readings_win_over_realigned_ones() -> None:
    band, _ = parse_items(parse_otsl(BAND))
    table, _ = parse_items(parse_otsl(SHIFTED))
    merged = merge_items(band, table)
    assert [i.line_number.value for i in merged] == [1, 2, 3]
    assert [i.line_number.status for i in merged] == ["check", "ok", "ok"]


def test_consignment_names_are_read_in_order_whatever_row_they_landed_on() -> None:
    # Two consignments in row 1's cell, every later name one row late, the last row's name in
    # its amount column, a caption ("Mr. No") missing and "0 395" in one cell.
    table = (
        "<fcel>Sr. No.<fcel>Dlvr. Dt<fcel>Lr. No<fcel>Inv. No<fcel>Chrg. Wt<fcel>Pkgs Consonor/ Consignee"
        "<fcel>FreightHamali Dlvr.chrg Amount<lcel><lcel><lcel><nl>"
        "<fcel>1<fcel>32763<fcel>24-07-2026<fcel>BWD-12074<fcel>518<fcel>50<fcel>1 LABISA\\nESSA GARMENTS PVT LTD\\n2 CITY GIRL\\nESSA GARMENTS PVT LTD<fcel>395<fcel>0<fcel>0 395<nl>"
        "<fcel>2<fcel>32768<fcel>24-07-2026<fcel>MBJ-17525<fcel>758/26-27<fcel>100<fcel>1 B FASHION STUDIO LLP\\nESSA GARMENTS PVT LTD<fcel>890<fcel>0<fcel>0 890<nl>"
        "<fcel>3<fcel>32766<fcel>24-07-2026<fcel>SRT-255741✓<fcel>1/983<fcel>50<fcel>455<fcel>0<fcel>0<fcel>455<nl>"
        "<fcel>780.000<lcel><lcel><lcel><lcel><fcel>14<fcel>1740<fcel>1740<lcel><lcel><nl>"
    )
    items, warnings = freight_items(parse_otsl(table))  # type: ignore[misc]
    assert rows(items) == [
        (1, "LABISA (LR BWD-12074, Inv 518, Chrg Wt 50)", Decimal(1), Decimal(395)),
        (2, "CITY GIRL (LR MBJ-17525, Inv 758/26-27, Chrg Wt 100)", Decimal(2), Decimal(890)),
        (3, "B FASHION STUDIO LLP (LR SRT-255741, Inv 1/983, Chrg Wt 50)", Decimal(1), Decimal(455)),
    ]
    assert warnings == []  # every amount on its own row, a name for every row


def test_only_the_repeated_consignee_is_stripped_from_names() -> None:
    from app.services.invoice_extraction import _strip_suffix

    suffix = "ESSA GARMENTS PVT LTD".split()
    assert _strip_suffix("SONY PVT LTD ESSA GARMENTS PVT LTD", suffix) == "SONY PVT LTD"
    assert _strip_suffix("KRISHNA LEELA ESSA GARMENTS PVT LTD LTD", suffix) == "KRISHNA LEELA"
    assert _strip_suffix("KRISHA ESSA GARMENTS PVT", suffix) == "KRISHA"


def test_goods_invoices_are_not_freight_bills() -> None:
    goods = "<fcel>Sl No.<fcel>Description of Goods<fcel>HSN/SAC<fcel>Quantity<fcel>Rate<fcel>Amount<nl>"
    assert freight_items(parse_otsl(goods + "<fcel>1<fcel>Shirt<fcel>6205<fcel>2 pcs<fcel>100.00<fcel>200.00<nl>")) is None
