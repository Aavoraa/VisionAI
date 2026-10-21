"""Speed and accuracy of the full invoice pipeline on the benchmark invoices (live model calls).

    python scripts/performance_benchmark.py --name baseline            # every invoice in benchmarks/invoices
    python scripts/performance_benchmark.py --name opt01 --only invoice-01

Writes ``benchmarks/performance/<name>.json`` (machine-readable summary + one record per invoice)
and ``benchmarks/performance/<name>/<invoice>.result.json`` (each full result, so a later change
can be re-scored from the same readings). Needs the llama.cpp servers of ``.env`` running; the 8B
fallback runs only when ``VISION_INVOICE_FALLBACK_LLM_URL`` is set.

Accuracy is scored like ``benchmark_invoices.py`` (fields printed on the invoice, exact match);
latency is wall-clock per invoice; GPU memory is the peak ``nvidia-smi`` reading during the run.
"""

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.models.llama_cpp import LlamaCppVisionModel  # noqa: E402
from app.services.document_service import load_pages  # noqa: E402
from app.services.invoice_extraction import InvoiceExtractor, ZoomOptions  # noqa: E402
from app.services.invoice_fallback import FallbackReader  # noqa: E402
from app.services.invoice_llm import LlmFieldMapper  # noqa: E402
from app.services.invoice_service import finalize  # noqa: E402
from scripts.benchmark_invoices import find_document, score_document  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "benchmarks" / "invoices"
OUT = ROOT / "benchmarks" / "performance"
CRITICAL = ("invoice_number", "invoice_date", "vendor_gstin", "buyer_gstin", "total_amount", "subtotal",
            "cgst", "sgst", "igst", "quantity", "rate", "taxable_value")


