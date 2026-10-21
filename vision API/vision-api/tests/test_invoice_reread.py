"""Zoomed readings: locating blocks, zoom crops, the merge rules and the extractor wiring.

No model runs here: the OCR model and the locator are fakes, like in the other invoice tests.
"""

import datetime as dt
import io
from decimal import Decimal
from typing import Any

import pytest
from PIL import Image

from app.models.base import ModelUnavailableError
from app.schemas.invoice import Extracted, Invoice
from app.services.invoice_bands import (
    FIELD_PAD,
    OCR_MAX_PIXELS,
    Box,
    TableLocation,
    band_images,
    parse_field_box,
    parse_regions,
    zoom_crop,
)
from app.services.invoice_extraction import (
    TABLE_TASK,
    REREAD_BAND_FRACTION,
    InvoiceExtractor,
    ZoomOptions,
    gstin_check_char,
    split_bands,
    split_raw_text,
    split_zoom,
)
from app.services.invoice_reread import (
    REGION,
    REREAD,
    REREAD_FIELDS,
    apply_zoom,
    region_values,
    reread_candidates,
    reread_targets,
)
from test_invoice_extraction import BUYER_GSTIN, HEADER, SELLER_GSTIN, FakePaddle, page_png

pytestmark = pytest.mark.anyio

# A seller GSTIN with its last character misread: the checksum no longer matches.
MISREAD_GSTIN = SELLER_GSTIN[:-1] + ("X" if SELLER_GSTIN[-1] != "X" else "Y")
OTHER_GSTIN = "33ABCDE1234F1Z" + gstin_check_char("33ABCDE1234F1Z")


def invoice(**fields: Extracted[Any]) -> Invoice:
    base: dict[str, Extracted[Any]] = {
        "invoice_number": Extracted.found("AB/7/26-27", "Invoice No. AB/7/26-27"),
        "invoice_date": Extracted.found(dt.date(2026, 8, 3), "03-Aug-26"),
        "vendor_name": Extracted.found("ACME TEXTILES A/C - (26-27)"),
        "vendor_gstin": Extracted.found(SELLER_GSTIN),
        "buyer_name": Extracted.found("NOVA FASHIONS PRIVATE LIMITED"),
        "buyer_gstin": Extracted.found(BUYER_GSTIN),
        "subtotal": Extracted.found(Decimal("1000.00")),
        "cgst": Extracted.found(Decimal("25.00")),
        "sgst": Extracted.found(Decimal("25.00")),
        "total_amount": Extracted.found(Decimal("1050.00")),
    }
    return Invoice(**{**base, **fields})


# ===========================================================================
# Locating and cropping
# ===========================================================================


def test_regions_keep_plausible_boxes_only() -> None:
    answer = (
        'Sure: [{"label": "seller", "bbox_2d": [20, 10, 500, 200]}, '
        '{"label": "buyer", "bbox_2d": [500, 200, 400, 300]}, '      # inverted
        '{"label": "totals", "bbox_2d": [0, 0, 1000, 1000]}, '       # the whole page
        '{"label": "invoice_details", "bbox_2d": [600, 10, 980, 90]}, '
        '{"label": "logo", "bbox_2d": [0, 0, 100, 100]}]'            # not asked for
    )
    regions = parse_regions(answer)
    assert set(regions) == {"seller", "invoice_details"}
    assert regions["seller"] == Box(0.02, 0.01, 0.5, 0.2)
    assert parse_regions("no boxes here") == {} and parse_regions("[not json]") == {}


