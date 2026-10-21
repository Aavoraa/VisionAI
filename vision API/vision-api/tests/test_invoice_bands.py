"""Phase 6: band reading of the item table and the layout rules added for non-Tally invoices."""

import io
from decimal import Decimal

import numpy as np
from PIL import Image, ImageDraw

from app.schemas.invoice import Extracted, LineItem
from app.services.invoice_bands import (
    TableLocation,
    band_images,
    deskew,
    parse_location,
    plan_bands,
    skew_angle,
)
from app.services.invoice_extraction import (
    map_invoice,
    merge_items,
    parse_items,
    parse_otsl,
    parse_parties,
    split_bands,
    split_raw_text,
    value_for_label,
)

BUYER_GSTIN = "33AADCE6591N1Z7"
SELLER_GSTIN = "27AIGPG5972P1ZK"


def lined_page(lines: int = 40, angle: float = 0.0) -> Image.Image:
    """A white page with ``lines`` lines of dark word-like blocks, optionally rotated."""
    image = Image.new("L", (600, 900), 255)
    draw = ImageDraw.Draw(image)
    for n in range(lines):
        y = 40 + n * 20
        for x in range(40, 560, 60):
            draw.rectangle((x, y, x + 35, y + 8), fill=0)
    if angle:
        image = image.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=255)
    return image


def png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


# --- Image side ----------------------------------------------------------------


def test_skew_is_measured_and_undone() -> None:
    assert abs(skew_angle(lined_page(angle=2.0)) + 2.0) <= 0.3
    fixed, angle = deskew(png(lined_page(angle=-1.5)))
    assert abs(angle - 1.5) <= 0.3
    assert abs(skew_angle(Image.open(io.BytesIO(fixed)))) <= 0.3


def test_level_and_blank_pages_are_left_alone() -> None:
    level = png(lined_page())
    assert deskew(level) == (level, 0.0)
    blank = png(Image.new("RGB", (200, 300), "white"))
    assert deskew(blank) == (blank, 0.0)


def test_bands_overlap_and_cut_between_lines() -> None:
    gray = np.asarray(lined_page())
    bands = plan_bands(gray, 40, 840)
    assert len(bands) >= 4
    assert bands[0][0] == 40 and bands[-1][1] == 840
    for (_, end), (start, _) in zip(bands, bands[1:], strict=False):
        assert start < end  # neighbours overlap
    for _, end in bands[:-1]:
        assert (end - 40) % 20 >= 8  # never inside a text bar (bars occupy rows 0-8 of each 20)


def test_every_band_carries_the_caption_row() -> None:
    page = png(lined_page())
    images = band_images(page, TableLocation(caption_top=0.04, caption_bottom=0.06, total_bottom=None))
    assert len(images) >= 3
    heights = [Image.open(io.BytesIO(b)).height for b in images]
    assert all(h < 900 for h in heights)
    assert band_images(page, TableLocation(0.97, 0.98, None)) == []  # no rows below the caption


def test_locator_answers_are_checked() -> None:
    answer = '```json\n[{"label": "header_row", "bbox_2d": [44, 365, 877, 385]}, {"label": "total_row", "bbox_2d": [44, 705, 877, 725]}]\n```'
    assert parse_location(answer) == TableLocation(0.365, 0.385, 0.725)
    upside_down = '[{"label": "header_row", "bbox_2d": [0, 500, 900, 520]}, {"label": "total_row", "bbox_2d": [0, 100, 900, 120]}]'
    assert parse_location(upside_down) == TableLocation(0.5, 0.52, None)  # total above the caption: ignored
    assert parse_location("no table here") is None
    assert parse_location('[{"label": "header_row", "bbox_2d": [0, 100, 900, 600]}]') is None  # not a row


# --- Rules ----------------------------------------------------------------------


def item(number: int, quantity: str | None, rate: str | None, amount: str, description: str | None = None) -> LineItem:
    def found(value: object) -> Extracted:  # type: ignore[type-arg]
        return Extracted.found(value, str(value)) if value is not None else Extracted()

    return LineItem(
        line_number=found(number), description=found(description),
        quantity=found(Decimal(quantity) if quantity else None), rate=found(Decimal(rate) if rate else None),
        taxable_value=found(Decimal(amount)),
    )