class GpuSampler(threading.Thread):
    """Peak GPU memory (MiB) while the benchmark runs, sampled every half second."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.peak = 0
        self.samples: list[int] = []
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                     capture_output=True, text=True, timeout=5).stdout
                used = int(out.strip().splitlines()[0])
                self.samples.append(used)
                self.peak = max(self.peak, used)
            except (OSError, ValueError, IndexError, subprocess.SubprocessError):
                pass
            self._stop.wait(0.5)

    def stop(self) -> None:
        self._stop.set()


def invoice_type(expected: dict[str, Any]) -> str:
    notes = " ".join(expected.get("notes") or []).lower()
    if "freight" in notes:
        return "freight bill"
    if "size" in notes or "garment" in notes or any("PARTY FROCK" in str(i.get("description")) for i in expected["items"]):
        return "garment matrix"
    return "GST invoice"


def tally(tallies: dict[str, Any]) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """(per category [correct, total], per field name [correct, total]); printed fields only, no details."""
    by_category: dict[str, list[int]] = {}
    by_field: dict[str, list[int]] = {}
    for category, t in tallies.items():
        if category.startswith("Details"):
            continue
        for name, outcome, expected, _ in t.rows:
            if expected is None:
                continue
            for key, table in ((category, by_category), (name, by_field)):
                cell = table.setdefault(key, [0, 0])
                cell[0] += outcome == "correct"
                cell[1] += 1
    return by_category, by_field


def pct(cell: list[int]) -> float | None:
    return round(100 * cell[0] / cell[1], 1) if cell[1] else None


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


async def run(name: str, only: str | None) -> dict[str, Any]:
    settings = get_settings()
    model = await asyncio.to_thread(LlamaCppVisionModel, settings.model_name, settings.llama_cpp_url,
                                    timeout_seconds=settings.model_timeout_seconds,
                                    concurrency=settings.llama_cpp_concurrency)
    mapper = (LlmFieldMapper(settings.extraction_llm_name, settings.extraction_llm_url,
                             timeout_seconds=settings.extraction_llm_timeout_seconds)
              if settings.extraction_llm_url else None)
    fallback = (FallbackReader(settings.invoice_fallback_llm_name, settings.invoice_fallback_llm_url,
                               timeout_seconds=settings.invoice_fallback_timeout_seconds)
                if settings.invoice_fallback_llm_url else None)
    out_dir = OUT / name
    out_dir.mkdir(parents=True, exist_ok=True)
    sampler = GpuSampler()
    sampler.start()
    records: list[dict[str, Any]] = []
    totals_cat: dict[str, list[int]] = {}
    totals_field: dict[str, list[int]] = {}
    by_type: dict[str, list[int]] = {}
    try:
        for expected_path in sorted(BENCH.glob("invoice-*.expected.json")):
            stem = expected_path.name.split(".")[0]
            if only and not stem.startswith(only):
                continue
            expected = json.loads(expected_path.read_text(encoding="utf-8"))
            document = find_document(expected_path)
            if document is None:
                print(f"{stem}: no document next to the answer file; skipped", file=sys.stderr)
                continue
            pages = await asyncio.to_thread(load_pages, document.read_bytes(), settings)
            extractor = InvoiceExtractor(model, mapper, ZoomOptions.from_settings(settings), fallback=fallback)
            started = time.perf_counter()
            result = finalize(await extractor.extract(pages), settings, None)
            seconds = time.perf_counter() - started
            (out_dir / f"{stem}.result.json").write_text(result.model_dump_json(indent=1), encoding="utf-8")
            tallies = score_document(expected, json.loads(result.model_dump_json()))
            by_category, by_field = tally(tallies)
            for source, target in ((by_category, totals_cat), (by_field, totals_field)):
                for key, (ok, n) in source.items():
                    cell = target.setdefault(key, [0, 0])
                    cell[0] += ok
                    cell[1] += n
            correct, total = sum(c[0] for c in by_category.values()), sum(c[1] for c in by_category.values())
            kind = invoice_type(expected)
            cell = by_type.setdefault(kind, [0, 0])
            cell[0] += correct
            cell[1] += total
            profile = extractor.profile
            record = {
                "invoice": stem, "type": kind, "pages": result.pages, "seconds": round(seconds, 1),
                "accuracy": pct([correct, total]), "fields": total, "route": result.review_status,
                "confidence": result.confidence, "model_calls": profile["calls"],
                "stages_ms": profile["stages_ms"], "model_ms": profile["model_ms"], "calls_by_stage": profile["calls_by_stage"],
                "tokens": {"input": result.usage.input_tokens, "output": result.usage.output_tokens},
                "items": len(result.invoice.items), "expected_items": len(expected["items"]),
            }
            records.append(record)
            calls = ", ".join(f"{k} {v}" for k, v in profile["calls"].items())
            print(f"{stem}: {seconds:6.1f} s  {record['accuracy']:5.1f}%  {result.review_status:12}  calls: {calls}", flush=True)
    finally:
        sampler.stop()
        await model.close()
        if mapper is not None:
            await mapper.close()
        if fallback is not None:
            await fallback.close()

    latencies = [r["seconds"] for r in records]
    stage_names = sorted({s for r in records for s in r["stages_ms"]})
    models = sorted({m for r in records for m in r["model_calls"]})
    correct = sum(c[0] for c in totals_cat.values())
    total = sum(c[1] for c in totals_cat.values())
    summary = {
        "name": name, "invoices": len(records), "fields": total,
        "field_accuracy": pct([correct, total]),
        "invoice_accuracy_mean": round(statistics.mean(r["accuracy"] for r in records), 1) if records else None,
        "average_latency_s": round(statistics.mean(latencies), 1) if latencies else None,
        "p50_latency_s": round(percentile(latencies, 0.5), 1) if latencies else None,
        "p95_latency_s": round(percentile(latencies, 0.95), 1) if latencies else None,
        "total_time_s": round(sum(latencies), 1),
        "average_model_calls": {m: round(statistics.mean(r["model_calls"].get(m, 0) for r in records), 2) for m in models},
        "average_model_calls_total": round(statistics.mean(sum(r["model_calls"].values()) for r in records), 2) if records else None,
        "average_stage_ms": {s: round(statistics.mean(r["stages_ms"].get(s, 0) for r in records)) for s in stage_names},
        "average_model_ms": {s: round(statistics.mean(r["model_ms"].get(s, 0) for r in records))
                             for s in sorted({s for r in records for s in r["model_ms"]})},
        "llama_cpp_concurrency": get_settings().llama_cpp_concurrency,
        "parallel_calls": get_settings().invoice_parallel_calls,
        "targeted_blocks": get_settings().invoice_targeted_blocks,
        "8b_trigger_rate": round(sum(1 for r in records if r["model_calls"].get("qwen8b")) / len(records), 3) if records else None,
        "review_rate": round(sum(1 for r in records if r["route"] != "approved") / len(records), 3) if records else None,
        "routes": {route: sum(1 for r in records if r["route"] == route) for route in ("approved", "needs_review", "reprocess")},
        "gpu_memory_peak_mib": sampler.peak,
        "gpu_memory_mean_mib": round(statistics.mean(sampler.samples)) if sampler.samples else None,
        "accuracy_by_category": {k: pct(v) for k, v in totals_cat.items()},
        "accuracy_by_field": {k: pct(v) for k, v in sorted(totals_field.items()) if k in CRITICAL or k in (
            "vendor_name", "buyer_name", "description")},
        "accuracy_by_type": {k: pct(v) for k, v in by_type.items()},
        "records": records,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True, help="name of this run (benchmarks/performance/<name>.json)")
    parser.add_argument("--only", help="only invoices whose name starts with this")
    args = parser.parse_args()
    summary = asyncio.run(run(args.name, args.only))
    print(json.dumps({k: v for k, v in summary.items() if k != "records"}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