def test_regions_also_parse_the_object_form_qwen_sometimes_returns() -> None:
    # Qwen3-VL-2B's real answer on a digital invoice: an object keyed by label, in a code fence.
    answer = """```json
{
  "seller": {"label": "seller", "bbox_2d": [26, 117, 242, 340]},
  "buyer": {"label": "buyer", "bbox_2d": [500, 208, 767, 340]},
  "invoice_details": {"label": "invoice_details", "bbox_2d": [267, 62, 962, 148]},
  "totals": {"label": "totals", "bbox_2d": [26, 352, 962, 800]}
}
```"""
    regions = parse_regions(answer)
    assert set(regions) == {"seller", "buyer", "invoice_details", "totals"}
    assert regions["buyer"] == Box(0.5, 0.208, 0.767, 0.34)
    assert parse_regions('{"seller": [20, 10, 500, 200]}') == {"seller": Box(0.02, 0.01, 0.5, 0.2)}
    assert parse_field_box('{"label": "value", "bbox_2d": [100, 50, 300, 80]}') == Box(0.1, 0.05, 0.3, 0.08)


def test_field_box_is_small_and_valid() -> None:
    assert parse_field_box('[{"label": "value", "bbox_2d": [100, 50, 300, 80]}]') == Box(0.1, 0.05, 0.3, 0.08)
    assert parse_field_box('[{"label": "value", "bbox_2d": [0, 0, 900, 900]}]') is None  # not one value
    assert parse_field_box('[{"label": "value", "bbox_2d": [100, 50, 105, 52]}]') is None  # a point


def test_zoom_crop_enlarges_small_crops_up_to_the_cap() -> None:
    image, scale = zoom_crop(page_png((400, 600)), Box(0.1, 0.1, 0.4, 0.2), max_scale=2.0)
    size = Image.open(io.BytesIO(image)).size
    # 0.1-0.4 plus 3% margins of 400 px = 144 px wide; 0.1-0.2 plus 2% of 600 px = 84 px high
    assert scale == 2.0 and size == (288, 168)
    _, wider = zoom_crop(page_png((400, 600)), Box(0.1, 0.1, 0.4, 0.2), max_scale=2.0, pad=FIELD_PAD)
    assert wider == 2.0


def test_zoom_crop_never_exceeds_the_ocr_pixel_budget() -> None:
    image, scale = zoom_crop(page_png((1200, 1600)), Box(0.0, 0.0, 0.5, 0.3), max_scale=3.0)
    width, height = Image.open(io.BytesIO(image)).size
    assert 1.05 <= scale < 3.0 and width * height <= OCR_MAX_PIXELS * 1.01
    _, whole = zoom_crop(page_png((2000, 2600)), Box(0.0, 0.0, 0.6, 0.5), max_scale=3.0)
    assert whole == 1.0  # already above the budget: the OCR model would shrink it anyway


# ===========================================================================
# Reading values
# ===========================================================================


def test_region_values_per_block() -> None:
    assert region_values("seller", HEADER) == {"vendor_name": "ACME TEXTILES A/C - (26-27)", "vendor_gstin": SELLER_GSTIN}
    assert region_values("buyer", HEADER) == {"buyer_name": "NOVA FASHIONS PRIVATE LIMITED", "buyer_gstin": BUYER_GSTIN}
    assert region_values("invoice_details", "Invoice No. Dated\nAB/7/26-27 03-Aug-26\nDated 03-Aug-26") == {
        "invoice_number": "AB/7/26-27", "invoice_date": dt.date(2026, 8, 3)}
    totals = "<fcel>Taxable Amount<fcel>1,000.00<nl><fcel>Grand Total<fcel>1,050.00<nl>"
    assert region_values("totals", totals) == {"subtotal": Decimal("1000.00"), "total_amount": Decimal("1050.00")}
    # A misread GSTIN in a zoomed block is not a usable second opinion.
    assert "vendor_gstin" not in region_values("seller", HEADER.replace(SELLER_GSTIN, MISREAD_GSTIN))