def test_merge_keeps_the_arithmetically_consistent_reading() -> None:
    band = [item(1, "36", "150", "5400"), item(2, "38", "160", "6080")]
    whole_page = [item(1, "36", "150", "5400", "Dupatta"), item(2, "3", "160", "6080", "Saheli"), item(3, "14", "210", "2940")]
    merged = merge_items(band, whole_page)
    assert [i.line_number.value for i in merged] == [1, 2, 3]
    assert merged[1].quantity.value == 38  # 38 x 160 = 6080; the whole-page "3" is a misread
    assert merged[0].description.value == "Dupatta"  # gap filled from the other reading of the row
    assert merged[1].description.value == "Saheli"


def test_line_number_in_its_own_cell() -> None:
    grid = parse_otsl(
        "<fcel>SI No.<fcel>Description of Goods<fcel>HSN/SAC<fcel>Quantity<fcel>Rate<fcel>per<fcel>Amount<nl>"
        "<fcel>1<fcel>Dupatta-540752\\nAngoori<fcel>54075290<fcel>36 PCS<fcel>150.00<fcel>PCS<fcel>5,400.00<nl>"
    )
    items, _ = parse_items(grid)
    assert [(i.line_number.value, i.description.value, i.quantity.value, i.taxable_value.value) for i in items] == [
        (1, "Dupatta-540752 Angoori", 36, Decimal("5400.00"))
    ]


def test_items_are_read_by_column_caption() -> None:
    grid = parse_otsl(
        "<fcel>Sr.<fcel>Design<fcel>Hsn<fcel>Description<fcel>Set<fcel>Size<fcel>Qty<fcel>Rate<fcel>Disc%<fcel>N.Rate<fcel>Amount<nl>"
        "<fcel>1<fcel>4313<fcel>620469<fcel>FROCK<fcel>1<fcel>16*22<fcel>4<fcel>1350<fcel>0.00<fcel>1350.00<fcel>5400.00<nl>"
        "<fcel>2<fcel>4437<fcel>620469<fcel>FROCK<fcel>2<fcel>16*22<fcel>8<fcel>1350<fcel>0.00<fcel>1350.00<fcel>10800.00<nl>"
        "<fcel>TOTAL<fcel>40980.00<nl><fcel>5407<fcel>x<fcel>1<fcel>y<fcel>1<fcel>1<fcel>3<fcel>1<fcel>1<fcel>1<fcel>9.00<nl>"
    )
    items, _ = parse_items(grid)
    assert [(i.line_number.value, i.description.value, i.hsn_sac.value, i.quantity.value, i.rate.value) for i in items] == [
        (1, "4313 FROCK", "620469", 4, 1350), (2, "4437 FROCK", "620469", 8, 1350)
    ]  # rows below "TOTAL" (tax summaries) are not items


def test_value_in_the_label_cell() -> None:
    grid = parse_otsl("<fcel>Invoice No. KDC/833/26-27 Delivery Note<fcel>Dated 15-Jul-26 Mode/Terms of Payment<nl>")
    assert value_for_label(grid, "invoice no") == ("KDC/833/26-27", "Invoice No. KDC/833/26-27 Delivery Note")
    assert value_for_label(grid, "dated")[0] == "15-Jul-26"  # type: ignore[index]


def test_invoice_number_and_date_in_free_text() -> None:
    header = f"CITY GIRL\nGST NO:- {SELLER_GSTIN}\nBilling Address :\nM/s. ESSA GARMENTS PVT LTD\nTIRUPPUR-641604\nSTATE : TAMIL NADU-33\nGST NO : {BUYER_GSTIN}\nInvoice No. : Dated\n758 / 26-27\nDATE : 16/07/2026\n"
    invoice, _ = map_invoice(header, ["<fcel>x<nl>"])
    assert invoice.invoice_number.value == "758/26-27"
    assert str(invoice.invoice_date.value) == "2026-07-16"
    assert (invoice.buyer_name.value, invoice.buyer_gstin.value) == ("M/s. ESSA GARMENTS PVT LTD", BUYER_GSTIN)
    assert invoice.buyer_address.value == "TIRUPPUR-641604"  # the STATE line is not address
    assert invoice.place_of_supply.value == "Tamil Nadu (33)"
    assert invoice.currency.value == "INR" and invoice.currency.status == "check"  # not printed, from the GSTIN


def test_to_caption_and_header_values_between() -> None:
    header = f"GOLDEN TRANSPORT SERVICE\nP.N. ROAD, TIRUPUR.\nGST.No, 24AHHPS3168D1ZD\nTo,\nESSA GARMENTS PVT LTD\nBill No. 1120\nBill Date 24-07-2026\nFrom TIRUPPUR\nTiruppur\n{BUYER_GSTIN}\n"
    parties = parse_parties(header)
    assert [(p.name, p.role) for p in parties] == [("GOLDEN TRANSPORT SERVICE", None), ("ESSA GARMENTS PVT LTD", "buyer")]
    assert parties[1].address == ["Tiruppur"]


