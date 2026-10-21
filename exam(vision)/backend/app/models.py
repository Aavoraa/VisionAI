# backend/app/models.py
"""Database tables.

History is append-only where it matters: rubric versions, extractions, evaluations,
overrides and audit entries are never updated in place. "Current" pointers select
which row is in use.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _id() -> str:
    return uuid.uuid4().hex


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


ROLES = ("admin", "coordinator", "teacher", "student")


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(200))
    student_id: Mapped[str | None] = mapped_column(ForeignKey("students.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)


class Setting(Base):
    """One configuration value. Secrets are encrypted in ``secret`` and never returned."""

    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON, nullable=True)
    secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, onupdate=now)


class Student(Base):
    __tablename__ = "students"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    admission_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    class_name: Mapped[str] = mapped_column(String(20))
    section: Mapped[str] = mapped_column(String(10), default="")
    roll_no: Mapped[str] = mapped_column(String(10), default="")


class Exam(Base):
    __tablename__ = "exams"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String(160))
    academic_year: Mapped[str] = mapped_column(String(20))
    class_name: Mapped[str] = mapped_column(String(20))
    section: Mapped[str] = mapped_column(String(10), default="")
    subject: Mapped[str] = mapped_column(String(60))
    grade_level: Mapped[str] = mapped_column(String(40), default="")
    instructions: Mapped[str] = mapped_column(Text, default="")
    results_status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|reviewed|approved|published
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    questions: Mapped[list[Question]] = relationship(back_populates="exam", order_by="Question.order",
                                                     cascade="all, delete-orphan")


class ExamTeacher(Base):
    __tablename__ = "exam_teachers"
    exam_id: Mapped[str] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)


QUESTION_TYPES = ("short", "long", "math", "science", "diagram", "essay", "comprehension", "translation")


class Question(Base):
    __tablename__ = "questions"
    __table_args__ = (UniqueConstraint("exam_id", "number"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    exam_id: Mapped[str] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True)
    number: Mapped[str] = mapped_column(String(10))  # "1", "2a"
    order: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    qtype: Mapped[str] = mapped_column(String(20), default="short")
    max_marks: Mapped[float] = mapped_column(Float)
    mark_increment: Mapped[float] = mapped_column(Float, default=0.5)
    # optional deterministic check for numerical answers
    numeric_answer: Mapped[str | None] = mapped_column(String(40), nullable=True)
    numeric_tolerance: Mapped[float] = mapped_column(Float, default=0.01)
    unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    current_rubric_version: Mapped[int] = mapped_column(Integer, default=0)

    exam: Mapped[Exam] = relationship(back_populates="questions")


class RubricVersion(Base):
    __tablename__ = "rubric_versions"
    __table_args__ = (UniqueConstraint("question_id", "version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    question_id: Mapped[str] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    reference_answer: Mapped[str] = mapped_column(Text, default="")
    # [{"id": "C1", "name": ..., "max_marks": 1, "description": ...}]
    criteria: Mapped[list] = mapped_column(JSON, default=list)
    expected_concepts: Mapped[list] = mapped_column(JSON, default=list)
    alternatives: Mapped[list] = mapped_column(JSON, default=list)
    partial_credit_rules: Mapped[str] = mapped_column(Text, default="")
    misconceptions: Mapped[list] = mapped_column(JSON, default=list)
    required_elements: Mapped[str] = mapped_column(Text, default="")  # reasoning, formulas, units...
    ignore_criteria: Mapped[str] = mapped_column(Text, default="")  # e.g. "spelling is not assessed"
    subject_rules: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(20), default="teacher")  # teacher | ai_draft
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        UniqueConstraint("exam_id", "student_id"),
        UniqueConstraint("exam_id", "file_sha256"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    exam_id: Mapped[str] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"), index=True)
    # uploaded|extracting|extracted|evaluating|evaluated|needs_attention|reviewed|approved|failed
    status: Mapped[str] = mapped_column(String(20), default="uploaded")
    status_note: Mapped[str] = mapped_column(Text, default="")
    file_sha256: Mapped[str] = mapped_column(String(64))
    original_filename: Mapped[str] = mapped_column(String(200))
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    identity_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    identity_verified_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    active_extraction_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    uploaded_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    pages: Mapped[list[Page]] = relationship(order_by="Page.page_no", cascade="all, delete-orphan")


class Page(Base):
    __tablename__ = "pages"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), index=True)
    page_no: Mapped[int] = mapped_column(Integer)
    image_path: Mapped[str] = mapped_column(String(300))
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    quality: Mapped[dict] = mapped_column(JSON, default=dict)  # {"brightness":..,"sharpness":..,"warnings":[..]}


class Extraction(Base):
    """One Claude reading of a whole submission (every reading is kept)."""

    __tablename__ = "extractions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(20), default="claude")
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|done|failed
    # [{"page_no": 1, "text": "...", "warnings": [...], "uncertain": [...], "confidence": null, "duration_ms": 1}]
    pages: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    # Claude's mapping of answers to questions: {"1": {"text": .., "pages": [1], "uncertain": [..]}}
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    unassigned_text: Mapped[str] = mapped_column(Text, default="")  # text under no exam question
    name_on_sheet: Mapped[str | None] = mapped_column(String(120), nullable=True)
    redacted: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Answer(Base):
    __tablename__ = "answers"
    __table_args__ = (UniqueConstraint("submission_id", "question_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), index=True)
    question_id: Mapped[str] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), index=True)
    extraction_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    original_text: Mapped[str] = mapped_column(Text, default="")  # as read; never overwritten by edits
    corrected_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    page_refs: Mapped[list] = mapped_column(JSON, default=list)
    # unverified|verified|needs_verification
    ocr_status: Mapped[str] = mapped_column(String(24), default="unverified")
    flags: Mapped[list] = mapped_column(JSON, default=list)
    current_evaluation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # final marks: suggested (from AI) until a teacher approves
    final_marks: Mapped[float | None] = mapped_column(Float, nullable=True)
    criterion_marks: Mapped[dict] = mapped_column(JSON, default=dict)
    mark_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|suggested|approved|needs_review
    feedback: Mapped[str] = mapped_column(Text, default="")
    approved_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def text(self) -> str:
        return self.corrected_text if self.corrected_text is not None else self.original_text


class Evaluation(Base):
    __tablename__ = "evaluations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    answer_id: Mapped[str] = mapped_column(ForeignKey("answers.id", ondelete="CASCADE"), index=True)
    rubric_version: Mapped[int] = mapped_column(Integer)
    prompt_version: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(20))  # ok|invalid|failed
    result: Mapped[dict] = mapped_column(JSON, default=dict)  # validated structure
    suggested_marks: Mapped[float | None] = mapped_column(Float, nullable=True)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=True)
    review_reasons: Mapped[list] = mapped_column(JSON, default=list)
    checks: Mapped[list] = mapped_column(JSON, default=list)  # deterministic checks run by the backend
    answer_text_used: Mapped[str] = mapped_column(Text, default="")
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    requested_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class MarkOverride(Base):
    __tablename__ = "mark_overrides"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    answer_id: Mapped[str] = mapped_column(ForeignKey("answers.id", ondelete="CASCADE"), index=True)
    original_marks: Mapped[float | None] = mapped_column(Float, nullable=True)
    new_marks: Mapped[float] = mapped_column(Float)
    original_criteria: Mapped[dict] = mapped_column(JSON, default=dict)
    new_criteria: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text, default="")
    user_id: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_active", "submission_id", "kind", "status"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    kind: Mapped[str] = mapped_column(String(20))  # extract|evaluate|compare
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"))
    provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|done|failed|cancelled
    progress: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    requested_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, onupdate=now)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(60))
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)
