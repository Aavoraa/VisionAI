"""Benchmark Claude on the demo sheets: reading accuracy and marking agreement with the teacher key.

    python -m tools.benchmark_claude [--no-mark]

Uses the AI Configuration in the exam-vision database (key, models, effort). Reading: each
sheet is read in one request with the question list, then compared with ground_truth.json
(word error rate per answer, question mapping, numbers, name). Marking: the ground-truth answer
text is marked against the demo rubrics (so reading errors do not mix in) and compared with the
teacher's expected marks. Results go to ../benchmarks/<timestamp>.json.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
from pathlib import Path

from app import ai_config, db, providers
from app.models import Student
from app.providers.base import ProviderError
from app.providers.claude_common import estimate_cost
from app.services import prompts, scoring
from app.services.documents import load_pages, to_png
from app.services.processing import name_check
from app.services.reports import word_error_rate

from .demo_data import EXAM, QUESTIONS

ROOT = Path(__file__).resolve().parents[2]
SHEETS = ROOT / "sample-answer-sheets"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("÷", "/")).strip()


def _nums(text: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?", text)


def run(mark: bool) -> dict:
    db.configure()
    with db.new_session() as s:
        cfg = ai_config.load(s)
    truth = json.loads((SHEETS / "ground_truth.json").read_text(encoding="utf-8"))
    questions = [{"number": q["number"], "text": q["text"]} for q in QUESTIONS]
    report: dict = {"at": dt.datetime.now().isoformat(timespec="seconds"), "reading_model": cfg.claude_vision_model,
                    "marking_model": cfg.claude_eval_model, "effort": cfg.claude_effort}
    reader = providers.reader(cfg)
    rows, seconds, read_cost = [], [], 0.0
    for st in truth["students"]:
        images = [to_png(p) for p in load_pages((SHEETS / f"{st['admission_no']}.pdf").read_bytes(), 40)]
        try:
            r = reader.read_sheet(images, questions)
        except ProviderError as exc:
            rows.append({"student": st["admission_no"], "error": str(exc)})
            print(f"read {st['admission_no']}: FAILED {exc}")
            continue
        seconds.append(r.duration_ms / 1000)
        cost = estimate_cost(r.model, r.usage) or 0.0
        read_cost += cost
        nc = name_check(r.name_on_sheet, Student(admission_no=st["admission_no"], name=st["name"], class_name="7"))
        print(f"read {st['admission_no']}: {r.duration_ms / 1000:.1f} s, ${cost:.4f}, name {r.name_on_sheet!r} ({nc['status']})")
        for q in questions:
            expected = st["answers"].get(q["number"])
            got = (r.answers.get(q["number"]) or {}).get("text", "")
            row = {"student": st["admission_no"], "question": q["number"],
                   "mapped_correctly": bool(got) == (expected is not None), "read": got}
            if expected is not None and got:
                row["wer"] = round(word_error_rate(_norm(expected), _norm(got)), 3)
                row["numbers_ok"] = _nums(expected) == _nums(got)
            rows.append(row)
    scored = [r for r in rows if "wer" in r]
    mapped = [r for r in rows if "mapped_correctly" in r]
    report["reading"] = {
        "sheets": len(truth["students"]), "failures": sum(1 for r in rows if "error" in r),
        "mean_word_error_rate": round(statistics.mean(r["wer"] for r in scored), 3) if scored else None,
        "question_mapping_accuracy": round(sum(r["mapped_correctly"] for r in mapped) / len(mapped), 3) if mapped else None,
        "number_accuracy": round(sum(r["numbers_ok"] for r in scored) / len(scored), 3) if scored else None,
        "seconds_per_sheet": round(statistics.mean(seconds), 1) if seconds else None,
        "cost_usd": round(read_cost, 4), "rows": rows,
    }
    print("reading:", {k: v for k, v in report["reading"].items() if k != "rows"})
    if mark:
        report["marking"] = _mark(cfg, truth)
        print("marking:", {k: v for k, v in report["marking"].items() if k != "rows"})
    out = ROOT / "benchmarks"
    out.mkdir(exist_ok=True)
    path = out / f"benchmark-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"saved {path}")
    return report


def _mark(cfg, truth) -> dict:
    evaluator = providers.evaluator(cfg)
    diffs, rows, cost = [], [], 0.0
    for st in truth["students"]:
        for q in QUESTIONS:
            answer = st["answers"].get(q["number"], "")
            expected = st["expected_marks"][q["number"]]
            rubric = {"version": 1, **q["rubric"]}
            qd = {k: q.get(k) for k in ("number", "text", "qtype", "max_marks", "mark_increment", "numeric_answer", "unit")}
            qd["numeric_tolerance"] = 0.01
            if not answer.strip():
                got, reasons = 0.0, ["blank"]
            else:
                msg = prompts.build_user_message(exam={"subject": EXAM["subject"], "class_name": EXAM["class_name"]},
                                                 question=qd, rubric=rubric, answer_text=answer, ocr_status="verified")
                raw, usage = evaluator.evaluate(msg, prompts.output_schema(qd, rubric))
                cost += estimate_cost(evaluator.model, usage) or 0.0
                v = scoring.validate(raw, question=qd, rubric=rubric, answer_text=answer, ocr_status="verified")
                got, reasons = (v.result["suggested_marks"], v.result["review_reasons"]) if v.ok else (None, v.errors)
            rows.append({"student": st["admission_no"], "question": q["number"], "teacher": expected, "ai": got,
                         "review_reasons": reasons})
            if got is not None:
                diffs.append(got - expected)
            print(f"  mark {st['admission_no']} Q{q['number']}: teacher {expected}, Claude {got}")
    return {"answers": len(rows), "exact_agreement": sum(1 for d in diffs if abs(d) < 1e-9),
            "within_half_mark": sum(1 for d in diffs if abs(d) <= 0.5 + 1e-9),
            "mean_absolute_difference": round(statistics.mean(abs(d) for d in diffs), 3) if diffs else None,
            "cost_usd": round(cost, 4), "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-mark", action="store_true")
    run(mark=not parser.parse_args().no_mark)


if __name__ == "__main__":
    main()
