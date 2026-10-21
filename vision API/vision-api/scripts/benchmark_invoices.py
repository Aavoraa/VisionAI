"""Field-level accuracy of invoice extraction against hand-checked answers.

Each benchmark invoice is a document plus an answer file in ``benchmarks/invoices/``:

    invoice-001.jpg                 the invoice (image or PDF)
    invoice-001.expected.json       the correct values (see benchmarks/invoices/README.md)
    invoice-001.result.json         written by this script: the extraction result

Run from the project root (needs the llama.cpp server unless --cached):

    python scripts/benchmark_invoices.py              # extract every invoice, then score
    python scripts/benchmark_invoices.py --cached     # re-map and re-score saved OCR output (seconds)
    python scripts/benchmark_invoices.py --rules-only # skip the reasoning model
    python scripts/benchmark_invoices.py --cached --no-zoom  # same OCR, zoomed readings ignored

With a reasoning model configured (VISION_EXTRACTION_LLM_URL or --llm-url), every invoice is
scored twice on the same OCR output: rules only, and rules cross-checked by the model.
    python scripts/benchmark_invoices.py --only invoice-001

Scoring: identifiers, numbers and dates must match exactly (after removing spaces and
case); names, addresses and descriptions count as "close" when at least 90% similar.
Fields that are absent on the invoice must come back empty. Line items are matched by
line number.
"""

import argparse
import asyncio
import datetime as dt
import difflib
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.models.base import ModelError  # noqa: E402
from app.schemas.invoice import Invoice, InvoiceExtraction  # noqa: E402
from app.services.document_service import DocumentError  # noqa: E402
from app.services.invoice_details import extract_details  # noqa: E402
from app.services.invoice_extraction import map_invoice, split_bands, split_fallback, split_raw_text, split_zoom  # noqa: E402
from app.services.invoice_fallback import apply_fallback, needs_fallback  # noqa: E402
from app.services.invoice_llm import LlmFieldMapper, document_text, merge  # noqa: E402
from app.services.invoice_reread import apply_zoom  # noqa: E402
from app.services.invoice_validation import validate_invoice  # noqa: E402
from scripts.extract_invoice import extract_file  # noqa: E402

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "benchmarks" / "invoices"
DOCUMENT_SUFFIXES = (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif")

CATEGORIES: dict[str, tuple[str, ...]] = {
    "Invoice details": ("invoice_number", "invoice_date", "place_of_supply", "currency", "delivery_note", "payment_terms"),
    "Parties": ("vendor_name", "vendor_address", "vendor_gstin", "buyer_name", "buyer_address", "buyer_gstin"),
    "Totals and taxes": ("total_quantity", "subtotal", "cgst", "sgst", "igst", "discount", "round_off", "total_amount"),
}
ITEM_COLUMNS = ("description", "hsn_sac", "quantity", "unit", "rate", "discount_percent", "taxable_value")
NUMERIC = {"total_quantity", "subtotal", "cgst", "sgst", "igst", "discount", "round_off", "total_amount",
           "quantity", "rate", "discount_percent", "taxable_value", "line_number"}
FUZZY = {"vendor_name", "vendor_address", "buyer_name", "buyer_address", "place_of_supply", "delivery_note",
         "payment_terms", "description", "transporter", "destination", "agent", "broker", "bank_name", "bank_branch",
         "amount_in_words"}
DATES = {"invoice_date", "ack_date", "reference_date", "lr_date", "due_date", "order_date"}

CORRECT, CLOSE, WRONG = "correct", "close", "wrong"


def _text(value: Any) -> str:
    text = " ".join(str(value).upper().split())
    text = re.sub(r"\s*-\s*", "-", text)  # "4342 - 34" == "4342-34"
    return text.strip(" .,")


def compare(name: str, expected: Any, got: Any) -> str:
    if expected is None or got is None:
        return CORRECT if expected is None and got is None else WRONG
    if name in NUMERIC:
        try:
            return CORRECT if Decimal(str(expected)) == Decimal(str(got)) else WRONG
        except InvalidOperation:
            return WRONG
    if name in DATES:
        return CORRECT if dt.date.fromisoformat(str(expected)) == dt.date.fromisoformat(str(got)) else WRONG
    a, b = _text(expected), _text(got)
    if name not in FUZZY:
        a, b = a.replace(" ", ""), b.replace(" ", "")
    if a == b:
        return CORRECT
    if name in FUZZY and difflib.SequenceMatcher(None, a, b).ratio() >= 0.9:
        return CLOSE
    return WRONG


@dataclass
class Tally:
    rows: list[tuple[str, str, Any, Any]] = field(default_factory=list)  # (name, outcome, expected, got)

    def add(self, name: str, expected: Any, got: Any) -> None:
        self.rows.append((name, compare(name, expected, got), expected, got))

    def score(self, *, present_only: bool = False) -> tuple[int, int, int]:
        rows = [r for r in self.rows if not present_only or r[2] is not None]
        return sum(r[1] == CORRECT for r in rows), sum(r[1] == CLOSE for r in rows), len(rows)


def pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:5.1f}%" if whole else "   n/a"