def test_reread_candidates_per_field() -> None:
    assert reread_candidates("vendor_gstin", f"GSTIN/UIN: {SELLER_GSTIN[:5]} {SELLER_GSTIN[5:]}") == [SELLER_GSTIN]
    assert reread_candidates("invoice_number", "Invoice No.\nAB/7/26-27") == ["AB/7/26-27"]
    assert reread_candidates("invoice_number", "AB/7/26-27") == ["AB/7/26-27"]  # tight crop: the value alone
    assert reread_candidates("invoice_number", "331234567890") == []  # e-way bill number, not an invoice number
    assert reread_candidates("invoice_date", "Dated\n03-Aug-26") == [dt.date(2026, 8, 3)]
    assert reread_candidates("total_amount", "Grand Total ₹ 1,050.00") == [Decimal("1050.00")]


# ===========================================================================
# Merge rules
# ===========================================================================


def test_agreement_adds_a_note_but_no_confirmation_bonus() -> None:
    merged, warnings = apply_zoom(invoice(), [(REGION, "seller", HEADER)], "")
    assert merged.vendor_gstin.status == "ok"
    assert merged.vendor_gstin.note == "matches the zoomed seller crop"
    assert "confirmed by" not in (merged.vendor_gstin.note or "") and not warnings


def test_a_gstin_failing_its_checksum_is_replaced_by_a_valid_zoomed_reading() -> None:
    bad = Extracted.check(MISREAD_GSTIN, "GSTIN checksum does not match; verify every character", MISREAD_GSTIN)
    merged, warnings = apply_zoom(invoice(vendor_gstin=bad), [(REGION, "seller", HEADER)], "")
    assert (merged.vendor_gstin.value, merged.vendor_gstin.status) == (SELLER_GSTIN, "check")
    assert f"first reading {MISREAD_GSTIN} did not pass validation" in (merged.vendor_gstin.note or "")
    assert warnings == ["zoomed crops changed vendor_gstin; marked 'check'"]


def test_a_valid_value_is_kept_when_the_zoomed_reading_differs() -> None:
    seller = HEADER.replace(SELLER_GSTIN, OTHER_GSTIN)
    merged, warnings = apply_zoom(invoice(), [(REGION, "seller", seller)], "")
    assert (merged.vendor_gstin.value, merged.vendor_gstin.status) == (SELLER_GSTIN, "ok")
    assert f"reads {OTHER_GSTIN}" in (merged.vendor_gstin.note or "")
    assert warnings == ["zoomed crops read vendor_gstin differently; see the field notes"]


def test_the_other_partys_gstin_is_ignored() -> None:
    # A buyer box that caught the seller block: its GSTIN is already the seller's.
    merged, _ = apply_zoom(invoice(buyer_gstin=Extracted()), [(REGION, "buyer", f"GSTIN/UIN: {SELLER_GSTIN}")], "")
    assert merged.buyer_gstin.value is None


def test_totals_that_do_not_add_up_are_fixed_by_the_totals_crop() -> None:
    misread = invoice(total_amount=Extracted.found(Decimal("1650.00")))
    merged, _ = apply_zoom(misread, [(REGION, "totals", "<fcel>Grand Total<fcel>1,050.00<nl>")], "")
    assert (merged.total_amount.value, merged.total_amount.status) == (Decimal("1050.00"), "check")
    # A totals crop whose amount does not add up either changes nothing.
    merged, _ = apply_zoom(misread, [(REGION, "totals", "<fcel>Grand Total<fcel>1,750.00<nl>")], "")
    assert merged.total_amount.value == Decimal("1650.00")


def test_an_empty_field_is_filled_but_flagged() -> None:
    merged, warnings = apply_zoom(invoice(invoice_date=Extracted()), [(REGION, "invoice_details", "Dated : 03-Aug-26")], "")
    assert (merged.invoice_date.value, merged.invoice_date.status) == (dt.date(2026, 8, 3), "check")
    assert merged.invoice_date.note == "read only in the zoomed invoice details crop; verify"
    assert warnings == ["zoomed crops changed invoice_date; marked 'check'"]


