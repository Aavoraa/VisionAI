# backend/app/services/processing.py
"""Reading (Claude reads the whole sheet) and evaluation (rubric marking) of a submission.

Rules kept here:
- a failed reading is recorded and the submission marked "needs attention"; the original pages
  and finished work are kept, and the teacher can retry;
- teacher edits and approved marks are never overwritten by a new reading;
- answers with uncertain transcription are not marked until a teacher verifies them;
- a missing rubric is flagged for the teacher, never invented.
"""

from __future__ import annotations

import difflib
import io
import logging
import re
import time
from pathlib import Path

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ai_config, providers
from ..models import Answer, Evaluation, Exam, Extraction, Page, Question, RubricVersion, Student, Submission
from ..providers.base import ILLEGIBLE, ProviderError
from ..security import audit
from . import prompts, scoring
from .documents import redact_top, to_png

log = logging.getLogger(__name__)


# --- reading -----------------------------------------------------------------

def _page_bytes(page: Page, redact_percent: float | None) -> bytes:
    data = Path(page.image_path).read_bytes()
    if redact_percent and page.page_no == 1:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        return to_png(redact_top(img, redact_percent))
    return data


def run_extraction(db: Session, submission_id: str, *, user_id: str | None, make_active: bool = True,
                   progress=None) -> Extraction:
    say = progress or (lambda _msg: None)
    sub = db.get(Submission, submission_id)
    exam = db.get(Exam, sub.exam_id)
    cfg = ai_config.load(db)
    extraction = Extraction(submission_id=sub.id, provider="claude", status="running", requested_by=user_id,
                            redacted=bool(cfg.redact_top_percent))
    db.add(extraction)
    sub.status = "extracting"
    db.commit()
    started = time.perf_counter()
    try:
        reader = providers.reader(cfg)
        extraction.model = reader.model
        say(f"Claude is reading {len(sub.pages)} page(s)")
        images = [_page_bytes(p, cfg.redact_top_percent) for p in sub.pages]
        result = reader.read_sheet(images, [{"number": q.number, "text": q.text} for q in exam.questions])
    except ProviderError as exc:
        extraction.status, extraction.error = "failed", str(exc)
        extraction.duration_ms = int((time.perf_counter() - started) * 1000)
        sub.status, sub.status_note = "needs_attention", f"Reading failed: {exc}"
        audit(db, user_id, "extraction_failed", "submission", sub.id, kind=exc.kind)
        db.commit()
        return extraction
    quality = {p.page_no: p.quality.get("warnings", []) for p in sub.pages}
    pages = []
    for page in result.pages:
        page.warnings = list(quality.get(page.page_no, [])) + page.warnings
        pages.append(page.to_dict())
    extraction.pages = pages
    extraction.answers = result.answers
    extraction.unassigned_text = result.unassigned_text
    extraction.name_on_sheet = result.name_on_sheet
    extraction.warnings = list(result.warnings)
    extraction.model = result.model
    extraction.usage = result.usage
    extraction.status = "done"
    extraction.duration_ms = int((time.perf_counter() - started) * 1000)
    audit(db, user_id, "extracted", "submission", sub.id, model=extraction.model, pages=len(pages),
          redacted=extraction.redacted)
    if make_active or not sub.active_extraction_id:
        activate_extraction(db, sub, extraction, user_id=user_id)
    else:
        sub.status = "extracted" if sub.status == "extracting" else sub.status
    db.commit()
    return extraction


def _ocr_status(text: str, uncertain: list[str] | None = None) -> str:
    doubtful = ILLEGIBLE in text or re.search(r"\[\?.+?\?\]", text) or uncertain
    return "needs_verification" if doubtful else "unverified"


def _norm_name(name: str) -> str:
    return " ".join(re.findall(r"[a-z]+", name.lower()))


