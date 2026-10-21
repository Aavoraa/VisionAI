"""Phase 1 invoice extraction: parsing helpers, field mapping, documents, llama.cpp backend.

Fixtures imitate PaddleOCR-VL output (header OCR text and OTSL table markup) for a made-up
GST invoice; the GSTINs are fake but have valid checksums.
"""

import datetime as dt
import io
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from PIL import Image

from app.core.config import Settings
from app.models.base import ModelError, ModelOutput, ModelUnavailableError, VisionModel
from app.models.llama_cpp import PADDLEOCR_VL_TASKS, LlamaCppVisionModel
from app.models.local_transformers import MockVisionModel
from app.services.document_service import DocumentError, detect_document_type, load_pages
from app.services.invoice_extraction import (
    FOOTER_FRACTION,
    HEADER_FRACTION,
    OCR_TASK,
    TABLE_TASK,
    ExtractionUnavailableError,
    InvoiceExtractor,
    ZoomOptions,
    amounts_in,
    find_gstin,
    is_label,
    is_valid_gstin,
    map_invoice,
    parse_amount,
    parse_date,
    parse_otsl,
    parse_signature,
    split_raw_text,
    repair_gstin,
    strip_repetition,
    value_for_label,
)

pytestmark = pytest.mark.anyio

SELLER_GSTIN = "29ABCDE1234F1ZW"
BUYER_GSTIN = "27PQRST6789K1ZW"
CONSIGNEE_GSTIN = "24LMNOP4321Q1ZA"

HEADER = f"""Tax Invoice

Invoice No. Dated
AB/7/26-27 03-Aug-26

ACME TEXTILES A/C - (26-27)
12 MILL STREET
BENGALURU-560001
GSTIN/UIN: {SELLER_GSTIN}
State Name : Karnataka, Code : 29
E-Mail : accounts@acme.example

Buyer (Bill to)
NOVA FASHIONS PRIVATE LIMITED
4 MARKET ROAD
PUNE-411001
PH:9000000000
GSTIN/UIN : {BUYER_GSTIN}
State Name : Maharashtra, Code : 27
"""

FOOTER = """Declaration
We declare that this invoice shows the actual price of the goods.
for ACME TEXTILES PRIVATE LIMITED A/C - (26-27)
Authorised Signatory
This is a Computer Generated Invoice
"""

TABLE = (
    "<fcel>Tax Invoice<lcel><lcel><lcel><lcel><lcel><lcel><nl>"
    "<fcel>Invoice No.<lcel><lcel><fcel>Dated<lcel><lcel><lcel><nl>"
    "<fcel>AB/7/26-27<lcel><lcel><fcel>03-Aug-26<lcel><lcel><lcel><nl>"
    "<fcel>Delivery Note<lcel><lcel><fcel>Mode/Terms of Payment<lcel><lcel><lcel><nl>"
    "<fcel>DN-55<lcel><lcel><fcel>30 days<lcel><lcel><lcel><nl>"
    "<fcel>Description of Goods<fcel>HSN/SAC<fcel>Quantity<fcel>Rate<fcel>per<fcel>Disc. %<fcel>Amount<nl>"
    "<fcel>1 COTTON SHIRT<fcel>6205<fcel>2 pcs<fcel>500.00<fcel>pcs<fcel>10 %<fcel>900.00<nl>"
    "<fcel>2 DENIM - 32 JEANS<fcel>6203<fcel>1 pcs<fcel>1,200.00<fcel>pcs<fcel>10 %<fcel>1,080.00<nl>"
    "<fcel>IGST 5 % ROUND OFF<lcel><lcel><fcel>5 %<lcel><fcel>1,980.00 99.00 0.20<lcel><nl>"
    "<fcel>Total<lcel><fcel>3 pcs<ecel><ecel><fcel>₹ 2,079.20 E & OE<lcel><nl>"
    "<fcel>HSN/SAC<fcel>Taxable Value<fcel>IGST<lcel><fcel>Total Tax Amount<lcel><lcel><nl>"
    "<fcel>6205<fcel>900.00<fcel>5%<fcel>45.00<fcel>45.00<lcel><lcel><nl>"
    "<fcel>6203<fcel>1,080.00<fcel>5%<fcel>54.00<fcel>54.00<lcel><lcel><nl>"
    "<fcel>Total<fcel>1,980.00<ecel><fcel>99.00<fcel>99.00<lcel><lcel><nl>"
)