def test_invoice_number_takes_two_out_of_three_readings() -> None:
    details = "Invoice No. Dated\nAB/1/26-27 03-Aug-26"  # the block crop misreads 7 as 1
    merged, _ = apply_zoom(invoice(), [(REGION, "invoice_details", details)], "")
    assert (merged.invoice_number.value, merged.invoice_number.status) == ("AB/7/26-27", "check")

    # The re-read agrees with the first reading: the block crop was wrong, the flag goes.
    merged, _ = apply_zoom(invoice(), [(REGION, "invoice_details", details), (REREAD, "invoice_number", "AB/7/26-27")], "")
    assert (merged.invoice_number.value, merged.invoice_number.status) == ("AB/7/26-27", "ok")
    assert merged.invoice_number.note == "matches the zoomed re-read"

    # The re-read agrees with the block crop: two zoomed readings beat the first one.
    merged, _ = apply_zoom(invoice(), [(REGION, "invoice_details", details), (REREAD, "invoice_number", "AB/1/26-27")], "")
    assert (merged.invoice_number.value, merged.invoice_number.status) == ("AB/1/26-27", "check")
    assert "both read AB/1/26-27 (first reading AB/7/26-27)" in (merged.invoice_number.note or "")


def test_names_only_get_a_note_when_readings_differ() -> None:
    seller = HEADER.replace("ACME TEXTILES A/C - (26-27)", "ZENITH MILLS")
    merged, _ = apply_zoom(invoice(), [(REGION, "seller", seller)], "")
    assert (merged.vendor_name.value, merged.vendor_name.status) == ("ACME TEXTILES A/C - (26-27)", "ok")
    assert "reads ZENITH MILLS" in (merged.vendor_name.note or "")


def test_a_value_found_only_by_the_reasoning_model_gives_way_to_the_zoomed_crop() -> None:
    guess = Extracted.check("NOVA FASHIONS SLIP", "found only by tiny-llm; verify", "NOVA FASHIONS SLIP")
    merged, warnings = apply_zoom(invoice(vendor_name=guess), [(REGION, "seller", HEADER)], "")
    assert (merged.vendor_name.value, merged.vendor_name.status) == ("ACME TEXTILES A/C - (26-27)", "check")
    assert "was found only by the reasoning model" in (merged.vendor_name.note or "")
    assert warnings == ["zoomed crops changed vendor_name; marked 'check'"]


def test_a_non_gstin_from_the_reasoning_model_is_replaced_by_a_valid_zoomed_reading() -> None:
    guess = Extracted.check("IGCHENNAI@GMAIL.COM", "found only by tiny-llm; verify", "igchennai@gmail.com")
    merged, _ = apply_zoom(invoice(vendor_gstin=guess), [(REGION, "seller", HEADER)], "")
    assert (merged.vendor_gstin.value, merged.vendor_gstin.status) == (SELLER_GSTIN, "check")


def test_reread_targets_flagged_failing_and_missing_fields_in_priority_order() -> None:
    flagged = invoice(
        invoice_number=Extracted.check("AB/7/26-27", "Qwen read AB/9/26-27"),
        buyer_name=Extracted(),
        total_amount=Extracted.found(Decimal("1650.00")),  # fails the grand total check
    )
    assert reread_targets(flagged, "", 10) == ["invoice_number", "total_amount", "subtotal", "buyer_name"]
    assert reread_targets(flagged, "", 2) == ["invoice_number", "total_amount"]
    assert reread_targets(flagged, "", 0) == [] and reread_targets(invoice(), "", 4) == []


# ===========================================================================
# In the extractor
# ===========================================================================


class ZoomMapper:
    """A reasoning model that disagrees about the invoice number and can locate boxes."""

    name = "Qwen-test"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.fields_asked: list[str] = []

    async def read_fields(self, document: str) -> tuple[dict[str, str | None], int, int]:
        return {"invoice_number": "AB/9/26-27"}, 10, 5

    async def locate_regions(self, page: bytes) -> tuple[dict[str, Box], int, int]:
        if self.fail:
            raise ModelUnavailableError("reasoning model not reachable")
        return {"seller": Box(0.0, 0.1, 0.5, 0.3), "buyer": Box(0.0, 0.3, 0.5, 0.45),
                "invoice_details": Box(0.5, 0.05, 1.0, 0.12), "totals": Box(0.5, 0.8, 1.0, 0.9)}, 20, 7

    async def locate_field(self, page: bytes, what: str) -> tuple[Box | None, int, int]:
        self.fields_asked.append(what)
        return Box(0.5, 0.05, 0.8, 0.08), 20, 3


