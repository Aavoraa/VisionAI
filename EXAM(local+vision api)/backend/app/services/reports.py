# backend/app/services/reports.py
"""Comparison of two readings, and exam reports (all totals computed here, on the backend)."""

from __future__ import annotations

import difflib
import re
import statistics
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Answer, Evaluation, Exam, Extraction, MarkOverride, Student, Submission
from ..providers.claude_common import estimate_cost
from .processing import submission_total


def _tokens(text: str) -> list[str]:
    return re.findall(r"\S+", text)


def word_diff(a: str, b: str) -> list[dict]:
    """Word-level differences between two readings: [{"op": "equal|replace|delete|insert", "a": .., "b": ..}]."""
    ta, tb = _tokens(a), _tokens(b)
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=ta, b=tb, autojunk=False).get_opcodes():
        out.append({"op": op, "a": " ".join(ta[i1:i2]), "b": " ".join(tb[j1:j2])})
    return out


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref, hyp = _tokens(reference.lower()), _tokens(hypothesis.lower())
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1] / len(ref)


def _numbers(text: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?", text)


def compare_extractions(a: Extraction, b: Extraction) -> dict:
    """Disagreements between two providers' readings. Nothing is merged automatically."""
    pages = []
    by_page_b = {p["page_no"]: p for p in b.pages}
    for pa in a.pages:
        pb = by_page_b.get(pa["page_no"], {"text": ""})
        diff = word_diff(pa["text"], pb["text"])
        changes = [d for d in diff if d["op"] != "equal"]
        na, nb = _numbers(pa["text"]), _numbers(pb["text"])
        pages.append({
            "page_no": pa["page_no"], "diff": diff, "changed_spans": len(changes),
            "agreement": round(1 - word_error_rate(pa["text"], pb["text"]), 3) if pa["text"] or pb["text"] else 1.0,
            "numbers_a": na, "numbers_b": nb, "numbers_match": na == nb,
            "warnings_a": pa.get("warnings", []), "warnings_b": pb.get("warnings", []),
        })

    def side(e: Extraction) -> dict:
        return {"id": e.id, "provider": e.provider, "model": e.model, "status": e.status, "error": e.error,
                "duration_ms": e.duration_ms, "usage": e.usage, "cost_usd": estimate_cost(e.model, e.usage),
                "redacted": e.redacted}

    return {"a": side(a), "b": side(b), "pages": pages}


def exam_report(db: Session, exam: Exam) -> dict:
    subs = list(db.scalars(select(Submission).where(Submission.exam_id == exam.id)))
    students = {s.id: s for s in db.scalars(select(Student).where(Student.id.in_([x.student_id for x in subs])))}
    questions = list(exam.questions)
    answers = list(db.scalars(select(Answer).where(Answer.submission_id.in_([s.id for s in subs]))))
    evals = {e.id: e for e in db.scalars(select(Evaluation).where(Evaluation.answer_id.in_([a.id for a in answers])))}
    overrides = list(db.scalars(select(MarkOverride).where(MarkOverride.answer_id.in_([a.id for a in answers]))))
    extractions = list(db.scalars(select(Extraction).where(Extraction.submission_id.in_([s.id for s in subs]))))

    student_rows = []
    for sub in subs:
        st = students.get(sub.student_id)
        tot = submission_total(db, sub)
        student_rows.append({"submission_id": sub.id, "admission_no": st.admission_no if st else "",
                             "name": st.name if st else "", "status": sub.status, **tot,
                             "percent": round(100 * tot["total"] / tot["maximum"], 1) if tot["maximum"] else None})
    student_rows.sort(key=lambda r: r["name"])

    q_rows = []
    misconceptions: Counter = Counter()
    missing: Counter = Counter()
    for q in questions:
        qa = [a for a in answers if a.question_id == q.id]
        marks = [a.final_marks for a in qa if a.final_marks is not None]
        for a in qa:
            ev = evals.get(a.current_evaluation_id or "")
            if ev and ev.status == "ok":
                misconceptions.update(f"Q{q.number}: {m}" for m in ev.result.get("misconceptions", []))
                missing.update(f"Q{q.number}: {m}" for m in ev.result.get("missing_concepts", []))
        q_rows.append({
            "number": q.number, "text": q.text[:80], "max_marks": q.max_marks, "answers": len(qa),
            "average": round(statistics.mean(marks), 2) if marks else None,
            "average_percent": round(100 * statistics.mean(marks) / q.max_marks, 1) if marks and q.max_marks else None,
            "full_marks": sum(1 for m in marks if abs(m - q.max_marks) < 1e-9),
            "zero": sum(1 for m in marks if m == 0),
            "needs_review": sum(1 for a in qa if a.mark_status == "needs_review"),
        })

    # AI suggestion vs teacher-approved marks
    diffs = []
    for a in answers:
        if a.mark_status != "approved" or not a.current_evaluation_id:
            continue
        ev = evals.get(a.current_evaluation_id)
        if ev and ev.suggested_marks is not None and a.final_marks is not None:
            diffs.append(a.final_marks - ev.suggested_marks)
    ai_vs_teacher = {
        "approved_with_ai_suggestion": len(diffs),
        "exact_agreement": sum(1 for d in diffs if abs(d) < 1e-9),
        "mean_absolute_difference": round(statistics.mean(abs(d) for d in diffs), 3) if diffs else None,
        "within_half_mark": sum(1 for d in diffs if abs(d) <= 0.5 + 1e-9),
        "teacher_higher": sum(1 for d in diffs if d > 1e-9),
        "teacher_lower": sum(1 for d in diffs if d < -1e-9),
    }
    corrected = sum(1 for a in answers if a.corrected_text is not None)
    by_provider: dict[str, dict] = {}
    for e in extractions:
        row = by_provider.setdefault(e.provider, {"runs": 0, "failed": 0, "duration_ms": [], "cost_usd": 0.0})
        row["runs"] += 1
        row["failed"] += e.status == "failed"
        if e.status == "done":
            row["duration_ms"].append(e.duration_ms)
        row["cost_usd"] += estimate_cost(e.model, e.usage) or 0.0
    for row in by_provider.values():
        d = row.pop("duration_ms")
        row["average_seconds"] = round(statistics.mean(d) / 1000, 1) if d else None
        row["cost_usd"] = round(row["cost_usd"], 4)
    eval_cost = sum(estimate_cost(e.model, e.usage) or 0.0 for e in evals.values())
    totals = [r["total"] for r in student_rows if r["marked"]]
    return {
        "exam": {"id": exam.id, "name": exam.name, "subject": exam.subject, "class_name": exam.class_name,
                 "section": exam.section, "results_status": exam.results_status,
                 "maximum": sum(q.max_marks for q in questions)},
        "summary": {
            "submissions": len(subs),
            "approved_sheets": sum(1 for r in student_rows if r["approved"]),
            "pending_review": sum(1 for a in answers if a.mark_status in ("needs_review", "suggested", "pending")),
            "failed_or_attention": sum(1 for s in subs if s.status in ("needs_attention", "failed")),
            "class_average": round(statistics.mean(totals), 2) if totals else None,
            "highest": max(totals) if totals else None,
            "lowest": min(totals) if totals else None,
        },
        "students": student_rows,
        "questions": q_rows,
        "common_misconceptions": misconceptions.most_common(10),
        "common_missing_concepts": missing.most_common(10),
        "ai_vs_teacher": ai_vs_teacher,
        "overrides": {"count": len(overrides),
                      "answers_overridden": len({o.answer_id for o in overrides})},
        "ocr": {"answers": len(answers), "corrected_by_teacher": corrected,
                "correction_rate": round(corrected / len(answers), 3) if answers else None},
        "processing": by_provider,
        "evaluation_cost_usd": round(eval_cost, 4),
    }
