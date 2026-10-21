"""Benchmark the vision providers (and, if configured, marking) on the demo sheets.

    python -m tools.benchmark_providers [--providers own,claude] [--mark]

Uses the AI Configuration stored in the EXAM database. Reports, per provider:
word error rate per answer, question-mapping accuracy, number accuracy, time and failures;
with --mark, the evaluation provider's marks against the teacher's expected marks
(exact agreement, mean absolute difference). Results go to ../benchmarks/<timestamp>.json.
Nothing is assumed about which provider is better: the numbers are what was measured.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
import time
from pathlib import Path

from app import ai_config, db, providers
from app.providers.base import ProviderError
from app.services import prompts, scoring
from app.services.documents import load_pages, redact_top, to_png
from app.services.reports import word_error_rate
from app.services.segmentation import split_answers

from .demo_data import QUESTIONS

ROOT = Path(__file__).resolve().parents[2]
SHEETS = ROOT / "sample-answer-sheets"


def _nums(text: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?", text)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("÷", "/")).strip()


def run(provider_names: list[str], mark: bool) -> dict:
    db.configure()
    with db.new_session() as s:
        cfg = ai_config.load(s)
    truth = json.loads((SHEETS / "ground_truth.json").read_text(encoding="utf-8"))
    numbers = [q["number"] for q in QUESTIONS]
    report: dict = {"at": dt.datetime.now().isoformat(timespec="seconds"), "providers": {}}
    for name in provider_names:
        try:
            provider = providers.vision_provider(name, cfg)
        except ProviderError as exc:
            report["providers"][name] = {"skipped": str(exc)}
            print(f"{name}: skipped — {exc}")
            continue
        rows, failures, seconds = [], 0, []
        for st in truth["students"]:
            pages = load_pages((SHEETS / f"{st['admission_no']}.pdf").read_bytes(), 40)
            started = time.perf_counter()
            texts = []
            try:
                for no, img in enumerate(pages, start=1):
                    if name == "claude" and no == 1 and cfg.redact_top_percent:
                        img = redact_top(img, cfg.redact_top_percent)
                    texts.append((no, provider.extract_page(to_png(img), no, {}).text))
            except ProviderError as exc:
                failures += 1
                rows.append({"student": st["admission_no"], "error": str(exc)})
                continue
            seconds.append(time.perf_counter() - started)
            seg = split_answers(texts, numbers)
            for q in numbers:
                expected = st["answers"].get(q)
                got = seg.answers.get(q)
                row = {"student": st["admission_no"], "question": q,
                       "mapped_correctly": (got is not None) == (expected is not None)}
                if expected is not None and got is not None:
                    row["wer"] = round(word_error_rate(_norm(expected), _norm(got.text)), 3)
                    row["numbers_ok"] = _nums(expected) == _nums(got.text)
                    row["read"] = got.text
                rows.append(row)
            print(f"{name} {st['admission_no']}: {seconds[-1]:.1f} s")
        scored = [r for r in rows if "wer" in r]
        summary = {
            "model": provider.model,
            "sheets": len(truth["students"]), "failures": failures,
            "mean_word_error_rate": round(statistics.mean(r["wer"] for r in scored), 3) if scored else None,
            "question_mapping_accuracy": round(sum(r.get("mapped_correctly", False) for r in rows if "question" in r)
                                               / max(1, sum(1 for r in rows if "question" in r)), 3),
            "number_accuracy": round(sum(r["numbers_ok"] for r in scored) / len(scored), 3) if scored else None,
            "seconds_per_sheet": round(statistics.mean(seconds), 1) if seconds else None,
            "usage": getattr(provider, "usage", {}),
        }
        report["providers"][name] = {"summary": summary, "rows": rows}
        print(f"{name}: {summary}")

    if mark:
        report["marking"] = _mark(cfg, truth)
    out = ROOT / "benchmarks"
    out.mkdir(exist_ok=True)
    path = out / f"benchmark-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"saved {path}")
    return report


def _mark(cfg, truth) -> dict:
    """Marks the ground-truth answer text (so reading errors do not mix in) against the rubrics."""
    try:
        evaluator = providers.evaluator(cfg.evaluation_provider, cfg)
    except ProviderError as exc:
        print(f"marking: skipped — {exc}")
        return {"skipped": str(exc)}
    diffs, rows = [], []
    for st in truth["students"]:
        for q in QUESTIONS:
            answer = st["answers"].get(q["number"], "")
            expected = st["expected_marks"][q["number"]]
            rubric = {"version": 1, **q["rubric"]}
            qd = {k: q.get(k) for k in ("number", "text", "qtype", "max_marks", "mark_increment", "numeric_answer", "unit")}
            qd["numeric_tolerance"] = 0.01
            if not answer.strip():
                got = 0.0
            else:
                msg = prompts.build_user_message(exam={"subject": "Science", "class_name": "7"}, question=qd,
                                                 rubric=rubric, answer_text=answer, ocr_status="verified")
                raw, _ = evaluator.evaluate(msg, prompts.output_schema(qd, rubric))
                v = scoring.validate(raw, question=qd, rubric=rubric, answer_text=answer, ocr_status="verified")
                got = v.result["suggested_marks"] if v.ok else None
            rows.append({"student": st["admission_no"], "question": q["number"], "expected": expected, "ai": got})
            if got is not None:
                diffs.append(got - expected)
            print(f"marking {st['admission_no']} Q{q['number']}: teacher {expected}, AI {got}")
    return {"provider": cfg.evaluation_provider, "model": getattr(evaluator, "model", None), "rows": rows,
            "exact_agreement": sum(1 for d in diffs if abs(d) < 1e-9), "answers": len(rows),
            "within_half_mark": sum(1 for d in diffs if abs(d) <= 0.5),
            "mean_absolute_difference": round(statistics.mean(abs(d) for d in diffs), 3) if diffs else None}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--providers", default="own")
    parser.add_argument("--mark", action="store_true")
    args = parser.parse_args()
    run([p.strip() for p in args.providers.split(",") if p.strip()], args.mark)


if __name__ == "__main__":
    main()