def score_document(expected: dict[str, Any], result: dict[str, Any]) -> dict[str, Tally]:
    invoice = result["invoice"]
    tallies: dict[str, Tally] = {}
    for category, names in CATEGORIES.items():
        tally = tallies.setdefault(category, Tally())
        for name in names:
            tally.add(name, expected["fields"].get(name), invoice[name]["value"])
    by_number = {item["line_number"]["value"]: item for item in invoice["items"]}
    for column in ITEM_COLUMNS:
        tally = tallies.setdefault(f"Items: {column}", Tally())
        for row in expected["items"]:
            got = by_number.get(row["line_number"])
            tally.add(column, row.get(column), got[column]["value"] if got else None)
    details = result.get("details") or {}
    for name, want in (expected.get("details") or {}).items():
        tallies.setdefault("Details (purchase entry)", Tally()).add(name, want, (details.get(name) or {}).get("value"))
    return tallies


def print_document(stem: str, expected: dict[str, Any], result: dict[str, Any], tallies: dict[str, Tally]) -> None:
    print(f"\n=== {stem}  ({result['model']}, {result['pages']} page(s), {result['processing_ms'] / 1000:.0f} s)")
    marks = {CORRECT: "OK   ", CLOSE: "CLOSE", WRONG: "WRONG"}
    for category, names in CATEGORIES.items():
        for name, outcome, want, got in tallies[category].rows:
            if outcome != CORRECT or want is not None:
                print(f"  {marks[outcome]} {name:16} expected {want!r:44.44} got {got!r:.44}")
    for name, outcome, want, got in tallies.get("Details (purchase entry)", Tally()).rows:
        if outcome != CORRECT:
            print(f"  {marks[outcome]} {name:16} expected {want!r:44.44} got {got!r:.44}")
    wrong_items = [(c, r) for c in ITEM_COLUMNS for r in tallies[f"Items: {c}"].rows if r[1] != CORRECT]
    extra = {i["line_number"]["value"] for i in result["invoice"]["items"]} - {r["line_number"] for r in expected["items"]}
    print(f"  items: {len(expected['items'])} expected, {len(result['invoice']['items'])} extracted"
          + (f", unexpected line numbers {sorted(extra)}" if extra else ""))
    for column, (_, outcome, want, got) in wrong_items:
        print(f"    {marks[outcome]} {column:16} expected {want!r:30.30} got {got!r:.30}")


