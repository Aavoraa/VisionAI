"""Phase 2: normalisation and the reasoning-model cross-check."""

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.models.base import ModelError, ModelUnavailableError
from app.schemas.invoice import Extracted
from app.services.invoice_extraction import map_invoice
from app.services.invoice_llm import FIELD_GUIDE, LlmFieldMapper, document_text, merge, response_schema
from app.services.invoice_normalize import format_state, normalize_invoice_number, normalize_unit
from test_invoice_extraction import BUYER_GSTIN, FOOTER, HEADER, SELLER_GSTIN, TABLE

pytestmark = pytest.mark.anyio

MODEL = "tiny-llm"


# ===========================================================================
# Normalisation
# ===========================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Tamil Nadu, Code : 33", "Tamil Nadu (33)"),
        ("TAMIL NADU", "Tamil Nadu (33)"),
        ("33", "Tamil Nadu (33)"),
        ("Karnataka code 29", "Karnataka (29)"),
        ("Orissa", "Odisha (21)"),
        ("  goa ", "Goa (30)"),
        ("Tamil Nadu, Code : 29", "Tamil Nadu, Code : 29"),  # contradictory: left as printed
        ("Narnia", "Narnia"),
    ],
)
def test_format_state(text: str, expected: str) -> None:
    assert format_state(text) == expected


def test_normalize_unit() -> None:
    assert [normalize_unit(u) for u in ("PCS.", "Pieces", "Nos", "Kgs", "Metres", "bundle")] == ["pcs", "pcs", "nos", "kg", "mtr", "bundle"]


def test_invoice_number_year_separator() -> None:
    assert normalize_invoice_number("CR/134/26/27", "for SHALIHA A/C - (26-27)") == ("CR/134/26-27", True)
    assert normalize_invoice_number("CR/134/26/27", "no year printed elsewhere") == ("CR/134/26/27", False)
    assert normalize_invoice_number("CR/134/26/29", "(26-29)") == ("CR/134/26/29", False)  # not consecutive
    assert normalize_invoice_number("INV/2026/27", "(26-27)") == ("INV/2026/27", False)


def test_rules_use_the_normalisers() -> None:
    header = HEADER.replace("AB/7/26-27 03-Aug-26", "AB/7/26-27 03-Aug-26\nfor ACME (26-27)")
    table = TABLE.replace("<fcel>AB/7/26-27<lcel>", "<fcel>AB/7/26/27<lcel>").replace("2 pcs", "2 PCS.")
    invoice, _ = map_invoice(header, [table])
    assert invoice.invoice_number.value == "AB/7/26-27" and "normalised" in (invoice.invoice_number.note or "")
    assert invoice.items[0].unit.value == "pcs"


# ===========================================================================
# Cross-check
# ===========================================================================


def rules() -> tuple[Any, str]:
    invoice, _ = map_invoice(HEADER, [TABLE], FOOTER)
    return invoice, document_text(HEADER, [TABLE], FOOTER)


def model_reading(**overrides: str | None) -> dict[str, str | None]:
    fields: dict[str, str | None] = {
        "invoice_number": "AB/7/26-27",
        "invoice_date": "03-Aug-26",
        "vendor_name": "ACME TEXTILES PRIVATE LIMITED A/C - (26-27)",
        "vendor_address": "12 MILL STREET, BENGALURU-560001",
        "vendor_gstin": SELLER_GSTIN,
        "buyer_name": "NOVA FASHIONS PRIVATE LIMITED",
        "buyer_address": "4 MARKET ROAD, PUNE-411001",
        "buyer_gstin": BUYER_GSTIN,
        "place_of_supply": None,
        "delivery_note": "DN-55",
        "payment_terms": "30 days",
        "total_amount": "₹ 2,079.20",
    }
    fields.update(overrides)
    return fields


def test_document_text_leaves_out_line_items() -> None:
    _, document = rules()
    assert "COTTON SHIRT" not in document  # items are parsed by the rules, not sent to the model
    assert "Invoice No." in document and "2,079.20" in document and "for ACME TEXTILES" in document


def test_agreement_confirms_without_changing_values() -> None:
    invoice, document = rules()
    merged, warnings = merge(invoice, model_reading(), document, MODEL)
    assert warnings == []
    for name in ("invoice_number", "invoice_date", "vendor_gstin", "buyer_name", "total_amount"):
        field = getattr(merged, name)
        assert field.value == getattr(invoice, name).value and field.status == "ok"
        assert f"confirmed by {MODEL}" in (field.note or "")