def name_check(name_on_sheet: str | None, student: Student) -> dict:
    """Compares the name Claude read with the student chosen at upload. Only a hint: identity
    always comes from the teacher's choice, never from the reading."""
    if not name_on_sheet:
        return {"status": "unknown", "detail": "no name visible on the sheet"}
    read, chosen = _norm_name(name_on_sheet), _norm_name(student.name)
    long_read = [t for t in read.split() if len(t) > 2]
    long_chosen = [t for t in chosen.split() if len(t) > 2]
    word_match = any(difflib.SequenceMatcher(a=a, b=b).ratio() >= 0.8 for a in long_read for b in long_chosen)
    if read == chosen or word_match or difflib.SequenceMatcher(a=read, b=chosen).ratio() >= 0.8:
        return {"status": "match", "detail": f"name on sheet: {name_on_sheet}"}
    return {"status": "differs", "detail": f"name on sheet: {name_on_sheet} — does not match {student.name}"}


def activate_extraction(db: Session, sub: Submission, extraction: Extraction, *, user_id: str | None) -> None:
    """Use ``extraction`` for the answers. Teacher-corrected text and approved marks are kept."""
    exam = db.get(Exam, sub.exam_id)
    existing = {a.question_id: a for a in db.scalars(select(Answer).where(Answer.submission_id == sub.id))}
    for q in exam.questions:
        found = (extraction.answers or {}).get(q.number)
        text = (found or {}).get("text", "")
        answer = existing.get(q.id)
        if answer is None:
            answer = Answer(submission_id=sub.id, question_id=q.id)
            db.add(answer)
        flags = []
        if not text:
            flags.append("no answer found for this question in the reading; check the sheet")
        if answer.mark_status == "approved":
            if text != answer.original_text:
                flags.append("a newer reading differs; marks stay approved")
            answer.flags = flags
            continue
        changed = answer.original_text != text
        answer.original_text = text
        answer.extraction_id = extraction.id
        answer.page_refs = (found or {}).get("pages", [])
        if answer.corrected_text is None:
            answer.ocr_status = _ocr_status(text, (found or {}).get("uncertain"))
        if changed and answer.current_evaluation_id:
            answer.mark_status = "pending"
            flags.append("the transcription changed; re-evaluate")
        answer.flags = flags
    sub.active_extraction_id = extraction.id
    sub.status = "extracted"
    sub.status_note = ""
    audit(db, user_id, "extraction_activated", "submission", sub.id, extraction_id=extraction.id)


# --- evaluation --------------------------------------------------------------

def rubric_dict(db: Session, q: Question) -> dict | None:
    if not q.current_rubric_version:
        return None
    rv = db.scalars(select(RubricVersion).where(RubricVersion.question_id == q.id,
                                                RubricVersion.version == q.current_rubric_version)).first()
    if rv is None:
        return None
    return {
        "version": rv.version, "reference_answer": rv.reference_answer, "criteria": rv.criteria,
        "expected_concepts": rv.expected_concepts, "alternatives": rv.alternatives,
        "partial_credit_rules": rv.partial_credit_rules, "misconceptions": rv.misconceptions,
        "required_elements": rv.required_elements, "ignore_criteria": rv.ignore_criteria,
        "subject_rules": rv.subject_rules, "source": rv.source,
    }


def question_dict(q: Question) -> dict:
    return {"number": q.number, "text": q.text, "qtype": q.qtype, "max_marks": q.max_marks,
            "mark_increment": q.mark_increment, "numeric_answer": q.numeric_answer,
            "numeric_tolerance": q.numeric_tolerance, "unit": q.unit}


def exam_dict(exam: Exam) -> dict:
    return {"subject": exam.subject, "class_name": exam.class_name, "grade_level": exam.grade_level,
            "instructions": exam.instructions}


def rubric_problems(rubric: dict | None, q: Question) -> list[str]:
    if rubric is None:
        return ["missing: the teacher must define one before marking"]
    problems = []
    if not rubric["criteria"]:
        problems.append("the rubric has no criteria")
    for c in rubric["criteria"]:
        if not c.get("name") or float(c.get("max_marks") or 0) <= 0:
            problems.append(f"criterion {c.get('id')} needs a name and positive marks")
    total = sum(float(c.get("max_marks") or 0) for c in rubric["criteria"])
    if rubric["criteria"] and abs(total - q.max_marks) > 1e-9:
        problems.append(f"criteria add up to {total}, but the question is worth {q.max_marks}")
    return problems


