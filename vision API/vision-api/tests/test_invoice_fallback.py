"""The hybrid fallback: a larger vision model's reading, used only where it makes more checks pass."""

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.models.base import ModelError
from app.schemas.invoice import Extracted, Invoice, LineItem
from app.services.invoice_extraction import InvoiceExtractor, split_fallback
from app.services.invoice_fallback import (
    AVERAGE_RATE,
    FallbackReader,
    apply_fallback,
    fix_quantity,
    needs_fallback,
    repair_gross,
    to_items,
)
from test_invoice_extraction import FakePaddle

pytestmark = pytest.mark.anyio

MODEL = "big-vl"
D = Decimal


def line(number: int, qty: str | None, rate: str | None, amount: str, description: str = "Kurti", disc: str | None = None) -> LineItem:
    values: dict[str, Any] = {"line_number": Extracted.found(number), "description": Extracted.found(description),
                              "taxable_value": Extracted.found(D(amount))}
    if qty is not None:
        values["quantity"] = Extracted.found(D(qty))
    if rate is not None:
        values["rate"] = Extracted.found(D(rate))
    if disc is not None:
        values["discount_percent"] = Extracted.found(D(disc))
    return LineItem(**values)


def invoice(items: list[LineItem], **totals: str | None) -> Invoice:
    fields = {name: Extracted.found(D(value)) for name, value in totals.items() if value is not None}
    return Invoice(invoice_number=Extracted.found("A/1"), items=items, **fields)


def reading(items: list[dict[str, Any]], **totals: Any) -> dict[str, Any]:
    keys = ("total_quantity", "subtotal", "cgst", "sgst", "igst", "round_off", "total_amount")
    return {"items": items, **{k: totals.get(k) for k in keys}}


def model_line(number: int, qty: Any, rate: Any, amount: Any, description: str = "Kurti", disc: Any = None) -> dict[str, Any]:
    return {"line_number": number, "description": description, "hsn_sac": None, "unit": "pcs", "quantity": qty,
            "rate": rate, "discount_percent": disc, "taxable_value": amount, "tax_rate": None}


def test_a_quantity_printed_with_four_decimals_is_scaled_by_its_line_arithmetic() -> None:
    assert fix_quantity(D(150000), D(685), None, D(10275)) == 15
    assert fix_quantity(D(15), D(685), None, D(10275)) == 15  # already right
    assert fix_quantity(D(150000), D(685), None, D(999)) == 150000  # no scale fits: left for the checks


def test_gross_amounts_of_a_size_matrix_become_net_with_the_average_rate() -> None:
    items = to_items([reading([model_line(1, 4, 1555, 6360, disc=10), model_line(2, 6, 1375, 8670, disc=10)])], MODEL)
    fixed = repair_gross(items, D("13527"), MODEL)  # 0.9 x (6360 + 8670)
    assert [(i.taxable_value.value, i.rate.value, i.rate.status) for i in fixed] == [
        (D("5724.00"), D("1590.00"), "check"), (D("7803.00"), D("1445.00"), "check")]
    assert fixed[0].rate.note == AVERAGE_RATE
    assert repair_gross(items, D("15030"), MODEL) == items  # already adds up as read: untouched


def test_nothing_is_asked_when_the_checks_pass() -> None:
    good = invoice([line(1, "2", "100", "200"), line(2, "1", "50", "50")], subtotal="250", igst="12.50", total_amount="262.50")
    assert needs_fallback(good, "") == []


def test_only_the_lines_that_fail_are_taken_from_the_model() -> None:
    first = invoice([line(1, "2", "100", "200"), line(2, "3", "50", "900"), line(3, "1", "40", "40")],
                    subtotal="390", igst="19.50", total_amount="409.50")
    model = reading([model_line(1, 2, 100, 200, "Kurti misread"), model_line(2, 3, 50, 150), model_line(3, 1, 40, 40)])
    merged, warnings, adopted = apply_fallback(first, [model], "", MODEL)
    assert adopted and warnings[0].startswith(f"lines read by {MODEL}")
    assert [(i.line_number.value, i.taxable_value.value) for i in merged.items] == [(1, D(200)), (2, D(150)), (3, D(40))]
    assert merged.items[0].description.value == "Kurti"  # a passing line is kept as the rules read it
    assert merged.items[1].line_number.status == "check"  # the model's line goes to a reviewer


def test_totals_the_rules_missed_are_taken_when_they_add_up() -> None:
    first = invoice([line(1, "10", "100", "1000"), line(2, "4", "50", "200")], total_amount="1200")  # the subtotal as total
    model = reading([model_line(1, 10, 100, 1000), model_line(2, 4, 50, 200)], subtotal=1200, igst=60, total_amount=1260)
    merged, warnings, adopted = apply_fallback(first, [model], "", MODEL)
    assert adopted and warnings[0].startswith(f"totals read by {MODEL}")
    assert (merged.subtotal.value, merged.igst.value, merged.total_amount.value) == (D(1200), D(60), D(1260))
    assert merged.items == first.items  # the rules' lines stand


def test_a_reading_that_passes_no_more_checks_is_ignored() -> None:
    first = invoice([line(1, "2", "100", "200"), line(2, "3", "50", "900")], subtotal="1100", total_amount="1100")
    model = reading([model_line(1, 2, 100, 250), model_line(2, 3, 50, 999)], subtotal=5, total_amount=9)
    merged, warnings, adopted = apply_fallback(first, [model], "", MODEL)
    assert not adopted and merged == first and "kept" in warnings[0]