def test_labelled_totals_and_derived_round_off() -> None:
    table = (
        "<fcel>Sr<fcel>Description<fcel>QTY<fcel>Rate<fcel>Taxable A<nl>"
        "<fcel>1<fcel>FROCK<fcel>12.00<fcel>575.00<fcel>6900.00<nl>"
        "<fcel>2<fcel>FROCK<fcel>10.00<fcel>500.00<fcel>5000.00<nl>"
        "<fcel>Total :<fcel>22.00<fcel>11900.00<nl>"
        "<fcel>IGST 5%<fcel>595.00<nl><fcel>Grand Total<fcel>12495.00<nl>"
    )
    invoice, _ = map_invoice("", [table])
    assert invoice.total_amount.value == Decimal("12495.00")  # "Grand Total", not the "Total :" row
    assert invoice.igst.value == Decimal("595.00")
    assert invoice.subtotal.value == Decimal("11900.00")  # items sum, printed on the invoice
    assert invoice.total_quantity.value == 22

    unreadable_round_off = table.replace("12495.00", "12495.50").replace("595.00", "595.20")
    invoice, _ = map_invoice("", [unreadable_round_off])
    assert invoice.round_off.value == Decimal("0.30") and invoice.round_off.status == "check"


def test_band_readings_are_saved_and_reused() -> None:
    raw = "--- header OCR (page 1, top 35%) ---\nH\n\n--- table (page 1) ---\nT\n\n--- items band (page 1, band 1) ---\nB1\n\n--- items band (page 1, band 2) ---\nB2\n\n--- footer OCR (page 1, bottom 15%) ---\nF"
    assert split_raw_text(raw) == ("H", ["T"], "F")
    assert split_bands(raw) == ["B1", "B2"]


def test_stacked_captions_pair_with_the_last_amounts() -> None:
    rows = (
        "<fcel>Sl No.<fcel>Description<fcel>HSN/SAC<fcel>Quantity<fcel>Rate<fcel>per<fcel>Amount<nl>"
        "<fcel>1 Suit<fcel>620419<fcel>15 pcs<fcel>1,000.00<fcel>pcs<fcel>15,000.00<nl>"
        "<fcel>2 Suit<fcel>620419<fcel>5 pcs<fcel>1,000.00<fcel>pcs<fcel>5,000.00<nl>"
        "<fcel>Less:<fcel>Discount on Sale IGST on Sale<fcel>(-)5 %<fcel>20,000.00 (-)1,000.00 950.00<nl>"
    )
    invoice, _ = map_invoice("", [rows])
    assert (invoice.discount.value, invoice.igst.value) == (Decimal("1000.00"), Decimal("950.00"))
    blank_round_off = "<fcel>IGST 5% Round Off.<fcel>2049.00<nl>"
    invoice, _ = map_invoice("", [blank_round_off])
    assert invoice.igst.value == Decimal("2049.00")


def test_round_off_sign_follows_the_total() -> None:
    table = "<fcel>Taxable Amount<fcel>81858.30<nl><fcel>IGST 5% ROUND OFF<fcel>4093.00 0.30<nl><fcel>Grand Total<fcel>85951.00<nl><fcel>Round Off<fcel>0.30<nl>"
    invoice, _ = map_invoice("", [table])
    assert invoice.round_off.value == Decimal("-0.30")
    assert invoice.igst.value == Decimal("4093.00")
    signed = "<fcel>Round Off<fcel>(-)0.25<nl>"
    assert map_invoice("", [signed])[0].round_off.value == Decimal("-0.25")


def test_caption_value_is_the_adjacent_cell() -> None:
    summary = "<fcel>HsnCode<fcel>Qty<fcel>Taxable Amt<fcel>GST %<fcel>IGST<fcel>Net Amount<fcel>IGST<fcel>1804.95<nl>"
    invoice, _ = map_invoice("", [summary])
    assert invoice.subtotal.value is None and invoice.total_amount.value is None  # 1804.95 is the IGST
    assert invoice.igst.value == Decimal("1804.95")
    quantity_first = "<fcel>x<fcel>Total<fcel>23 Pcs<fcel>49,224.00<nl>"
    assert map_invoice("", [quantity_first])[0].total_amount.value == Decimal("49224.00")