# ===========================================================================
# Parsing helpers
# ===========================================================================


def test_parse_otsl_keeps_column_positions() -> None:
    grid = parse_otsl("<fcel>Invoice No.<lcel><fcel>Dated<nl><fcel> AB/7 <ecel><fcel>03-Aug-26<nl>")
    assert grid == [["Invoice No.", None, "Dated"], ["AB/7", "", "03-Aug-26"]]
    assert value_for_label(grid, "invoice no") == ("AB/7", "Invoice No.")
    assert value_for_label(grid, "dated") == ("03-Aug-26", "Dated")
    assert value_for_label(grid, "place of supply") is None


def test_value_for_label_ignores_empty_boxes() -> None:
    # An empty box is followed by the next caption, which must not be taken as its value.
    grid = parse_otsl("<fcel>Delivery Note<fcel>Mode/Terms of Payment<nl><fcel>Reference No.<fcel>Other References<nl>")
    assert value_for_label(grid, "delivery note") is None
    assert value_for_label(grid, "mode/terms of payment") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [("₹ 49,969.00 E & OE", "49969.00"), ("1,17,589.30", "117589.30"), ("-0.04", "-0.04"), ("288", "288"), ("pcs", None), (None, None)],
)
def test_parse_amount(text: str | None, expected: str | None) -> None:
    assert parse_amount(text) == (Decimal(expected) if expected else None)


def test_amounts_in_skips_percentages() -> None:
    assert amounts_in("2.50 % 2.50 % 47,589.30 1,189.83 0.04") == [Decimal("47589.30"), Decimal("1189.83"), Decimal("0.04")]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("16-Jul-26", dt.date(2026, 7, 16)),
        ("16/07/2026", dt.date(2026, 7, 16)),
        ("16.07.26", dt.date(2026, 7, 16)),
        ("16 July 2026", dt.date(2026, 7, 16)),
        ("2026-07-16", dt.date(2026, 7, 16)),
        ("31/02/2026", None),
        ("soon", None),
    ],
)
def test_parse_date_day_first(text: str, expected: dt.date | None) -> None:
    assert parse_date(text) == expected


def test_gstin_checksum() -> None:
    assert is_valid_gstin(SELLER_GSTIN)
    assert not is_valid_gstin(SELLER_GSTIN[:-1] + "X")
    assert not is_valid_gstin("29ABCDE1234F1Z")


def test_gstin_lookalikes_are_repaired_only_when_the_checksum_confirms() -> None:
    misread = SELLER_GSTIN.replace("1234", "I23A")  # I for 1 and A for 4: only I->1 is a look-alike
    assert repair_gstin("29ABCDEI234F1ZW") == SELLER_GSTIN
    assert repair_gstin("29A8CDE1234F1ZW") == SELLER_GSTIN  # 8 read for B in a letter position
    fixed = find_gstin("GSTIN/UIN: 29ABCDEI234F1ZW")
    assert fixed is not None and fixed.value == SELLER_GSTIN and fixed.status == "ok" and fixed.note
    wrong = find_gstin(f"GSTIN: {misread}")
    assert wrong is not None and wrong.status == "check"
    assert find_gstin("PH: 9944841384") is None


@pytest.mark.parametrize(
    ("text", "label"),
    [("Invoice No.", True), ("Buyer's Order No.", True), ("State Name : Goa", True), ("Dated", True),
     ("Pan India Traders", False), ("Invoice Solutions Pvt Ltd", False), ("Datedness", False), ("", False)],
)
def test_is_label(text: str, label: bool) -> None:
    assert is_label(text) is label


def test_strip_repetition() -> None:
    looping = "Total 185 pcs " + "1,189.83 " * 50
    text, cut = strip_repetition(looping)
    assert cut and text == "Total 185 pcs"
    assert strip_repetition("a normal page of text " * 3) == ("a normal page of text " * 3, False)
    # Seen on a real invoice: one word per line until the token limit, cut mid-word.
    text, cut = strip_repetition("Dispatch Doc No.\nDestination\n" + "DISPATCH\n" * 60 + "DI")
    assert cut and text == "Dispatch Doc No.\nDestination"


# ===========================================================================
# Field mapping
# ===========================================================================


