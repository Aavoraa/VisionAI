# backend/app/routers/submissions.py
"""Answer-sheet upload, pages, reading, comparison, review and marking."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ai_config
from ..config import settings
from ..db import get_db
from ..models import (
    Answer, Evaluation, Exam, Extraction, Job, MarkOverride, Page, Question, Student, Submission, User, now,
)
from ..security import CurrentUser, StaffUser, audit, exam_for_user
from ..services import jobs, processing
from ..services.documents import DocumentError, load_pages, quality, rotate, sha256, to_png
from ..services.reports import sheet_cost
from ..services.scoring import increments_ok

router = APIRouter(prefix="/api", tags=["submissions"])
DB = Annotated[Session, Depends(get_db)]


def _submission(db: Session, submission_id: str, user: User) -> tuple[Submission, Exam]:
    sub = db.get(Submission, submission_id)
    if sub is None:
        raise HTTPException(404, "Answer sheet not found.")
    exam = exam_for_user(db, sub.exam_id, user)  # enforces teacher assignment
    return sub, exam


def _answer(db: Session, answer_id: str, user: User) -> tuple[Answer, Submission, Exam]:
    answer = db.get(Answer, answer_id)
    if answer is None:
        raise HTTPException(404, "Answer not found.")
    sub, exam = _submission(db, answer.submission_id, user)
    return answer, sub, exam


def _job_out(job: Job | None) -> dict | None:
    if job is None:
        return None
    return {"id": job.id, "kind": job.kind, "status": job.status, "progress": job.progress, "error": job.error,
            "provider": job.provider}


def _sub_row(db: Session, sub: Submission) -> dict:
    st = db.get(Student, sub.student_id)
    last_job = db.scalars(select(Job).where(Job.submission_id == sub.id).order_by(Job.created_at.desc())).first()
    return {"id": sub.id, "student": {"id": st.id, "name": st.name, "admission_no": st.admission_no,
                                      "roll_no": st.roll_no},
            "status": sub.status, "status_note": sub.status_note, "pages": sub.page_count,
            "identity_verified": sub.identity_verified, "filename": sub.original_filename,
            "created_at": sub.created_at.isoformat() + "Z", "totals": processing.submission_total(db, sub),
            "cost": sheet_cost(db, sub), "name_check": _name_check(db, sub, st), "job": _job_out(last_job)}


def _name_check(db: Session, sub: Submission, student: Student) -> dict | None:
    ext = db.get(Extraction, sub.active_extraction_id) if sub.active_extraction_id else None
    return processing.name_check(ext.name_on_sheet, student) if ext else None


# --- upload ------------------------------------------------------------------

def _store_submission(db: Session, exam: Exam, student: Student, files: list[tuple[str, bytes]], user: User) -> Submission:
    if student.class_name != exam.class_name or (exam.section and student.section and student.section != exam.section):
        raise HTTPException(400, f"{student.name} is not in {exam.class_name} {exam.section}".strip() + ".")
    if db.scalars(select(Submission).where(Submission.exam_id == exam.id, Submission.student_id == student.id)).first():
        raise HTTPException(409, f"An answer sheet for {student.name} is already uploaded for this exam.")
    combined = b"".join(data for _, data in files)
    digest = sha256(combined)
    if db.scalars(select(Submission).where(Submission.exam_id == exam.id, Submission.file_sha256 == digest)).first():
        raise HTTPException(409, "This exact file was already uploaded for another student.")
    images: list[Image.Image] = []
    for name, data in files:
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(413, f"{name} is larger than {settings.max_upload_bytes // (1024 * 1024)} MB.")
        try:
            images += load_pages(data, settings.max_pages - len(images))
        except DocumentError as exc:
            raise HTTPException(400, f"{name}: {exc}") from exc
    if len(images) > settings.max_pages:
        raise HTTPException(400, f"At most {settings.max_pages} pages per answer sheet.")
    sub = Submission(exam_id=exam.id, student_id=student.id, file_sha256=digest,
                     original_filename=", ".join(n for n, _ in files)[:200], page_count=len(images),
                     uploaded_by=user.id, status="uploaded")
    db.add(sub)
    db.flush()
    folder = settings.upload_dir / sub.id
    folder.mkdir(parents=True, exist_ok=True)
    for i, (name, data) in enumerate(files, start=1):  # the originals, kept as uploaded
        suffix = ".pdf" if data[:4] == b"%PDF" else ".img"
        (folder / f"original-{i}{suffix}").write_bytes(data)
    for no, img in enumerate(images, start=1):
        path = folder / f"page-{no}.png"
        path.write_bytes(to_png(img))
        db.add(Page(submission_id=sub.id, page_no=no, image_path=str(path), width=img.width, height=img.height,
                    quality=quality(img)))
    audit(db, user, "sheet_uploaded", "submission", sub.id, exam_id=exam.id, pages=len(images))
    db.commit()
    db.refresh(sub)
    return sub


def _maybe_start(db: Session, sub: Submission, user: User) -> tuple[Job | None, str]:
    """Auto-processing after upload: the sheet is stored either way."""
    ok, why = ai_config.load(db).claude_vision_ready()
    if not ok:
        return None, f"Stored, not read yet: {why}"
    job, _ = jobs.enqueue(db, "process", sub.id, user_id=user.id)
    return job, ""


def _start(db: Session, sub: Submission, user: User, kind: str = "process") -> Job:
    ok, why = ai_config.load(db).claude_vision_ready()
    if not ok:
        raise HTTPException(400, why)
    job, _ = jobs.enqueue(db, kind, sub.id, user_id=user.id)
    return job


@router.post("/exams/{exam_id}/submissions")
async def upload(exam_id: str, user: StaffUser, db: DB, student_id: str = Form(...),
                 files: list[UploadFile] = File(...), auto_process: bool = Form(True)) -> dict:
    exam = exam_for_user(db, exam_id, user)
    student = db.get(Student, student_id)
    if student is None:
        raise HTTPException(404, "Student not found.")
    data = [(f.filename or "sheet", await f.read()) for f in files]
    sub = _store_submission(db, exam, student, data, user)
    job, note = _maybe_start(db, sub, user) if auto_process else (None, "")
    return {**_sub_row(db, sub), "job": _job_out(job), "note": note}


@router.post("/exams/{exam_id}/submissions/bulk")
async def bulk_upload(exam_id: str, user: StaffUser, db: DB, files: list[UploadFile] = File(...),
                      auto_process: bool = Form(True)) -> dict:
    """One file per student, named with the admission number (e.g. A1001.pdf). The match comes from
    the file name chosen by staff, never from OCR; identity still needs confirming in review."""
    exam = exam_for_user(db, exam_id, user)
    students = list(db.scalars(select(Student).where(Student.class_name == exam.class_name)))
    results = []
    for f in files:
        name = f.filename or ""
        stem = re.sub(r"\.[^.]+$", "", name).lower()
        match = [s for s in students if re.search(rf"(?<![a-z0-9]){re.escape(s.admission_no.lower())}(?![a-z0-9])", stem)]
        if len(match) != 1:
            results.append({"file": name, "ok": False,
                            "error": "no student with this admission number" if not match else "matches several students"})
            continue
        try:
            sub = _store_submission(db, exam, match[0], [(name, await f.read())], user)
            job, note = _maybe_start(db, sub, user) if auto_process else (None, "")
            results.append({"file": name, "ok": True, "submission_id": sub.id, "student": match[0].name,
                            "job": _job_out(job), "note": note})
        except HTTPException as exc:
            db.rollback()
            results.append({"file": name, "ok": False, "error": exc.detail})
    return {"results": results}


@router.get("/exams/{exam_id}/submissions")
def list_submissions(exam_id: str, user: StaffUser, db: DB) -> list[dict]:
    exam = exam_for_user(db, exam_id, user)
    subs = db.scalars(select(Submission).where(Submission.exam_id == exam.id).order_by(Submission.created_at))
    return [_sub_row(db, s) for s in subs]


# --- one sheet ---------------------------------------------------------------

def _evaluation_out(ev: Evaluation | None) -> dict | None:
    if ev is None:
        return None
    return {"id": ev.id, "status": ev.status, "provider": ev.provider, "model": ev.model,
            "rubric_version": ev.rubric_version, "prompt_version": ev.prompt_version,
            "suggested_marks": ev.suggested_marks, "result": ev.result if ev.status == "ok" else None,
            "requires_review": ev.requires_review, "review_reasons": ev.review_reasons, "checks": ev.checks,
            "error": ev.error, "created_at": ev.created_at.isoformat() + "Z"}


@router.get("/submissions/{submission_id}")
def get_submission(submission_id: str, user: StaffUser, db: DB) -> dict:
    sub, exam = _submission(db, submission_id, user)
    answers = {a.question_id: a for a in db.scalars(select(Answer).where(Answer.submission_id == sub.id))}
    extractions = db.scalars(select(Extraction).where(Extraction.submission_id == sub.id)
                             .order_by(Extraction.created_at.desc()))
    active = db.get(Extraction, sub.active_extraction_id) if sub.active_extraction_id else None
    q_rows = []
    for q in exam.questions:
        a = answers.get(q.id)
        ev = db.get(Evaluation, a.current_evaluation_id) if a and a.current_evaluation_id else None
        if a is not None:
            # a newer failed or invalid attempt is shown so the teacher knows marking did not work
            latest = db.scalars(select(Evaluation).where(Evaluation.answer_id == a.id)
                                .order_by(Evaluation.created_at.desc())).first()
            if latest is not None and latest.status != "ok" and (ev is None or latest.created_at > ev.created_at):
                ev = latest
        rubric = processing.rubric_dict(db, q)
        q_rows.append({
            "question": {"id": q.id, "number": q.number, "text": q.text, "qtype": q.qtype, "max_marks": q.max_marks,
                         "mark_increment": q.mark_increment, "unit": q.unit, "rubric": rubric},
            "answer": None if a is None else {
                "id": a.id, "original_text": a.original_text, "corrected_text": a.corrected_text, "text": a.text,
                "ocr_status": a.ocr_status, "page_refs": a.page_refs, "flags": a.flags,
                "final_marks": a.final_marks, "criterion_marks": a.criterion_marks, "mark_status": a.mark_status,
                "feedback": a.feedback, "approved_at": a.approved_at.isoformat() + "Z" if a.approved_at else None,
            },
            "evaluation": _evaluation_out(ev),
        })
    st = db.get(Student, sub.student_id)
    return {
        **_sub_row(db, sub),
        "exam": {"id": exam.id, "name": exam.name, "subject": exam.subject, "class_name": exam.class_name,
                 "results_status": exam.results_status},
        "student_full": {"name": st.name, "admission_no": st.admission_no, "class_name": st.class_name,
                         "section": st.section, "roll_no": st.roll_no},
        "page_list": [{"id": p.id, "page_no": p.page_no, "width": p.width, "height": p.height, "quality": p.quality}
                      for p in sub.pages],
        "active_extraction": None if active is None else {
            "id": active.id, "provider": active.provider, "model": active.model, "pages": active.pages,
            "warnings": active.warnings, "redacted": active.redacted, "name_on_sheet": active.name_on_sheet,
            "unassigned_text": active.unassigned_text, "usage": active.usage, "duration_ms": active.duration_ms},
        "extractions": [{"id": e.id, "provider": e.provider, "model": e.model, "status": e.status, "error": e.error,
                         "duration_ms": e.duration_ms, "created_at": e.created_at.isoformat() + "Z",
                         "active": e.id == sub.active_extraction_id} for e in extractions],
        "questions": q_rows,
    }


@router.get("/pages/{page_id}/image")
def page_image(page_id: str, user: StaffUser, db: DB) -> Response:
    page = db.get(Page, page_id)
    if page is None:
        raise HTTPException(404, "Page not found.")
    _submission(db, page.submission_id, user)
    from pathlib import Path

    return Response(Path(page.image_path).read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "private, no-store"})


class RotateIn(BaseModel):
    degrees: int


@router.post("/pages/{page_id}/rotate")
def rotate_page(page_id: str, body: RotateIn, user: StaffUser, db: DB) -> dict:
    page = db.get(Page, page_id)
    if page is None:
        raise HTTPException(404, "Page not found.")
    _submission(db, page.submission_id, user)
    try:
        img = rotate(Image.open(page.image_path).convert("RGB"), body.degrees)
    except DocumentError as exc:
        raise HTTPException(400, str(exc)) from exc
    with open(page.image_path, "wb") as fh:
        fh.write(to_png(img))
    page.width, page.height, page.quality = img.width, img.height, quality(img)
    audit(db, user, "page_rotated", "page", page.id, degrees=body.degrees)
    db.commit()
    return {"ok": True, "width": img.width, "height": img.height}


class OrderIn(BaseModel):
    page_ids: list[str]


@router.post("/submissions/{submission_id}/pages/order")
def reorder_pages(submission_id: str, body: OrderIn, user: StaffUser, db: DB) -> dict:
    sub, _ = _submission(db, submission_id, user)
    pages = {p.id: p for p in sub.pages}
    if set(body.page_ids) != set(pages):
        raise HTTPException(400, "Send every page id exactly once.")
    for no, pid in enumerate(body.page_ids, start=1):
        pages[pid].page_no = no + 1000  # avoid transient duplicates
    db.flush()
    for p in pages.values():
        p.page_no -= 1000
    audit(db, user, "pages_reordered", "submission", sub.id)
    db.commit()
    return {"ok": True}


class IdentityIn(BaseModel):
    verified: bool
    student_id: str | None = None


@router.post("/submissions/{submission_id}/identity")
def confirm_identity(submission_id: str, body: IdentityIn, user: StaffUser, db: DB) -> dict:
    sub, exam = _submission(db, submission_id, user)
    if body.student_id and body.student_id != sub.student_id:
        student = db.get(Student, body.student_id)
        if student is None or student.class_name != exam.class_name:
            raise HTTPException(400, "That student is not in this class.")
        if db.scalars(select(Submission).where(Submission.exam_id == exam.id,
                                               Submission.student_id == student.id)).first():
            raise HTTPException(409, "That student already has an answer sheet for this exam.")
        audit(db, user, "student_reassigned", "submission", sub.id, from_student=sub.student_id, to_student=student.id)
        sub.student_id = student.id
    sub.identity_verified = body.verified
    sub.identity_verified_by = user.id if body.verified else None
    audit(db, user, "identity_confirmed" if body.verified else "identity_unconfirmed", "submission", sub.id)
    db.commit()
    return _sub_row(db, sub)


@router.post("/submissions/{submission_id}/process")
def process(submission_id: str, user: StaffUser, db: DB) -> dict:
    """Claude reads the whole sheet, then marks it."""
    sub, _ = _submission(db, submission_id, user)
    return {"job": _job_out(_start(db, sub, user, "process"))}


@router.post("/submissions/{submission_id}/extract")
def extract(submission_id: str, user: StaffUser, db: DB) -> dict:
    sub, _ = _submission(db, submission_id, user)
    return {"job": _job_out(_start(db, sub, user, "extract"))}


class EvaluateIn(BaseModel):
    force: bool = False
    answer_ids: list[str] | None = None


@router.post("/submissions/{submission_id}/evaluate")
def evaluate(submission_id: str, body: EvaluateIn, user: StaffUser, db: DB) -> dict:
    sub, _ = _submission(db, submission_id, user)
    if not sub.active_extraction_id:
        raise HTTPException(409, "Read the sheet first.")
    job, _ = jobs.enqueue(db, "evaluate", sub.id, options=body.model_dump(), user_id=user.id)
    return {"job": _job_out(job)}


@router.post("/submissions/{submission_id}/extractions/{extraction_id}/activate")
def use_extraction(submission_id: str, extraction_id: str, user: StaffUser, db: DB) -> dict:
    sub, _ = _submission(db, submission_id, user)
    ext = db.get(Extraction, extraction_id)
    if ext is None or ext.submission_id != sub.id or ext.status != "done":
        raise HTTPException(404, "Reading not found.")
    processing.activate_extraction(db, sub, ext, user_id=user.id)
    db.commit()
    return {"ok": True}


@router.get("/jobs/{job_id}")
def job_status(job_id: str, user: StaffUser, db: DB) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")
    _submission(db, job.submission_id, user)
    return _job_out(job)


@router.delete("/submissions/{submission_id}")
def delete_submission(submission_id: str, user: StaffUser, db: DB) -> dict:
    sub, _ = _submission(db, submission_id, user)
    if user.role not in ("admin", "coordinator"):
        raise HTTPException(403, "Only an administrator or coordinator can delete an answer sheet.")
    if jobs.active_job(db, sub.id):
        raise HTTPException(409, "The sheet is being processed; wait for it to finish.")
    if db.scalars(select(Answer).where(Answer.submission_id == sub.id, Answer.mark_status == "approved")).first():
        raise HTTPException(409, "Marks on this sheet are approved; reopen them before deleting.")
    import shutil

    shutil.rmtree(settings.upload_dir / sub.id, ignore_errors=True)
    audit(db, user, "sheet_deleted", "submission", sub.id, exam_id=sub.exam_id)
    db.delete(sub)
    db.commit()
    return {"ok": True}


# --- answers: transcription, marks, approval --------------------------------

class TextIn(BaseModel):
    text: str


@router.patch("/answers/{answer_id}/text")
def correct_text(answer_id: str, body: TextIn, user: StaffUser, db: DB) -> dict:
    """Teacher correction of the transcription. The original reading is kept unchanged."""
    answer, _, _ = _answer(db, answer_id, user)
    if answer.mark_status == "approved":
        raise HTTPException(409, "Marks are approved; reopen the answer to edit it.")
    answer.corrected_text = body.text
    answer.corrected_by = user.id
    answer.ocr_status = "verified"
    flags = [f for f in answer.flags if "verify the transcription" not in f]
    if answer.current_evaluation_id and "the transcription changed; re-evaluate" not in flags:
        flags.append("the transcription changed; re-evaluate")
    answer.flags = flags
    audit(db, user, "transcription_corrected", "answer", answer.id, chars=len(body.text))
    db.commit()
    return {"ok": True, "ocr_status": answer.ocr_status}


@router.post("/answers/{answer_id}/verify")
def verify_text(answer_id: str, user: StaffUser, db: DB) -> dict:
    answer, _, _ = _answer(db, answer_id, user)
    answer.ocr_status = "verified"
    answer.flags = [f for f in answer.flags if "verify the transcription" not in f]
    audit(db, user, "transcription_verified", "answer", answer.id)
    db.commit()
    return {"ok": True}


@router.post("/answers/{answer_id}/evaluate")
def evaluate_one(answer_id: str, user: StaffUser, db: DB) -> dict:
    answer, sub, _ = _answer(db, answer_id, user)
    if answer.mark_status == "approved":
        raise HTTPException(409, "Marks are approved; reopen the answer to re-evaluate it.")
    job, created = jobs.enqueue(db, "evaluate", sub.id, options={"answer_ids": [answer.id], "force": True},
                                user_id=user.id)
    if not created:
        raise HTTPException(409, "This sheet is busy; try again when the current job finishes.")
    return {"job": _job_out(job)}


class MarksIn(BaseModel):
    criterion_marks: dict[str, float]
    reason: str = ""
    feedback: str | None = None


@router.put("/answers/{answer_id}/marks")
def set_marks(answer_id: str, body: MarksIn, user: StaffUser, db: DB) -> dict:
    """Teacher marks per criterion. Every change from the current marks is recorded as an override."""
    answer, _, exam = _answer(db, answer_id, user)
    if answer.mark_status == "approved":
        raise HTTPException(409, "Marks are approved; reopen the answer to change them.")
    q = db.get(Question, answer.question_id)
    rubric = processing.rubric_dict(db, q)
    if rubric is None:
        raise HTTPException(409, "Define a rubric for this question first.")
    crits = {c["id"]: c for c in rubric["criteria"]}
    if set(body.criterion_marks) != set(crits):
        raise HTTPException(400, "Give marks for every rubric criterion: " + ", ".join(crits))
    for cid, value in body.criterion_marks.items():
        if value < 0 or value > crits[cid]["max_marks"] + 1e-9:
            raise HTTPException(400, f"{cid}: marks must be between 0 and {crits[cid]['max_marks']}.")
        if not increments_ok(value, q.mark_increment):
            raise HTTPException(400, f"{cid}: marks must be in steps of {q.mark_increment}.")
    total = round(sum(body.criterion_marks.values()), 4)
    if total > q.max_marks + 1e-9:
        raise HTTPException(400, f"The total {total} exceeds the maximum {q.max_marks}.")
    changed = body.criterion_marks != answer.criterion_marks or answer.final_marks != total
    if changed and answer.final_marks is None:
        audit(db, user, "marks_entered", "answer", answer.id, marks=total)  # first marks, nothing overridden
    elif changed:
        if not body.reason.strip():
            raise HTTPException(400, "Give a reason for changing the marks.")
        db.add(MarkOverride(answer_id=answer.id, original_marks=answer.final_marks, new_marks=total,
                            original_criteria=answer.criterion_marks, new_criteria=body.criterion_marks,
                            reason=body.reason.strip(), user_id=user.id))
        audit(db, user, "marks_overridden", "answer", answer.id, original=answer.final_marks, new=total)
    answer.criterion_marks = dict(body.criterion_marks)
    answer.final_marks = total
    if body.feedback is not None:
        answer.feedback = body.feedback.strip()
    if answer.mark_status in ("pending", "suggested"):
        answer.mark_status = "needs_review"  # teacher-set, awaiting approval
    db.commit()
    return {"ok": True, "final_marks": total}


class ApproveIn(BaseModel):
    feedback: str | None = None


@router.post("/answers/{answer_id}/approve")
def approve(answer_id: str, body: ApproveIn, user: StaffUser, db: DB) -> dict:
    answer, sub, exam = _answer(db, answer_id, user)
    if answer.final_marks is None:
        raise HTTPException(409, "There are no marks to approve yet.")
    if answer.ocr_status == "needs_verification":
        raise HTTPException(409, "Verify the transcription first.")
    if not sub.identity_verified:
        raise HTTPException(409, "Confirm the student's identity for this sheet first.")
    if body.feedback is not None:
        answer.feedback = body.feedback.strip()
    answer.mark_status, answer.approved_by, answer.approved_at = "approved", user.id, now()
    audit(db, user, "marks_approved", "answer", answer.id, marks=answer.final_marks)
    statuses = [a.mark_status for a in db.scalars(select(Answer).where(Answer.submission_id == sub.id))]
    if len(statuses) == len(exam.questions) and all(s == "approved" for s in statuses):
        sub.status, sub.status_note = "approved", ""
    else:
        sub.status = "reviewed" if sub.status in ("evaluated", "extracted") else sub.status
    db.commit()
    return {"ok": True, "submission_status": sub.status}


class ReasonIn(BaseModel):
    reason: str


@router.post("/answers/{answer_id}/reopen")
def reopen(answer_id: str, body: ReasonIn, user: StaffUser, db: DB) -> dict:
    answer, sub, exam = _answer(db, answer_id, user)
    if exam.results_status == "published":
        raise HTTPException(409, "Results are published; unpublish them first (coordinator).")
    if not body.reason.strip():
        raise HTTPException(400, "Give a reason.")
    answer.mark_status = "needs_review"
    answer.approved_by = answer.approved_at = None
    if sub.status == "approved":
        sub.status = "reviewed"
    audit(db, user, "marks_reopened", "answer", answer.id, reason=body.reason.strip()[:200])
    db.commit()
    return {"ok": True}


@router.post("/answers/{answer_id}/request-review")
def request_review(answer_id: str, body: ReasonIn, user: StaffUser, db: DB) -> dict:
    answer, _, _ = _answer(db, answer_id, user)
    if answer.mark_status == "approved":
        raise HTTPException(409, "Marks are approved; reopen the answer first.")
    answer.mark_status = "needs_review"
    answer.flags = [*answer.flags, f"further review requested by {user.name}: {body.reason.strip()[:200]}"]
    audit(db, user, "review_requested", "answer", answer.id)
    db.commit()
    return {"ok": True}


@router.get("/answers/{answer_id}/history")
def answer_history(answer_id: str, user: StaffUser, db: DB) -> dict:
    answer, _, _ = _answer(db, answer_id, user)
    evals = db.scalars(select(Evaluation).where(Evaluation.answer_id == answer.id).order_by(Evaluation.created_at.desc()))
    overrides = db.scalars(select(MarkOverride).where(MarkOverride.answer_id == answer.id)
                           .order_by(MarkOverride.created_at.desc()))
    names = {u.id: u.name for u in db.scalars(select(User))}
    return {
        "evaluations": [_evaluation_out(e) for e in evals],
        "overrides": [{"original_marks": o.original_marks, "new_marks": o.new_marks, "reason": o.reason,
                       "original_criteria": o.original_criteria, "new_criteria": o.new_criteria,
                       "by": names.get(o.user_id, o.user_id), "at": o.created_at.isoformat() + "Z"} for o in overrides],
        "original_text": answer.original_text,
        "corrected_text": answer.corrected_text,
    }


# --- students' own published results -----------------------------------------

@router.get("/my/results")
def my_results(user: CurrentUser, db: DB) -> list[dict]:
    if user.role != "student" or not user.student_id:
        raise HTTPException(403, "Only student accounts have personal results.")
    out = []
    subs = db.scalars(select(Submission).where(Submission.student_id == user.student_id))
    for sub in subs:
        exam = db.get(Exam, sub.exam_id)
        if exam.results_status != "published":
            continue  # unpublished results are never shown to students
        answers = {a.question_id: a for a in db.scalars(select(Answer).where(Answer.submission_id == sub.id))}
        out.append({
            "exam": exam.name, "subject": exam.subject, "academic_year": exam.academic_year,
            "total": processing.submission_total(db, sub)["total"], "maximum": sum(q.max_marks for q in exam.questions),
            "questions": [{"number": q.number, "max_marks": q.max_marks,
                           "marks": answers[q.id].final_marks if q.id in answers else None,
                           "feedback": answers[q.id].feedback if q.id in answers else ""} for q in exam.questions],
        })
    return out