async def test_extractor_reads_zoomed_blocks_and_rereads_flagged_fields() -> None:
    model, mapper = FakePaddle(), ZoomMapper()
    result = await InvoiceExtractor(model, mapper, ZoomOptions(targeted_blocks=False)).extract([page_png()])  # type: ignore[arg-type]

    zoomed = split_zoom(result.raw_text)
    assert [(kind, label) for kind, label, _ in zoomed] == [
        ("region OCR", "seller"), ("region OCR", "buyer"), ("region OCR", "invoice_details"),
        ("region OCR", "totals"), ("re-read", "invoice_number"),
    ]
    assert "--- region OCR (page 1, seller, 2.0x) ---" in result.raw_text
    assert mapper.fields_asked == [REREAD_FIELDS["invoice_number"]]  # the only flagged field
    assert TABLE_TASK in [call[0] for call in model.calls[-2:]]  # the totals block is read as a table
    # The model's disagreement keeps the number flagged; both zoomed readings side with the rules.
    number = result.invoice.invoice_number
    assert (number.value, number.status) == ("AB/7/26-27", "check")
    assert "matches the zoomed invoice details crop" in (number.note or "")
    assert "matches the zoomed re-read" in (number.note or "")
    assert "matches the zoomed seller crop" in (result.invoice.vendor_gstin.note or "")
    # Zoomed sections do not leak into the header / table / footer text used for re-mapping.
    header, tables, footer = split_raw_text(result.raw_text)
    assert header == HEADER.strip() and len(tables) == 1 and footer.startswith("Declaration")


async def test_extractor_skips_zoom_when_the_locator_fails_or_time_is_short() -> None:
    result = await InvoiceExtractor(FakePaddle(), ZoomMapper(fail=True)).extract([page_png()])  # type: ignore[arg-type]
    assert any("blocks could not be located" in w for w in result.warnings)

    class FindsNothing(ZoomMapper):
        async def locate_regions(self, page: bytes) -> tuple[dict[str, Box], int, int]:
            return {}, 20, 7

    result = await InvoiceExtractor(FakePaddle(), FindsNothing()).extract([page_png()])  # type: ignore[arg-type]
    assert "the reasoning model found no invoice blocks; zoomed block crops skipped" in result.warnings
    assert result.invoice.buyer_gstin.value == BUYER_GSTIN

    quick = ZoomOptions(budget_seconds=1e-9)
    result = await InvoiceExtractor(FakePaddle(), ZoomMapper(), quick).extract([page_png()])  # type: ignore[arg-type]
    assert split_zoom(result.raw_text) == []
    assert any("block crops skipped" in w for w in result.warnings)
    assert any("re-reads stopped" in w for w in result.warnings)


async def test_zoom_can_be_switched_off() -> None:
    mapper = ZoomMapper()
    off = ZoomOptions(region_crops=False, reread_max_fields=0)
    result = await InvoiceExtractor(FakePaddle(), mapper, off).extract([page_png()])  # type: ignore[arg-type]
    assert split_zoom(result.raw_text) == [] and mapper.fields_asked == []


def test_saved_zoomed_readings_split_apart_from_the_other_sections() -> None:
    raw = (
        f"--- header OCR (page 1, top 35%) ---\n{HEADER}\n\n"
        f"--- region OCR (page 1, seller, 1.4x) ---\n{HEADER}\n\n"
        "--- re-read (page 1, invoice_number, 2.0x) ---\nAB/7/26-27\n\n"
        "--- footer OCR (page 1, bottom 15%) ---\nfor ACME"
    )
    assert split_zoom(raw) == [(REGION, "seller", HEADER.rstrip("\n")), (REREAD, "invoice_number", "AB/7/26-27")]
    header, tables, footer = split_raw_text(raw)
    assert header == HEADER.rstrip("\n") and tables == [] and footer == "for ACME"