def test_lines_without_a_rate_are_never_replaced() -> None:
    # A freight bill's consignments have no rate: they cannot fail the line check, so they stay.
    first = invoice([line(1, "1", None, "395", "LABISA (LR BWD-12074)")], total_amount="999")
    model = reading([model_line(1, 50, 7.9, 395, "LABISA")], total_amount=999)
    merged, _, _ = apply_fallback(first, [model], "", MODEL)
    assert merged.items[0].description.value == "LABISA (LR BWD-12074)"


def client(body: dict[str, Any] | None = None, *, status: int = 200) -> FallbackReader:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["response_format"]["type"] == "json_schema"
        assert payload["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        return httpx.Response(status, json=body or {})

    return FallbackReader(MODEL, "http://big:8094", timeout_seconds=5, transport=httpx.MockTransport(handler))


def chat(content: Any, finish: str = "stop") -> dict[str, Any]:
    return {"choices": [{"finish_reason": finish, "message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7}}


async def test_the_reader_returns_the_models_json(page_png: bytes) -> None:
    reader = client(chat(reading([model_line(1, 1, 10, 10)], total_amount=10)))
    data, used_in, used_out = await reader.read_page(page_png)
    assert data["items"][0]["line_number"] == 1 and (used_in, used_out) == (11, 7)
    await reader.close()


@pytest.mark.parametrize("body,status", [(chat({"items": []}, finish="length"), 200), ({}, 500), ({"choices": []}, 200)])
async def test_a_cut_off_or_failed_reading_is_an_error(page_png: bytes, body: dict[str, Any], status: int) -> None:
    reader = client(body, status=status)
    with pytest.raises(ModelError):
        await reader.read_page(page_png)
    await reader.close()


class FakeFallback:
    name = MODEL

    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self.result, self.calls, self.scopes = result, 0, []

    async def read_page(self, page: bytes, scope: str = "items") -> tuple[dict[str, Any], int, int]:
        self.calls += 1
        self.scopes.append(scope)
        if isinstance(self.result, Exception):
            raise self.result
        if scope == "totals":
            return {k: v for k, v in self.result.items() if k != "items"}, 5, 3
        return self.result, 5, 3


async def test_the_extractor_asks_only_when_checks_fail_and_saves_the_reading(page_png: bytes) -> None:
    good = invoice([line(1, "2", "100", "200"), line(2, "1", "50", "50")], subtotal="250", igst="12.50", total_amount="262.50")
    fallback = FakeFallback(reading([]))
    extractor = InvoiceExtractor(FakePaddle(), fallback=fallback)  # type: ignore[arg-type]
    assert await extractor._read_fallback([page_png], good, "") == (good, [], [])
    assert fallback.calls == 0

    broken = invoice([line(1, "10", "100", "1000"), line(2, "4", "50", "200")], total_amount="1200")
    fallback = FakeFallback(reading([model_line(1, 10, 100, 1000), model_line(2, 4, 50, 200)], subtotal=1200, igst=60, total_amount=1260))
    extractor = InvoiceExtractor(FakePaddle(), fallback=fallback)  # type: ignore[arg-type]
    merged, sections, warnings = await extractor._read_fallback([page_png], broken, "")
    assert fallback.calls == 1 and fallback.scopes == ["totals"] and merged.total_amount.value == 1260  # totals only
    name, readings = split_fallback("\n\n".join(sections))
    assert name == MODEL and readings[0]["total_amount"] == 1260  # saved, so the benchmark can re-decide it


async def test_a_failing_fallback_model_keeps_the_first_reading(page_png: bytes) -> None:
    broken = invoice([line(1, "10", "100", "1000")], total_amount="1")
    extractor = InvoiceExtractor(FakePaddle(), fallback=FakeFallback(ModelError("down")))  # type: ignore[arg-type]
    merged, sections, warnings = await extractor._read_fallback([page_png], broken, "")
    assert merged == broken and sections == [] and "could not read the page" in warnings[0]


@pytest.fixture
def page_png() -> bytes:
    from test_invoice_extraction import page_png as make

    return make()


async def test_totals_that_expose_missing_lines_escalate_to_the_items(page_png: bytes) -> None:
    # Two lines read of three: nothing fails until the model's subtotal shows they do not add up.
    first = invoice([line(1, "10", "100", "1000"), line(2, "4", "50", "200")], total_amount="1400")
    model = reading([model_line(1, 10, 100, 1000), model_line(2, 4, 50, 200), model_line(3, 2, 100, 200)],
                    subtotal=1400, total_amount=1400)
    fallback = FakeFallback(model)
    merged, sections, _ = await InvoiceExtractor(FakePaddle(), fallback=fallback)._read_fallback([page_png], first, "")  # type: ignore[arg-type]
    assert fallback.scopes == ["totals", "items"]
    assert [i.line_number.value for i in merged.items] == [1, 2, 3] and len(sections) == 1  # only the items reading kept


async def test_freight_lines_without_a_rate_do_not_call_the_fallback(page_png: bytes) -> None:
    consignments = invoice([line(1, "1", None, "395"), line(2, "2", None, "890")], subtotal="1285", total_amount="1285")
    fallback = FakeFallback(reading([]))
    await InvoiceExtractor(FakePaddle(), fallback=fallback)._read_fallback([page_png], consignments, "")  # type: ignore[arg-type]
    assert fallback.calls == 0