def print_summary(all_tallies: list[dict[str, Tally]], label: str = "") -> None:
    merged: dict[str, Tally] = {}
    for tallies in all_tallies:
        for key, tally in tallies.items():
            merged.setdefault(key, Tally()).rows.extend(tally.rows)
    print(f"\n=== Accuracy over {len(all_tallies)} invoice(s){label}")
    print(f"  {'Category':28} {'exact':>7} {'+close':>7} {'fields':>7}   (fields printed on the invoice)")
    totals = [0, 0, 0]
    for key, tally in merged.items():
        correct, close, count = tally.score(present_only=True)
        totals = [totals[0] + correct, totals[1] + close, totals[2] + count]
        print(f"  {key:28} {pct(correct, count)} {pct(correct + close, count)} {count:7}")
    print(f"  {'OVERALL':28} {pct(totals[0], totals[2])} {pct(totals[0] + totals[1], totals[2])} {totals[2]:7}")
    empty = [r for t in merged.values() for r in t.rows if r[2] is None]
    if empty:
        kept = sum(r[1] == CORRECT for r in empty)
        print(f"  Fields absent on the invoice left empty (not invented): {kept}/{len(empty)}")


def find_document(expected_path: Path) -> Path | None:
    stem = expected_path.name.removesuffix(".expected.json")
    return next((p for p in sorted(expected_path.parent.glob(f"{stem}.*")) if p.suffix.lower() in DOCUMENT_SUFFIXES), None)


def _zoomed(invoice: Invoice, saved: InvoiceExtraction, document: str, zoom: bool) -> tuple[Invoice, list[str]]:
    """The saved zoomed readings (block crops, field re-reads) merged in; unchanged if ``zoom`` is off."""
    readings = [(kind, label, text) for kind, label, text in split_zoom(saved.raw_text)]
    return apply_zoom(invoice, readings, document) if zoom and readings else (invoice, [])


def _fallback(invoice: Invoice, saved: InvoiceExtraction, document: str) -> tuple[Invoice, list[str]]:
    """The hybrid fallback's saved readings (if the run had one) decided again with the current rules."""
    name, readings = split_fallback(saved.raw_text)
    if not readings or not needs_fallback(invoice, document):
        return invoice, []
    invoice, warnings, _ = apply_fallback(invoice, readings, document, name or "fallback model")
    return invoice, warnings


def remap(saved: InvoiceExtraction, zoom: bool = True) -> dict[str, Any]:
    """Current rules applied to saved OCR output (no OCR, no reasoning model)."""
    header, tables, footer = split_raw_text(saved.raw_text)
    invoice, warnings = map_invoice(header, tables, footer, split_bands(saved.raw_text, zoom))
    invoice, zoom_warnings = _zoomed(invoice, saved, "\n".join([header, *tables, footer]), zoom)
    warnings += zoom_warnings
    invoice, fallback_warnings = _fallback(invoice, saved, "\n".join([header, *tables, footer]))
    warnings += fallback_warnings
    invoice, validation = validate_invoice(invoice, "\n".join([header, *tables, footer]))
    ocr_warnings = [w for w in saved.warnings if "repeated" in w]
    details = extract_details(header, tables, footer, invoice, split_bands(saved.raw_text, zoom))
    return json.loads(saved.model_copy(update={"invoice": invoice, "details": details, "warnings": ocr_warnings + warnings, "reasoning_model": None, "validation": validation}).model_dump_json())


async def cross_check(saved: InvoiceExtraction, mapper: LlmFieldMapper, zoom: bool = True) -> dict[str, Any]:
    """Current rules plus the reasoning model on saved OCR output."""
    header, tables, footer = split_raw_text(saved.raw_text)
    invoice, warnings = map_invoice(header, tables, footer, split_bands(saved.raw_text, zoom))
    document = document_text(header, tables, footer)
    fields, _, _ = await mapper.read_fields(document)
    invoice, merge_warnings = merge(invoice, fields, document, mapper.name)
    invoice, zoom_warnings = _zoomed(invoice, saved, "\n".join([header, *tables, footer]), zoom)
    merge_warnings += zoom_warnings
    invoice, fallback_warnings = _fallback(invoice, saved, "\n".join([header, *tables, footer]))
    merge_warnings += fallback_warnings
    invoice, validation = validate_invoice(invoice, "\n".join([header, *tables, footer]))
    details = extract_details(header, tables, footer, invoice, split_bands(saved.raw_text, zoom))
    return json.loads(saved.model_copy(update={"invoice": invoice, "details": details, "warnings": warnings + merge_warnings, "reasoning_model": mapper.name, "validation": validation}).model_dump_json())


