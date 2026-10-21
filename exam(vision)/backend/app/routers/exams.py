# backend/app/routers/exams.py
"""Examinations, questions, versioned rubrics, teacher assignments and result publication."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ai_config
from ..db import get_db
from ..models import (
    QUESTION_TYPES, Answer, Exam, ExamTeacher, Question, RubricVersion, Submission, User,
)
from ..security import ManagerUser, StaffUser, audit, can_manage, exam_for_user
from ..providers.base import ProviderError
from ..services import jobs, rubric_draft
from ..services.processing import rubric_dict, rubric_problems, submission_total
from ..services.scoring import increments_ok

router = APIRouter(prefix="/api", tags=["exams"])
DB = Annotated[Session, Depends(get_db)]


def exam_out(db: Session, e: Exam) -> dict:
    teachers = list(db.scalars(select(User).join(ExamTeacher, ExamTeacher.user_id == User.id)
                               .where(ExamTeacher.exam_id == e.id)))
    subs = list(db.scalars(select(Submission).where(Submission.exam_id == e.id)))
    return {
        "id": e.id, "name": e.name, "academic_year": e.academic_year, "class_name": e.class_name,
        "section": e.section, "subject": e.subject, "grade_level": e.grade_level, "instructions": e.instructions,
        "results_status": e.results_status,
        "maximum": sum(q.max_marks for q in e.questions), "question_count": len(e.questions),
        "teachers": [{"id": t.id, "name": t.name} for t in teachers],
        "counts": {
            "submissions": len(subs),
            "processing": sum(1 for s in subs if s.status in ("uploaded", "extracting", "evaluating")),
            "pending_review": sum(1 for s in subs if s.status in ("extracted", "evaluated", "reviewed")),
            "approved": sum(1 for s in subs if s.status == "approved"),
            "failed": sum(1 for s in subs if s.status in ("needs_attention", "failed")),
        },
    }


class ExamIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    academic_year: str = Field(min_length=4, max_length=20)
    class_name: str = Field(min_length=1, max_length=20)
    section: str = ""
    subject: str = Field(min_length=1, max_length=60)
    grade_level: str = ""
    instructions: str = ""


@router.get("/exams")
def list_exams(user: StaffUser, db: DB) -> list[dict]:
    q = select(Exam).order_by(Exam.created_at.desc())
    if not can_manage(user):
        q = q.join(ExamTeacher, ExamTeacher.exam_id == Exam.id).where(ExamTeacher.user_id == user.id)
    return [exam_out(db, e) for e in db.scalars(q)]


@router.post("/exams")
def create_exam(body: ExamIn, user: ManagerUser, db: DB) -> dict:
    exam = Exam(**body.model_dump(), created_by=user.id)
    db.add(exam)
    db.flush()
    audit(db, user, "exam_created", "exam", exam.id)
    db.commit()
    return exam_out(db, exam)


@router.get("/exams/{exam_id}")
def get_exam(exam_id: str, user: StaffUser, db: DB) -> dict:
    exam = exam_for_user(db, exam_id, user)
    out = exam_out(db, exam)
    out["questions"] = [question_out(db, q) for q in exam.questions]
    return out


@router.put("/exams/{exam_id}")
def update_exam(exam_id: str, body: ExamIn, user: ManagerUser, db: DB) -> dict:
    exam = exam_for_user(db, exam_id, user)
    for k, v in body.model_dump().items():
        setattr(exam, k, v)
    audit(db, user, "exam_updated", "exam", exam.id)
    db.commit()
    return exam_out(db, exam)


class TeachersIn(BaseModel):
    user_ids: list[str]


@router.put("/exams/{exam_id}/teachers")
def set_teachers(exam_id: str, body: TeachersIn, user: ManagerUser, db: DB) -> dict:
    exam = exam_for_user(db, exam_id, user)
    for row in db.scalars(select(ExamTeacher).where(ExamTeacher.exam_id == exam.id)):
        db.delete(row)
    for uid in dict.fromkeys(body.user_ids):
        t = db.get(User, uid)
        if t is None or t.role != "teacher":
            raise HTTPException(400, "Only teacher accounts can be assigned.")
        db.add(ExamTeacher(exam_id=exam.id, user_id=uid))
    audit(db, user, "teachers_assigned", "exam", exam.id, count=len(body.user_ids))
    db.commit()
    return exam_out(db, exam)


# --- questions & rubrics -----------------------------------------------------

def question_out(db: Session, q: Question) -> dict:
    rubric = rubric_dict(db, q)
    return {"id": q.id, "number": q.number, "order": q.order, "text": q.text, "qtype": q.qtype,
            "max_marks": q.max_marks, "mark_increment": q.mark_increment, "numeric_answer": q.numeric_answer,
            "numeric_tolerance": q.numeric_tolerance, "unit": q.unit, "rubric": rubric,
            "rubric_problems": rubric_problems(rubric, q)}


class QuestionIn(BaseModel):
    number: str = Field(min_length=1, max_length=10)
    text: str = Field(min_length=1)
    qtype: str = "short"
    max_marks: float = Field(gt=0, le=100)
    mark_increment: float = Field(default=0.5, gt=0)
    numeric_answer: str | None = None
    numeric_tolerance: float = 0.01
    unit: str | None = None
    order: int | None = None


def _check_question(body: QuestionIn) -> None:
    if body.qtype not in QUESTION_TYPES:
        raise HTTPException(400, f"Question type must be one of {', '.join(QUESTION_TYPES)}.")
    if not re.fullmatch(r"\d{1,2}[a-z]?", body.number.strip().lower()):
        raise HTTPException(400, "Question number must look like 1, 2 or 2a.")
    if not increments_ok(body.max_marks, body.mark_increment):
        raise HTTPException(400, "Maximum marks must be a multiple of the mark increment.")
    if body.numeric_answer:
        try:
            float(body.numeric_answer)
        except ValueError:
            raise HTTPException(400, "The numeric answer must be a number.") from None


@router.post("/exams/{exam_id}/questions")
def add_question(exam_id: str, body: QuestionIn, user: ManagerUser, db: DB) -> dict:
    exam = exam_for_user(db, exam_id, user)
    _check_question(body)
    number = body.number.strip().lower()
    if any(q.number == number for q in exam.questions):
        raise HTTPException(409, f"Question {number} already exists.")
    data = body.model_dump()
    data["number"] = number
    data["order"] = body.order if body.order is not None else len(exam.questions) + 1
    q = Question(exam_id=exam.id, **data)
    db.add(q)
    db.flush()
    audit(db, user, "question_added", "question", q.id, exam_id=exam.id)
    db.commit()
    return question_out(db, q)


@router.put("/questions/{question_id}")
def update_question(question_id: str, body: QuestionIn, user: ManagerUser, db: DB) -> dict:
    q = db.get(Question, question_id)
    if q is None:
        raise HTTPException(404, "Question not found.")
    exam_for_user(db, q.exam_id, user)
    _check_question(body)
    approved = db.scalars(select(Answer).where(Answer.question_id == q.id, Answer.mark_status == "approved")).first()
    if approved and body.max_marks != q.max_marks:
        raise HTTPException(409, "Marks are already approved for this question; its maximum cannot change.")
    for k, v in body.model_dump().items():
        if k == "number":
            v = v.strip().lower()
        if k == "order" and v is None:
            continue
        setattr(q, k, v)
    audit(db, user, "question_updated", "question", q.id)
    db.commit()
    return question_out(db, q)


@router.delete("/questions/{question_id}")
def delete_question(question_id: str, user: ManagerUser, db: DB) -> dict:
    q = db.get(Question, question_id)
    if q is None:
        raise HTTPException(404, "Question not found.")
    exam_for_user(db, q.exam_id, user)
    if db.scalars(select(Answer).where(Answer.question_id == q.id)).first():
        raise HTTPException(409, "Answers exist for this question; it cannot be deleted.")
    db.delete(q)
    audit(db, user, "question_deleted", "question", question_id)
    db.commit()
    return {"ok": True}


class CriterionIn(BaseModel):
    id: str = Field(min_length=1, max_length=10)
    name: str = Field(min_length=1, max_length=200)
    max_marks: float = Field(gt=0)
    description: str = ""


class RubricIn(BaseModel):
    source: str = "teacher"  # "ai_draft" when the teacher saves an AI draft unchanged
    reference_answer: str = ""
    criteria: list[CriterionIn]
    expected_concepts: list[str] = []
    alternatives: list[str] = []
    partial_credit_rules: str = ""
    misconceptions: list[str] = []
    required_elements: str = ""
    ignore_criteria: str = ""
    subject_rules: str = ""


@router.post("/questions/{question_id}/rubric")
def save_rubric(question_id: str, body: RubricIn, user: StaffUser, db: DB) -> dict:
    """Saves a NEW rubric version. Existing evaluations keep the version they used; approved
    marks are not changed (use re-evaluation explicitly)."""
    q = db.get(Question, question_id)
    if q is None:
        raise HTTPException(404, "Question not found.")
    exam_for_user(db, q.exam_id, user)
    ids = [c.id.strip().upper() for c in body.criteria]
    if len(set(ids)) != len(ids):
        raise HTTPException(400, "Criterion ids must be unique.")
    if not body.criteria:
        raise HTTPException(400, "Add at least one criterion.")
    for c in body.criteria:
        if not increments_ok(c.max_marks, q.mark_increment):
            raise HTTPException(400, f"Criterion {c.id}: marks must be in steps of {q.mark_increment}.")
    total = sum(c.max_marks for c in body.criteria)
    if abs(total - q.max_marks) > 1e-9:
        raise HTTPException(400, f"Criteria add up to {total}; the question is worth {q.max_marks}.")
    version = q.current_rubric_version + 1
    criteria = [{"id": c.id.strip().upper(), "name": c.name.strip(), "max_marks": c.max_marks,
                 "description": c.description.strip()} for c in body.criteria]
    clean = lambda items: [s.strip() for s in items if s and s.strip()]  # noqa: E731
    db.add(RubricVersion(question_id=q.id, version=version, reference_answer=body.reference_answer.strip(),
                         criteria=criteria, expected_concepts=clean(body.expected_concepts),
                         alternatives=clean(body.alternatives), partial_credit_rules=body.partial_credit_rules.strip(),
                         misconceptions=clean(body.misconceptions), required_elements=body.required_elements.strip(),
                         ignore_criteria=body.ignore_criteria.strip(), subject_rules=body.subject_rules.strip(),
                         source="ai_draft" if body.source == "ai_draft" else "teacher", created_by=user.id))
    q.current_rubric_version = version
    stale = db.scalars(select(Answer).where(Answer.question_id == q.id, Answer.current_evaluation_id.is_not(None),
                                            Answer.mark_status != "approved"))
    n_stale = 0
    for a in stale:
        n_stale += 1
        if "the rubric changed; re-evaluate" not in a.flags:
            a.flags = [*a.flags, "the rubric changed; re-evaluate"]
    audit(db, user, "rubric_saved", "question", q.id, version=version)
    db.commit()
    return {**question_out(db, q), "answers_to_reevaluate": n_stale}


@router.post("/questions/{question_id}/rubric/draft")
def draft_rubric(question_id: str, user: StaffUser, db: DB) -> dict:
    """An AI-drafted rubric for the form. Nothing is saved."""
    q = db.get(Question, question_id)
    if q is None:
        raise HTTPException(404, "Question not found.")
    exam = exam_for_user(db, q.exam_id, user)
    try:
        out = rubric_draft.draft(ai_config.load(db), exam, q)
    except ProviderError as exc:
        raise HTTPException(503 if exc.retryable else 400, str(exc)) from exc
    audit(db, user, "rubric_drafted_by_ai", "question", q.id)
    db.commit()
    return out


class DraftAllIn(BaseModel):
    only_missing: bool = True


@router.post("/exams/{exam_id}/rubrics/draft-all")
def draft_all_rubrics(exam_id: str, body: DraftAllIn, user: StaffUser, db: DB) -> dict:
    """Drafts and SAVES rubrics (marked as AI drafts) for questions without one, on request."""
    exam = exam_for_user(db, exam_id, user)
    cfg = ai_config.load(db)
    done, failed = [], []
    for q in exam.questions:
        if body.only_missing and q.current_rubric_version:
            continue
        try:
            r = rubric_draft.draft(cfg, exam, q)
        except ProviderError as exc:
            failed.append({"question": q.number, "error": str(exc)})
            continue
        save_rubric(q.id, RubricIn(**r, source="ai_draft"), user, db)
        done.append(q.number)
    audit(db, user, "rubrics_drafted_by_ai", "exam", exam.id, saved=done, failed=len(failed))
    db.commit()
    return {"saved": done, "failed": failed}


@router.get("/questions/{question_id}/rubrics")
def rubric_history(question_id: str, user: StaffUser, db: DB) -> list[dict]:
    q = db.get(Question, question_id)
    if q is None:
        raise HTTPException(404, "Question not found.")
    exam_for_user(db, q.exam_id, user)
    rows = db.scalars(select(RubricVersion).where(RubricVersion.question_id == q.id)
                      .order_by(RubricVersion.version.desc()))
    return [{"version": r.version, "criteria": r.criteria, "reference_answer": r.reference_answer, "source": r.source,
             "created_at": r.created_at.isoformat() + "Z", "created_by": r.created_by} for r in rows]


class ReevaluateIn(BaseModel):
    question_id: str | None = None


@router.post("/exams/{exam_id}/reevaluate")
def reevaluate(exam_id: str, body: ReevaluateIn, user: StaffUser, db: DB) -> dict:
    """Explicit re-evaluation after a rubric change. Approved answers are not touched (reopen first)."""
    exam = exam_for_user(db, exam_id, user)
    queued = 0
    for sub in db.scalars(select(Submission).where(Submission.exam_id == exam.id)):
        q = select(Answer).where(Answer.submission_id == sub.id, Answer.mark_status != "approved")
        if body.question_id:
            q = q.where(Answer.question_id == body.question_id)
        ids = [a.id for a in db.scalars(q)]
        if ids and sub.active_extraction_id:
            _, created = jobs.enqueue(db, "evaluate", sub.id, options={"answer_ids": ids, "force": True},
                                      user_id=user.id)
            queued += created
    audit(db, user, "reevaluation_requested", "exam", exam.id, question_id=body.question_id, jobs=queued)
    db.commit()
    return {"jobs_queued": queued}


# --- results workflow --------------------------------------------------------

RESULT_STEPS = {"reviewed": ("draft",), "approved": ("reviewed",), "published": ("approved",),
                "draft": ("reviewed", "approved", "published")}


@router.post("/exams/{exam_id}/results/{target}")
def results_status(exam_id: str, target: str, user: ManagerUser, db: DB) -> dict:
    exam = exam_for_user(db, exam_id, user)
    if target not in RESULT_STEPS:
        raise HTTPException(404, "Unknown status.")
    if exam.results_status not in RESULT_STEPS[target]:
        raise HTTPException(409, f"Results are '{exam.results_status}'; cannot move to '{target}'.")
    subs = list(db.scalars(select(Submission).where(Submission.exam_id == exam.id)))
    if target in ("approved", "published"):
        not_ready = [s for s in subs if not submission_total(db, s)["approved"]]
        if not subs or not_ready:
            raise HTTPException(409, f"{len(not_ready)} answer sheet(s) still have unapproved marks.")
    exam.results_status = target
    audit(db, user, f"results_{target}", "exam", exam.id)
    db.commit()
    return exam_out(db, exam)