def test_disagreement_keeps_the_rule_value_and_flags_it() -> None:
    invoice, document = rules()
    merged, warnings = merge(invoice, model_reading(buyer_gstin="27PQRST6789K1ZX", total_amount="2,079.00"), document, MODEL)
    assert (merged.buyer_gstin.value, merged.buyer_gstin.status) == (BUYER_GSTIN, "check")
    assert "27PQRST6789K1ZX" in (merged.buyer_gstin.note or "")
    assert (merged.total_amount.value, merged.total_amount.status) == (Decimal("2079.20"), "check")
    assert warnings == [f"2 field(s) read differently by {MODEL}; marked 'check'"]


def test_model_only_values_are_never_trusted_blindly() -> None:
    invoice, document = rules()
    invoice = invoice.model_copy(update={"delivery_note": Extracted(), "payment_terms": Extracted(), "place_of_supply": Extracted()})
    merged, warnings = merge(
        invoice,
        model_reading(delivery_note="DN-55", payment_terms="60 days", place_of_supply="AB/7/26-27"),
        document,
        MODEL,
    )
    # Found only by the model but present in the text: kept for review.
    assert (merged.delivery_note.value, merged.delivery_note.status) == ("DN-55", "check")
    # Not in the text at all (invented), or copied from another field: dropped.
    assert merged.payment_terms.value is None and merged.place_of_supply.value is None
    assert any("'60 days' for payment_terms, which is not in the OCR text" in w for w in warnings)
    assert any("'AB/7/26-27' for place_of_supply, the value of another field" in w for w in warnings)


@pytest.mark.parametrize("boilerplate", ["E & OE", "E. & O.E.", "INR Two Thousand Only", "Delivery Note Date", "Authorised Signatory"])
def test_printed_boilerplate_is_never_a_value(boilerplate: str) -> None:
    invoice, document = rules()
    invoice = invoice.model_copy(update={"payment_terms": Extracted()})
    merged, warnings = merge(invoice, model_reading(payment_terms=boilerplate), document + f"\n{boilerplate}", MODEL)
    assert merged.payment_terms.value is None
    assert any("boilerplate" in w for w in warnings)


def test_model_text_that_is_not_a_gstin_is_never_a_gstin() -> None:
    invoice, document = rules()
    invoice = invoice.model_copy(update={"vendor_gstin": Extracted(), "buyer_gstin": Extracted()})
    extra = "\nigchennai@gmail.com\n7de9e32d8ea6de0ccff6a1ccf4237b8b9"
    merged, warnings = merge(
        invoice,
        model_reading(vendor_gstin="7de9e32d8ea6de0ccff6a1ccf4237b8b9", buyer_gstin="igchennai@gmail.com"),
        document + extra,
        MODEL,
    )
    assert merged.vendor_gstin.value is None and merged.buyer_gstin.value is None
    assert any("'igchennai@gmail.com' for buyer_gstin, which is not a GSTIN" in w for w in warnings)


def test_codes_are_never_names_and_dates_are_never_addresses() -> None:
    invoice, document = rules()
    invoice = invoice.model_copy(update={"vendor_name": Extracted(), "buyer_address": Extracted()})
    extra = "\n7de9e32d8ea6de0ccff6a1ccf4237b8b9\n16-Jul-26"
    merged, warnings = merge(
        invoice, model_reading(vendor_name="7de9e32d8ea6de0ccff6a1ccf4237b8b9", buyer_address="16-Jul-26"), document + extra, MODEL
    )
    assert merged.vendor_name.value is None and merged.buyer_address.value is None
    assert any("for vendor_name, which has no word in it" in w for w in warnings)
    assert any("for buyer_address, which is a date" in w for w in warnings)


def test_empty_model_answers_change_nothing() -> None:
    invoice, document = rules()
    merged, warnings = merge(invoice, {name: None for name in FIELD_GUIDE}, document, MODEL)
    assert merged == invoice and warnings == []


# ===========================================================================
# llama.cpp client
# ===========================================================================


def llm_transport(requests: list[dict[str, Any]], content: str, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 900, "completion_tokens": 120}})

    return httpx.MockTransport(handler)