async def run(
    directory: Path, *, cached: bool, only: str | None, llama_url: str | None, llm_url: str | None, zoom: bool = True
) -> int:
    settings = get_settings()
    answer_files = sorted(directory.glob("*.expected.json"))
    if only:
        answer_files = [p for p in answer_files if p.name.startswith(only)]
    if not answer_files:
        print(f"No *.expected.json files in {directory}", file=sys.stderr)
        return 1
    mapper = LlmFieldMapper(settings.extraction_llm_name, llm_url, timeout_seconds=settings.extraction_llm_timeout_seconds) if llm_url else None
    rules_tallies: list[dict[str, Tally]] = []
    model_tallies: list[dict[str, Tally]] = []
    try:
        for expected_path in answer_files:
            stem = expected_path.name.removesuffix(".expected.json")
            result_path = directory / f"{stem}.result.json"
            expected = json.loads(expected_path.read_text(encoding="utf-8"))
            if not (cached and result_path.exists()):
                document = find_document(expected_path)
                if document is None:
                    print(f"{stem}: no document found next to the answer file; skipped", file=sys.stderr)
                    continue
                print(f"{stem}: extracting {document.name} ...", file=sys.stderr, flush=True)
                try:
                    extraction = await extract_file(document, settings, llama_url or settings.llama_cpp_url, llm_url)
                except (DocumentError, ModelError) as exc:
                    print(f"{stem}: extraction failed: {exc}", file=sys.stderr)
                    continue
                result_path.write_text(extraction.model_dump_json(indent=2), encoding="utf-8")
            saved = InvoiceExtraction.model_validate_json(result_path.read_text(encoding="utf-8"))
            rules = remap(saved, zoom)
            rules_tallies.append(score_document(expected, rules))
            final = rules
            if mapper is not None:
                try:
                    final = await cross_check(saved, mapper, zoom)
                except ModelError as exc:
                    print(f"{stem}: reasoning model failed: {exc}", file=sys.stderr)
                    final = rules
                model_tallies.append(score_document(expected, final))
            print_document(stem, expected, final, score_document(expected, final))
            for warning in final["warnings"]:
                print(f"  warning: {warning}")
            if final.get("validation"):
                checks = final["validation"]["checks"]
                counts = {status: sum(c["status"] == status for c in checks) for status in ("pass", "fail", "skip")}
                print(f"  validation: {final['validation']['status']} ({counts['pass']} passed, {counts['fail']} failed, {counts['skip']} skipped)")
                for check in checks:
                    if check["status"] != "pass":
                        print(f"    {check['status'].upper():4} {check['name']}: {check['message']}")
    finally:
        if mapper is not None:
            await mapper.close()
    if rules_tallies:
        print_summary(rules_tallies, " - rules only")
    if model_tallies:
        print_summary(model_tallies, f" - rules + {settings.extraction_llm_name}")
    return 0 if rules_tallies else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/benchmark_invoices.py", description=__doc__.split("\n")[0])
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="benchmark folder (default: %(default)s)")
    parser.add_argument("--cached", action="store_true", help="re-map saved *.result.json OCR output instead of re-extracting")
    parser.add_argument("--only", help="only invoices whose name starts with this")
    parser.add_argument("--llama-url", help="llama.cpp server URL (default: VISION_LLAMA_CPP_URL)")
    parser.add_argument("--llm-url", help="reasoning model server URL (default: VISION_EXTRACTION_LLM_URL)")
    parser.add_argument("--rules-only", action="store_true", help="do not use the reasoning model")
    parser.add_argument("--no-zoom", action="store_true", help="ignore saved zoomed readings (block crops, re-reads), to compare")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    llm_url = None if args.rules_only else (args.llm_url or get_settings().extraction_llm_url)
    return asyncio.run(run(args.dir, cached=args.cached, only=args.only, llama_url=args.llama_url, llm_url=llm_url, zoom=not args.no_zoom))


if __name__ == "__main__":
    raise SystemExit(main())