def test_map_invoice_fills_the_schema() -> None:
    invoice, warnings = map_invoice(HEADER, [TABLE])
    assert warnings == []
    assert invoice.invoice_number.value == "AB/7/26-27"
    assert invoice.invoice_date.value == dt.date(2026, 8, 3)
    assert invoice.delivery_note.value == "DN-55"
    assert invoice.payment_terms.value == "30 days"
    assert (invoice.vendor_name.value, invoice.vendor_gstin.value) == ("ACME TEXTILES A/C - (26-27)", SELLER_GSTIN)
    assert invoice.vendor_address.value == "12 MILL STREET, BENGALURU-560001"
    assert (invoice.buyer_name.value, invoice.buyer_gstin.value) == ("NOVA FASHIONS PRIVATE LIMITED", BUYER_GSTIN)
    assert invoice.buyer_address.value == "4 MARKET ROAD, PUNE-411001"  # phone line left out
    assert invoice.place_of_supply.value == "Maharashtra (27)"
    assert invoice.place_of_supply.status == "check"  # derived, not printed
    assert invoice.currency.value == "INR"

    assert [i.line_number.value for i in invoice.items] == [1, 2]
    first, second = invoice.items
    assert (first.description.value, first.hsn_sac.value, first.quantity.value, first.unit.value) == ("COTTON SHIRT", "6205", 2, "pcs")
    assert (first.rate.value, first.discount_percent.value, first.taxable_value.value) == (500, 10, 900)
    assert (second.description.value, second.rate.value, second.taxable_value.value) == ("DENIM - 32 JEANS", 1200, 1080)

    assert invoice.subtotal.value == Decimal("1980.00")
    assert invoice.igst.value == Decimal("99.00")
    assert invoice.cgst.status == invoice.sgst.status == "missing"
    assert invoice.round_off.value == Decimal("0.20")
    assert invoice.total_quantity.value == 3
    assert invoice.total_amount.value == Decimal("2079.20")
    assert invoice.discount.value is None and invoice.discount.status == "missing"


def test_signature_line_gives_the_full_vendor_name() -> None:
    assert parse_signature(FOOTER) == "ACME TEXTILES PRIVATE LIMITED A/C - (26-27)"
    assert parse_signature("(DUPLICATE FOR TRANSPORTER)\nfor the goods above") is None
    invoice, _ = map_invoice(HEADER, [TABLE], FOOTER)
    assert invoice.vendor_name.value == "ACME TEXTILES PRIVATE LIMITED A/C - (26-27)"
    assert invoice.vendor_gstin.value == SELLER_GSTIN  # the rest of the seller block still comes from the header
    assert invoice.vendor_address.value == "12 MILL STREET, BENGALURU-560001"  # header name matched the signature

    # A bracketed page caption above the block is a caption, not part of the address.
    captioned = HEADER.replace("ACME TEXTILES A/C", "(DUPLICATE FOR TRANSPORTER)\nACME TEXTILES A/C")
    invoice, _ = map_invoice(captioned, [TABLE], FOOTER)
    assert invoice.vendor_address.value == "12 MILL STREET, BENGALURU-560001"

    # Photo clipped the name: the header block starts with the street, which must stay in the address.
    clipped = HEADER.replace("ACME TEXTILES A/C - (26-27)\n", "")
    invoice, _ = map_invoice(clipped, [TABLE], FOOTER)
    assert invoice.vendor_name.value == "ACME TEXTILES PRIVATE LIMITED A/C - (26-27)"
    assert invoice.vendor_address.value == "12 MILL STREET, BENGALURU-560001"


def test_an_address_line_is_never_taken_as_the_name() -> None:
    # The photo clipped the seller's name: only the city line survived above the GSTIN.
    header = HEADER.replace("ACME TEXTILES A/C - (26-27)\n12 MILL STREET\n", "")
    invoice, _ = map_invoice(header, [TABLE])
    assert invoice.vendor_name.value is None and invoice.vendor_name.status == "missing"
    assert invoice.vendor_address.value == "BENGALURU-560001"


def test_missing_values_stay_null_instead_of_guessed() -> None:
    invoice, warnings = map_invoice("Tax Invoice", ["<fcel>Tax Invoice<nl>"])
    dumped = invoice.model_dump()
    assert all(field["value"] is None and field["status"] == "missing" for key, field in dumped.items() if key != "items")
    assert invoice.items == []
    assert warnings == ["no line items found"]


