# backend/app/services/jobs.py
"""Background jobs: reading and marking run off the request thread.

Duplicate prevention: one active job per (submission, kind). On restart, jobs that were
running are marked failed ("interrupted") so a user can retry them; queued jobs run again.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import new_session
from ..models import Job, Submission
from . import processing

log = logging.getLogger(__name__)
KINDS = ("process", "extract", "evaluate")
_executor: ThreadPoolExecutor | None = None


def _pool() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=settings.workers, thread_name_prefix="exam-job")
    return _executor


def active_job(db: Session, submission_id: str, kind: str | None = None) -> Job | None:
    q = select(Job).where(Job.submission_id == submission_id, Job.status.in_(("queued", "running")))
    if kind:
        q = q.where(Job.kind == kind)
    return db.scalars(q).first()


def enqueue(db: Session, kind: str, submission_id: str, *, options: dict | None = None,
            user_id: str | None = None) -> tuple[Job, bool]:
    """Returns (job, created). An equivalent active job is returned instead of a duplicate."""
    existing = active_job(db, submission_id)
    if existing:
        return existing, False
    job = Job(kind=kind, submission_id=submission_id, provider="claude", options=options or {}, requested_by=user_id)
    db.add(job)
    db.commit()
    if settings.run_jobs:
        _pool().submit(_run, job.id)
    else:
        _run(job.id)
    return job, True


def _run(job_id: str) -> None:
    db = new_session()
    try:
        job = db.get(Job, job_id)
        if job is None or job.status not in ("queued",):
            return
        job.status, job.attempts = "running", job.attempts + 1
        db.commit()

        def progress(message: str) -> None:
            job.progress = message
            db.commit()

        sub = db.get(Submission, job.submission_id)
        opts = job.options or {}
        if job.kind in ("extract", "process"):
            ext = processing.run_extraction(db, sub.id, user_id=job.requested_by,
                                            make_active=opts.get("make_active", True), progress=progress)
            if ext.status != "done":
                raise RuntimeError(ext.error or "reading failed")
            if job.kind == "process":
                processing.run_evaluation(db, sub.id, user_id=job.requested_by, progress=progress)
        elif job.kind == "evaluate":
            processing.run_evaluation(db, sub.id, user_id=job.requested_by, answer_ids=opts.get("answer_ids"),
                                      force=bool(opts.get("force")), progress=progress)
        job.progress = "finished"
        job.status = "done"
        db.commit()
    except Exception as exc:  # noqa: BLE001 - a job must always end in a recorded state
        log.exception("job %s failed", job_id)
        db.rollback()
        job = db.get(Job, job_id)
        if job:
            job.status, job.error = "failed", str(exc)[:500]
            sub = db.get(Submission, job.submission_id)
            if sub and sub.status in ("extracting", "evaluating"):
                sub.status, sub.status_note = "needs_attention", str(exc)[:300]
            db.commit()
    finally:
        db.close()


def recover() -> None:
    """Called at startup."""
    db = new_session()
    try:
        for job in db.scalars(select(Job).where(Job.status == "running")):
            job.status, job.error = "failed", "interrupted by a server restart; retry it"
            sub = db.get(Submission, job.submission_id)
            if sub and sub.status in ("extracting", "evaluating"):
                sub.status, sub.status_note = "needs_attention", "interrupted by a server restart; retry"
        db.commit()
        queued = [j.id for j in db.scalars(select(Job).where(Job.status == "queued"))]
    finally:
        db.close()
    for job_id in queued:
        _pool().submit(_run, job_id)