async def test_mapper_requests_schema_constrained_json() -> None:
    requests: list[dict[str, Any]] = []
    answer = json.dumps({**{name: None for name in FIELD_GUIDE}, "invoice_number": " AB/7/26-27 ", "buyer_name": "", "extra": "x"})
    mapper = LlmFieldMapper(MODEL, "http://llm:8091/", timeout_seconds=10, transport=llm_transport(requests, answer))
    fields, used_in, used_out = await mapper.read_fields("OCR text")
    await mapper.close()

    assert (used_in, used_out) == (900, 120)
    assert fields["invoice_number"] == "AB/7/26-27" and fields["buyer_name"] is None and "extra" not in fields
    body = requests[0]
    assert body["temperature"] == 0.0
    assert body["response_format"]["json_schema"]["schema"] == response_schema()
    assert set(response_schema()["required"]) == set(FIELD_GUIDE)
    assert "OCR text" in body["messages"][1]["content"]


@pytest.mark.parametrize(("content", "status"), [("not json", 200), ("[1, 2]", 200), ("{}", 500)])
async def test_mapper_failures_raise_model_errors(content: str, status: int) -> None:
    mapper = LlmFieldMapper(MODEL, "http://llm:8091", timeout_seconds=10, transport=llm_transport([], content, status))
    with pytest.raises(ModelError):
        await mapper.read_fields("OCR text")
    await mapper.close()


async def test_mapper_unreachable() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    mapper = LlmFieldMapper(MODEL, "http://llm:8091", timeout_seconds=10, transport=httpx.MockTransport(down))
    with pytest.raises(ModelUnavailableError):
        await mapper.read_fields("OCR text")
    await mapper.close()


# ===========================================================================
# In the extraction pipeline
# ===========================================================================


class FakeMapper:
    name = MODEL

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.documents: list[str] = []

    async def read_fields(self, document: str) -> tuple[dict[str, str | None], int, int]:
        self.documents.append(document)
        if self.fail:
            raise ModelUnavailableError("reasoning model not reachable")
        return model_reading(), 900, 120


async def test_extractor_cross_checks_and_counts_tokens() -> None:
    from app.services.invoice_extraction import InvoiceExtractor
    from test_invoice_extraction import FakePaddle, page_png

    mapper = FakeMapper()
    result = await InvoiceExtractor(FakePaddle(), mapper).extract([page_png()])  # type: ignore[arg-type]
    assert result.reasoning_model == MODEL and len(mapper.documents) == 1
    assert "confirmed by" in (result.invoice.buyer_gstin.note or "")
    # 3 OCR/table calls x 100 in / 50 out (FakePaddle) + the reasoning model's 900 / 120
    assert (result.usage.input_tokens, result.usage.output_tokens) == (3 * 100 + 900, 3 * 50 + 120)


async def test_extractor_falls_back_to_rules_when_the_model_fails() -> None:
    from app.services.invoice_extraction import InvoiceExtractor
    from test_invoice_extraction import FakePaddle, page_png

    result = await InvoiceExtractor(FakePaddle(), FakeMapper(fail=True)).extract([page_png()])  # type: ignore[arg-type]
    assert result.reasoning_model is None
    assert result.invoice.buyer_gstin.value == BUYER_GSTIN
    assert any("unavailable" in w and "rules only" in w for w in result.warnings)


# ===========================================================================
# response_format on /v1/vision
# ===========================================================================


async def test_vision_passes_response_format_to_models_that_support_it(
    settings: Any, make_client: Any, auth_headers: dict[str, str]
) -> None:
    from app.models.local_transformers import MockVisionModel
    from test_vision_api import png_bytes

    class StructuredMock(MockVisionModel):
        supports_structured_output = True
        seen: dict[str, Any] = {}

        async def analyze(self, images: list[bytes], prompt: str, options: dict[str, Any] | None = None) -> Any:
            StructuredMock.seen = dict(options or {})
            return {"text": '{"colour": "red"}', "input_tokens": 5, "output_tokens": 3}

    schema = {"type": "object", "properties": {"colour": {"type": "string"}}, "required": ["colour"]}
    files = [("images", ("a.png", png_bytes(), "image/png"))]
    async with make_client(settings, model=StructuredMock()) as client:
        response = await client.post(
            "/v1/vision", headers=auth_headers, data={"prompt": "Colour?", "response_format": json.dumps(schema)}, files=files
        )
    assert response.status_code == 200, response.text
    assert StructuredMock.seen["response_format"] == schema
    assert json.loads(response.json()["result"]["text"]) == {"colour": "red"}