def test_consignee_block_is_not_mistaken_for_the_buyer() -> None:
    header = HEADER.replace(
        "Buyer (Bill to)",
        f"Consignee (Ship to)\nNOVA WAREHOUSE\nPLOT 9, SURAT\nGSTIN/UIN : {CONSIGNEE_GSTIN}\n\nBuyer (Bill to)",
    )
    invoice, _ = map_invoice(header, [TABLE])
    assert invoice.vendor_gstin.value == SELLER_GSTIN
    assert invoice.buyer_gstin.value == BUYER_GSTIN
    assert invoice.buyer_name.value == "NOVA FASHIONS PRIVATE LIMITED"


def test_misaligned_item_rows_are_flagged_not_trusted() -> None:
    # Seen on a real invoice: the first item's numbers landed on the "No." caption row and the
    # rows below were shifted by one. Nothing can be repaired safely, so every item is flagged.
    table = (
        "<fcel>Description of Goods<fcel>HSN/SAC<fcel>Quantity<fcel>Rate<fcel>Disc. %<fcel>Amount<nl>"
        "<fcel>No.<fcel>6205<fcel>2 pcs<fcel>500.00<fcel>10 %<fcel>900.00<nl>"
        "<fcel>1 COTTON SHIRT<fcel>6203<fcel>1 pcs<fcel>1,200.00<fcel>10 %<fcel>1,080.00<nl>"
        "<fcel>2 DENIM JEANS<ecel><ecel><ecel><ecel><ecel><nl>"
    )
    invoice, warnings = map_invoice(HEADER, [table])
    assert [i.description.status for i in invoice.items] == ["check"]
    assert any("misaligned" in w for w in warnings)
    clean, clean_warnings = map_invoice(HEADER, [TABLE])
    assert all(i.description.status == "ok" for i in clean.items) and clean_warnings == []


def test_party_caption_on_the_same_line_as_the_name() -> None:
    header = HEADER.replace("Buyer (Bill to)\nNOVA FASHIONS", "Buyer (Bill to) NOVA FASHIONS")
    invoice, _ = map_invoice(header, [TABLE])
    assert invoice.buyer_name.value == "NOVA FASHIONS PRIVATE LIMITED"
    assert invoice.buyer_address.value == "4 MARKET ROAD, PUNE-411001"


def test_tax_summary_total_row_is_cross_checked() -> None:
    summary = (
        "<fcel>HSN/SAC<fcel>Taxable Value<fcel>CGST<lcel><fcel>SGST/UTGST<lcel><fcel>Total Tax Amount<nl>"
        "<fcel>5407<fcel>47,589.30<fcel>2.50%<fcel>1,189.83<fcel>2.50%<fcel>1,189.83<fcel>2,379.66<nl>"
        # Seen in practice: the Total row shifted one column right.
        "<fcel>Total<fcel>47,589.30<fcel>1,189.83<fcel>1,189.83<fcel>1,189.83<fcel>2,379.66<fcel>2,379.66<nl>"
    )
    invoice, _ = map_invoice(HEADER, [summary])
    assert (invoice.subtotal.value, invoice.subtotal.status) == (Decimal("47589.30"), "ok")
    assert (invoice.cgst.value, invoice.cgst.status) == (Decimal("1189.83"), "ok")
    assert (invoice.sgst.value, invoice.sgst.status) == (Decimal("1189.83"), "check")
    assert invoice.sgst.note and "disagrees" in invoice.sgst.note


def test_items_are_collected_from_every_page() -> None:
    page_two = "<fcel>Description of Goods<fcel>Disc. %<nl><fcel>3 LINEN KURTA<fcel>6206<fcel>1 pcs<fcel>800.00<fcel>10 %<fcel>720.00<nl>"
    invoice, _ = map_invoice(HEADER, [TABLE, page_two])
    assert [i.line_number.value for i in invoice.items] == [1, 2, 3]


# ===========================================================================
# Orchestration
# ===========================================================================


class FakePaddle(VisionModel):
    name = "PaddleOCR-VL-test"
    tasks = PADDLEOCR_VL_TASKS

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[int, int], int]] = []

    async def analyze(self, images: list[bytes], prompt: str, options: dict[str, Any] | None = None) -> ModelOutput:
        size = Image.open(io.BytesIO(images[0])).size
        self.calls.append((prompt, size, (options or {})["max_tokens"]))
        if prompt == OCR_TASK:
            assert (options or {}).get("anti_repetition") is True
        if prompt == TABLE_TASK:
            text = TABLE + " 1,189.83" * 40  # table output loops at the end
        else:
            text = FOOTER if size[1] == round(300 * FOOTER_FRACTION) else HEADER
        return ModelOutput(text=text, input_tokens=100, output_tokens=50)