def evaluate_answer(db: Session, answer: Answer, evaluator, provider_name: str, *, user_id: str | None) -> Evaluation | None:
    q = db.get(Question, answer.question_id)
    exam = db.get(Exam, q.exam_id)
    rubric = rubric_dict(db, q)
    problems = rubric_problems(rubric, q)
    if problems:
        answer.mark_status = "needs_review"
        answer.flags = list(dict.fromkeys([*answer.flags, *("rubric: " + p for p in problems)]))
        return None
    if answer.ocr_status == "needs_verification":
        answer.mark_status = "needs_review"
        note = "verify the transcription (uncertain words) before marking"
        if note not in answer.flags:
            answer.flags = [*answer.flags, note]
        return None

    text = answer.text
    qd = question_dict(q)
    started = time.perf_counter()
    if not text.strip():
        # nothing written: zero by rule, no model call; a teacher confirms (it may be a reading miss)
        result = {"question_id": q.number, "maximum_marks": q.max_marks, "suggested_marks": 0.0,
                  "criteria": [{"criterion_id": c["id"], "criterion_name": c["name"], "maximum_marks": c["max_marks"],
                                "awarded_marks": 0.0, "evidence": "", "explanation": "No answer was found."}
                               for c in rubric["criteria"]],
                  "strengths": [], "missing_concepts": [], "misconceptions": [], "student_final_answer": None,
                  "ocr_status": answer.ocr_status, "evaluation_confidence": None, "requires_teacher_review": True,
                  "review_reasons": ["no answer text was found; confirm the question was left blank"]}
        ev = Evaluation(answer_id=answer.id, rubric_version=rubric["version"], prompt_version=prompts.PROMPT_VERSION,
                        provider="rules", model=None, status="ok", result=result, suggested_marks=0.0,
                        requires_review=True, review_reasons=result["review_reasons"], answer_text_used=text,
                        requested_by=user_id)
        return _store(db, answer, ev)

    message = prompts.build_user_message(exam=exam_dict(exam), question=qd, rubric=rubric,
                                         answer_text=text, ocr_status=answer.ocr_status)
    schema = prompts.output_schema(qd, rubric)
    usage_total = {"input_tokens": 0, "output_tokens": 0}
    errors: list[str] = []
    try:
        for attempt in range(2):  # one retry when the output fails validation
            msg = message if attempt == 0 else message + "\n\n" + prompts.retry_note(errors)
            raw, usage = evaluator.evaluate(msg, schema)
            for k in usage_total:
                usage_total[k] += usage.get(k, 0)
            check = scoring.validate(raw, question=qd, rubric=rubric, answer_text=text, ocr_status=answer.ocr_status)
            if check.ok:
                break
            errors = check.errors
    except ProviderError as exc:
        ev = Evaluation(answer_id=answer.id, rubric_version=rubric["version"], prompt_version=prompts.PROMPT_VERSION,
                        provider=provider_name, model=getattr(evaluator, "model", None), status="failed",
                        error=str(exc), requires_review=True, review_reasons=[f"evaluation failed: {exc}"],
                        answer_text_used=text, usage=usage_total, requested_by=user_id,
                        duration_ms=int((time.perf_counter() - started) * 1000))
        db.add(ev)
        answer.mark_status = "needs_review"
        return ev
    duration = int((time.perf_counter() - started) * 1000)
    common = dict(answer_id=answer.id, rubric_version=rubric["version"], prompt_version=prompts.PROMPT_VERSION,
                  provider=provider_name, model=getattr(evaluator, "model", None), answer_text_used=text,
                  usage=usage_total, requested_by=user_id, duration_ms=duration, checks=check.checks)
    if not check.ok:
        ev = Evaluation(**common, status="invalid", error="; ".join(check.errors), requires_review=True,
                        review_reasons=["the AI marking failed validation twice; mark manually"], result={"raw": raw})
        db.add(ev)
        answer.mark_status = "needs_review"
        return ev
    ev = Evaluation(**common, status="ok", result=check.result, suggested_marks=check.result["suggested_marks"],
                    requires_review=check.result["requires_teacher_review"], review_reasons=check.result["review_reasons"])
    return _store(db, answer, ev)