def test_side_by_side_bill_to_and_ship_to() -> None:
    table = (
        "<fcel>Bill to Party :<fcel>Ship to Party :<nl>"
        "<fcel>Name : ESSA GARMENTS PRIVATE LIMITED<fcel>Name : ESSA GARMENTS PRIVATE LIMITED<nl>"
        "<fcel>Address : 21, KANGAYAM ROAD, TIRUPUR-641604<nl>"
        f"<fcel>Mobile : GSTIN : {BUYER_GSTIN}<fcel>Mobile : GSTIN : {BUYER_GSTIN}<nl>"
    )
    invoice, _ = map_invoice(f"SONY COLLECTION\nDADAR (W) MUMBAI-400 028\nGSTIN : {SELLER_GSTIN}\n", [table])
    assert invoice.vendor_name.value == "SONY COLLECTION"
    assert (invoice.buyer_name.value, invoice.buyer_gstin.value) == ("ESSA GARMENTS PRIVATE LIMITED", BUYER_GSTIN)
    assert invoice.buyer_address.value == "21, KANGAYAM ROAD, TIRUPUR-641604"


def test_contact_lines_do_not_end_the_buyer_block() -> None:
    header = (
        f"GSTIN : {SELLER_GSTIN}\nTAX INVOICE\nBuyer.: Bill No. : 224/2026\nBill Date : 16-Jul-26\n"
        f"Essa Garments Private Limited\n21, Venkatesaiya Colony,\nEmail : 021-4290577\nContact : {BUYER_GSTIN}\n"
    )
    buyer = parse_parties(header)[1]
    assert (buyer.name, buyer.role, buyer.address) == ("Essa Garments Private Limited", "buyer", ["21, Venkatesaiya Colony,"])


def test_gstin_look_alike_in_a_mixed_position() -> None:
    from app.services.invoice_extraction import find_gstin

    found = find_gstin("GSTIN NO:27AAAPG8395GIZJ")
    assert found is not None and (found.value, found.status) == ("27AAAPG8395G1ZJ", "ok")


def test_tally_decimal_comma_in_quantities() -> None:
    grid = parse_otsl(
        "<fcel>Sl No.<fcel>Description of Goods<fcel>HSN/SAC<fcel>Quantity<fcel>Rate<fcel>per<fcel>Amount<nl>"
        "<fcel>1<fcel>Kurti<fcel>62114210<fcel>7,0000 pcs<fcel>795.00<fcel>pcs<fcel>5,565.00<nl>"
    )
    assert parse_items(grid)[0][0].quantity.value == 7


def test_mixed_gst_rates_are_checked_per_line() -> None:
    from app.services.invoice_validation import validate_invoice

    table = (
        "<fcel>Sl No.<fcel>Description of Goods<fcel>HSN/SAC<fcel>GST Rate<fcel>Quantity<fcel>Rate<fcel>per<fcel>Amount<nl>"
        "<fcel>1 D.No 9881 M<fcel>621143<fcel>5 %<fcel>2 Pcs<fcel>2,000.00<fcel>Pcs<fcel>4,000.00<nl>"
        "<fcel>2 D.No 9881 XL<fcel>621143<fcel>18 %<fcel>2 Pcs<fcel>1,000.00<fcel>Pcs<fcel>2,000.00<nl>"
        "<fcel>Taxable Amount<fcel>6,000.00<nl><fcel>IGST<fcel>560.00<nl><fcel>Total<fcel>4 Pcs<fcel>6,560.00<nl>"
    )
    invoice, _ = map_invoice(f"A\nGSTIN : {SELLER_GSTIN}\n", [table])
    _, report = validate_invoice(invoice, table)
    check = next(c for c in report.checks if c.name == "Tax rate")
    assert check.status == "pass" and "Mixed rates" in check.message


def test_invoice_number_is_not_the_e_way_bill_number() -> None:
    header = f"B Fashion Studio LLP\nGSTIN/UIN: {SELLER_GSTIN}\nInvoice No. e-Way Bill No. Dated\nBF-2453/26-27 242245499788 17-Jul-26\n"
    invoice, _ = map_invoice(header, ["<fcel>x<nl>"])
    assert invoice.invoice_number.value == "BF-2453/26-27"


def test_phone_number_on_the_caption_line_is_not_the_name() -> None:
    header = f"SELLER CO\nGSTIN/UIN: {SELLER_GSTIN}\nBuyer (Bill to) : 0421\nEssa Garments Pvt Ltd\nTirupur\nGSTIN/UIN : {BUYER_GSTIN}\n"
    buyer = parse_parties(header)[1]
    assert (buyer.name, buyer.role) == ("Essa Garments Pvt Ltd", "buyer")