def page_png(size: tuple[int, int] = (200, 300)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


SERIAL = ZoomOptions(parallel=False)  # the call order these tests spell out


async def test_extractor_reads_the_header_and_every_page_table() -> None:
    model = FakePaddle()
    result = await InvoiceExtractor(model, zoom=SERIAL).extract([page_png(), page_png()])

    assert model.calls[0] == (OCR_TASK, (200, round(300 * HEADER_FRACTION)), 1024)  # top of page 1
    assert [c[0] for c in model.calls[1:3]] == [TABLE_TASK, TABLE_TASK]
    assert all(c[1] == (200, 300) for c in model.calls[1:3])
    assert model.calls[3] == (OCR_TASK, (200, round(300 * FOOTER_FRACTION)), 256)  # bottom of the last page
    assert result.pages == 2 and result.model == "PaddleOCR-VL-test"
    assert result.invoice.invoice_number.value == "AB/7/26-27"
    assert result.invoice.vendor_name.value == "ACME TEXTILES PRIVATE LIMITED A/C - (26-27)"
    assert len(result.invoice.items) == 4  # both pages carry the fixture table
    assert "page 1: table output repeated itself; the repeated tail was removed" in result.warnings
    assert "--- table (page 2) ---" in result.raw_text and "--- footer OCR (page 2" in result.raw_text
    json.loads(result.model_dump_json())  # serialisable end to end
    header, tables, footer = split_raw_text(result.raw_text)  # saved results can be re-mapped
    assert (header, footer) == (HEADER.strip(), FOOTER.strip()) and len(tables) == 2
    from app.services.invoice_validation import validate_invoice

    remapped, _ = map_invoice(header, tables, footer)
    assert validate_invoice(remapped, "\n".join([header, *tables, footer]))[0] == result.invoice
    # Both fake pages carry lines 1-2, so validation flags the repeated line numbers.
    assert result.validation is not None and result.validation.status == "failed"
    assert any(c.id == "line_numbers" and c.status == "fail" for c in result.validation.checks)


async def test_extractor_retries_ocr_that_loops() -> None:
    class LoopsOnce(FakePaddle):
        async def analyze(self, images: list[bytes], prompt: str, options: dict[str, Any] | None = None) -> ModelOutput:
            output = await super().analyze(images, prompt, options)
            if prompt == OCR_TASK and len(self.calls) == 1:
                return ModelOutput(text="Tax Invoice\n" + "DISPATCH\n" * 60, input_tokens=1, output_tokens=1)
            return output

    model = LoopsOnce()
    result = await InvoiceExtractor(model, zoom=SERIAL).extract([page_png()])
    assert [c[0] for c in model.calls] == [OCR_TASK, OCR_TASK, TABLE_TASK, OCR_TASK]  # header read twice
    assert result.invoice.buyer_gstin.value == BUYER_GSTIN
    assert not any("header OCR" in w for w in result.warnings)


async def test_extractor_needs_a_document_model() -> None:
    with pytest.raises(ExtractionUnavailableError, match="needs PaddleOCR-VL"):
        InvoiceExtractor(MockVisionModel())
    with pytest.raises(ValueError):
        await InvoiceExtractor(FakePaddle()).extract([])


# ===========================================================================
# Documents
# ===========================================================================


def pdf_bytes(pages: int) -> bytes:
    image = Image.new("RGB", (612, 792), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PDF", save_all=True, append_images=[image.copy() for _ in range(pages - 1)])
    return buffer.getvalue()


def test_pdf_pages_are_rendered_and_sanitised() -> None:
    pages = load_pages(pdf_bytes(2), Settings(max_document_side=1000))
    assert len(pages) == 2
    for page in pages:
        with Image.open(io.BytesIO(page)) as image:
            assert image.format == "PNG" and max(image.size) == 1000  # rendered at 200 DPI, then capped


def test_images_pass_through_the_image_pipeline() -> None:
    [page] = load_pages(page_png((400, 600)), Settings())
    assert page.startswith(b"\x89PNG")


@pytest.mark.parametrize(
    ("data", "settings", "message"),
    [
        (b"MZ\x90\x00 not a document", Settings(), "Unsupported file type"),
        (b"%PDF-1.7\n this is not really a pdf", Settings(), "could not be opened"),
        (pdf_bytes(3), Settings(max_document_pages=2), "3 pages; at most 2"),
        (pdf_bytes(1), Settings(max_document_bytes=100), "at most 0 MB"),
    ],
    ids=["not-a-document", "damaged-pdf", "too-many-pages", "too-large"],  # raw bytes make unusable test ids
)
def test_bad_documents_are_rejected(data: bytes, settings: Settings, message: str) -> None:
    with pytest.raises(DocumentError, match=message):
        load_pages(data, settings)


def test_document_type_comes_from_content() -> None:
    assert detect_document_type(pdf_bytes(1)) == "application/pdf"
    assert detect_document_type(page_png()) == "image/png"
    assert detect_document_type(b"hello") is None


# ===========================================================================
# llama.cpp backend
# ===========================================================================


def llama_transport(requests: list[dict[str, Any]], *, status: int = 200, health: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(health, json={"status": "ok"})
        body = json.loads(request.content)
        requests.append(body)
        if status != 200:
            return httpx.Response(status, json={"error": "boom"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": f" text {len(requests)} "}}], "usage": {"prompt_tokens": 10, "completion_tokens": 3}},
        )

    return httpx.MockTransport(handler)


async def test_llama_cpp_sends_each_image_with_the_task_prompt() -> None:
    requests: list[dict[str, Any]] = []
    model = LlamaCppVisionModel("PaddleOCR-VL-1.6-GGUF", "http://llama:8090/", timeout_seconds=10, transport=llama_transport(requests))
    assert model.tasks == PADDLEOCR_VL_TASKS

    output = await model.analyze(
        [page_png(), b"\xff\xd8\xff fake jpeg"], " OCR: ", {"max_tokens": 64, "temperature": 0.9, "anti_repetition": True}
    )

    assert output == {"text": "Image 1:\ntext 1\n\nImage 2:\ntext 2", "input_tokens": 20, "output_tokens": 6}
    first = requests[0]
    image_part, text_part = first["messages"][0]["content"]
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")
    assert requests[1]["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert text_part == {"type": "text", "text": "OCR:"}
    assert (first["max_tokens"], first["temperature"], first["stream"]) == (64, 0.0, False)  # tasks are deterministic
    assert first["dry_multiplier"] > 0  # anti-repetition sampling requested
    await model.close()


async def test_llama_cpp_chat_models_take_questions() -> None:
    requests: list[dict[str, Any]] = []
    model = LlamaCppVisionModel("Qwen3-VL-2B-GGUF", "http://llama:8090", timeout_seconds=10, transport=llama_transport(requests))
    assert model.tasks is None and model.supports_structured_output
    schema = {"type": "object", "properties": {"colour": {"type": "string"}}, "required": ["colour"]}
    await model.analyze([page_png()], "What is this?", {"temperature": 0.5})
    await model.analyze([page_png()], "Which colour?", {"response_format": schema})
    assert requests[0]["temperature"] == 0.5
    assert "dry_multiplier" not in requests[0] and "response_format" not in requests[0]
    assert requests[1]["response_format"] == {"type": "json_schema", "json_schema": {"name": "response", "schema": schema}}
    paddle = LlamaCppVisionModel("PaddleOCR-VL-1.6", "http://llama:8090", timeout_seconds=10, transport=llama_transport([]))
    assert not paddle.supports_structured_output
    await model.close()


def test_llama_cpp_must_be_running() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ModelUnavailableError, match="start llama-server"):
        LlamaCppVisionModel("PaddleOCR-VL", "http://llama:8090", timeout_seconds=10, transport=httpx.MockTransport(down))
    with pytest.raises(ModelUnavailableError, match="not ready"):
        LlamaCppVisionModel("PaddleOCR-VL", "http://llama:8090", timeout_seconds=10, transport=llama_transport([], health=503))


async def test_llama_cpp_errors_become_model_errors() -> None:
    model = LlamaCppVisionModel("PaddleOCR-VL", "http://llama:8090", timeout_seconds=10, transport=llama_transport([], status=500))
    with pytest.raises(ModelError, match="HTTP 500"):
        await model.analyze([page_png()], "OCR:")
    await model.close()