# ===========================================================================
# Item rows read again in shorter bands
# ===========================================================================

ITEM_CAPTIONS = "<fcel>Sl No.<fcel>Description of Goods<fcel>HSN/SAC<fcel>Quantity<fcel>Rate<fcel>per<fcel>Amount<nl>"
MISREAD_ROW = ITEM_CAPTIONS + "<fcel>1<fcel>Kurti<fcel>6211<fcel>8 pcs<fcel>100.00<fcel>pcs<fcel>700.00<nl>"  # 8 x 100 != 700
CLEAR_ROW = ITEM_CAPTIONS + "<fcel>1<fcel>Kurti<fcel>6211<fcel>7 pcs<fcel>100.00<fcel>pcs<fcel>700.00<nl>"
TALL_PAGE = page_png((600, 2000))


class TablePaddle(FakePaddle):
    """Table readings are misread for the first ``misread`` calls, clear afterwards."""

    def __init__(self, misread: int) -> None:
        super().__init__()
        self.misread = misread
        self.table_calls = 0

    async def analyze(self, images: list[bytes], prompt: str, options: dict[str, Any] | None = None) -> Any:
        if prompt != TABLE_TASK:
            return await super().analyze(images, prompt, options)
        self.table_calls += 1
        text = MISREAD_ROW if self.table_calls <= self.misread else CLEAR_ROW
        return {"text": text, "input_tokens": 100, "output_tokens": 50}


class TableMapper:
    name = "Qwen-test"
    location = TableLocation(caption_top=0.10, caption_bottom=0.12, total_bottom=None)

    async def read_fields(self, document: str) -> tuple[dict[str, str | None], int, int]:
        return {}, 10, 5

    async def locate_table(self, page: bytes) -> tuple[TableLocation, int, int]:
        return self.location, 20, 7


async def test_item_rows_failing_their_arithmetic_are_read_again_in_shorter_bands() -> None:
    first_pass = 1 + len(band_images(TALL_PAGE, TableMapper.location))  # whole page + bands
    model = TablePaddle(misread=first_pass)
    result = await InvoiceExtractor(model, TableMapper()).extract([TALL_PAGE])  # type: ignore[arg-type]

    reread = len(band_images(TALL_PAGE, TableMapper.location, REREAD_BAND_FRACTION))
    assert reread > first_pass - 1  # shorter bands: more of them
    assert model.table_calls == first_pass + reread
    item = result.invoice.items[0]
    assert (item.quantity.value, item.taxable_value.value) == (7, Decimal("700.00"))  # the reading that adds up
    assert "item rows read again in shorter bands; the item checks now pass" in result.warnings
    assert "--- items band (page 1, re-read band 1) ---" in result.raw_text
    assert len(split_bands(result.raw_text)) == first_pass - 1 + reread
    assert len(split_bands(result.raw_text, reread=False)) == first_pass - 1  # what --no-zoom compares with


async def test_items_are_not_read_again_when_they_add_up_or_it_is_switched_off() -> None:
    model = TablePaddle(misread=0)
    await InvoiceExtractor(model, TableMapper()).extract([TALL_PAGE])  # type: ignore[arg-type]
    assert model.table_calls == 1 + len(band_images(TALL_PAGE, TableMapper.location))

    model = TablePaddle(misread=100)
    off = ZoomOptions(reread_items=False)
    result = await InvoiceExtractor(model, TableMapper(), off).extract([TALL_PAGE])  # type: ignore[arg-type]
    assert model.table_calls == 1 + len(band_images(TALL_PAGE, TableMapper.location))
    assert result.invoice.items[0].quantity.value == 8  # still misread: flagged by validation instead