def _store(db: Session, answer: Answer, ev: Evaluation) -> Evaluation:
    db.add(ev)
    db.flush()
    answer.current_evaluation_id = ev.id
    answer.final_marks = ev.suggested_marks
    answer.criterion_marks = {c["criterion_id"]: c["awarded_marks"] for c in ev.result.get("criteria", [])}
    answer.mark_status = "needs_review" if ev.requires_review else "suggested"
    answer.flags = [f for f in answer.flags if "re-evaluate" not in f and not f.startswith("rubric:")
                    and "verify the transcription" not in f]
    return ev


def run_evaluation(db: Session, submission_id: str, *, user_id: str | None, answer_ids: list[str] | None = None,
                   force: bool = False, progress=None) -> dict:
    say = progress or (lambda _msg: None)
    sub = db.get(Submission, submission_id)
    exam = db.get(Exam, sub.exam_id)
    cfg = ai_config.load(db)
    provider_name = "claude"
    answers = list(db.scalars(select(Answer).where(Answer.submission_id == sub.id)))
    if answer_ids:
        answers = [a for a in answers if a.id in answer_ids]
    sub.status = "evaluating"
    db.commit()
    try:
        evaluator = providers.evaluator(cfg)
    except ProviderError as exc:
        sub.status, sub.status_note = "needs_attention", f"Marking unavailable ({provider_name}): {exc}"
        db.commit()
        return {"evaluated": 0, "failed": len(answers), "error": str(exc)}
    counts = {"evaluated": 0, "failed": 0, "skipped": 0, "needs_review": 0}
    order = {q.id: q.order for q in exam.questions}
    for answer in sorted(answers, key=lambda a: order.get(a.question_id, 0)):
        if answer.mark_status == "approved" and not force:
            counts["skipped"] += 1
            continue
        current = db.get(Evaluation, answer.current_evaluation_id) if answer.current_evaluation_id else None
        q = db.get(Question, answer.question_id)
        if (current and not force and current.status == "ok" and current.answer_text_used == answer.text
                and current.rubric_version == q.current_rubric_version):
            counts["skipped"] += 1  # idempotent: same text, same rubric -> same evaluation
            continue
        say(f"marking question {q.number}")
        ev = evaluate_answer(db, answer, evaluator, provider_name, user_id=user_id)
        db.commit()
        if ev is None or ev.status != "ok":
            counts["failed" if ev is not None else "needs_review"] += 1
        else:
            counts["evaluated"] += 1
    db.refresh(sub)
    statuses = [a.mark_status for a in db.scalars(select(Answer).where(Answer.submission_id == sub.id))]
    if all(s == "approved" for s in statuses):
        sub.status = "approved"
    elif any(s in ("pending",) for s in statuses) or counts["failed"]:
        sub.status, sub.status_note = "needs_attention", "Some answers could not be marked; see the flags."
    else:
        sub.status, sub.status_note = "evaluated", ""
    audit(db, user_id, "evaluated", "submission", sub.id, provider=provider_name,
          model=getattr(evaluator, "model", None), **counts)
    db.commit()
    return counts


def submission_total(db: Session, sub: Submission) -> dict:
    """Totals computed on the backend from per-answer marks."""
    answers = list(db.scalars(select(Answer).where(Answer.submission_id == sub.id)))
    exam = db.get(Exam, sub.exam_id)
    maximum = sum(q.max_marks for q in exam.questions)
    marked = [a for a in answers if a.final_marks is not None]
    approved = all(a.mark_status == "approved" for a in answers) and len(answers) == len(exam.questions)
    return {"total": round(sum(a.final_marks for a in marked), 2), "maximum": maximum,
            "marked": len(marked), "questions": len(exam.questions), "approved": approved}
