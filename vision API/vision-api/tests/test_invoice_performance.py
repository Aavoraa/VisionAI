"""Performance work: stage profile, parallel call order, targeted block crops."""

from decimal import Decimal

import pytest

from app.schemas.invoice import Extracted
from app.services.invoice_extraction import InvoiceExtractor, ZoomOptions
from app.services.invoice_reread import blocks_needing_zoom
from test_invoice_extraction import BUYER_GSTIN, FakePaddle, page_png
from test_invoice_reread import invoice

pytestmark = pytest.mark.anyio


async def test_parallel_and_serial_orders_read_the_same_invoice() -> None:
    serial = await InvoiceExtractor(FakePaddle(), zoom=ZoomOptions(parallel=False)).extract([page_png(), page_png()])
    parallel_model = FakePaddle()
    parallel = await InvoiceExtractor(parallel_model, zoom=ZoomOptions(parallel=True)).extract([page_png(), page_png()])
    assert parallel.invoice == serial.invoice and parallel.invoice.buyer_gstin.value == BUYER_GSTIN
    assert parallel.details == serial.details


async def test_the_profile_counts_every_model_call_and_times_each_stage() -> None:
    model = FakePaddle()
    extractor = InvoiceExtractor(model)
    await extractor.extract([page_png()])
    profile = extractor.profile
    assert profile["calls"] == {"paddleocr": len(model.calls)}
    assert {"header_ocr", "table_ocr", "footer_ocr", "mapping", "validation"} <= profile["stages_ms"].keys()
    assert sum(n for key, n in profile["calls_by_stage"].items() if key.endswith(":paddleocr")) == len(model.calls)


def test_only_blocks_with_a_field_in_doubt_are_zoomed() -> None:
    # Inter-state (29 -> 27): IGST, so the absent CGST/SGST are not "missing".
    interstate = {"cgst": Extracted(), "sgst": Extracted(), "igst": Extracted.found(Decimal("50.00"))}
    assert blocks_needing_zoom(invoice(**interstate), "") == set()  # every field read and checking out
    assert blocks_needing_zoom(invoice(**interstate, buyer_gstin=Extracted()), "") == {"buyer"}
    assert blocks_needing_zoom(invoice(**interstate, invoice_number=Extracted.check("AB/7/26-27", "differs")), "") == {"invoice_details"}
    # A grand total that does not add up, or CGST/SGST charged across states, puts the totals in doubt.
    assert blocks_needing_zoom(invoice(**interstate, total_amount=Extracted.found(Decimal("1650.00"))), "") == {"totals"}
    assert blocks_needing_zoom(invoice(), "") == {"totals"}


async def test_reading_side_by_side_gives_the_same_invoice() -> None:
    serial = await InvoiceExtractor(FakePaddle(), zoom=ZoomOptions(parallel=False)).extract([page_png(), page_png()])
    extractor = InvoiceExtractor(FakePaddle(), zoom=ZoomOptions(parallel=True, ocr_concurrency=2))
    side = await extractor.extract([page_png(), page_png()])
    assert side.invoice == serial.invoice and side.details == serial.details
    assert {"table_ocr", "footer_ocr", "header_ocr"} <= extractor.profile["model_ms"].keys()  # no locator here: no bands
