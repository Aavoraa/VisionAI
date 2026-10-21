# backend/app/routers/reports.py
"""Exam reports, CSV export and the audit trail."""

from __future__ import annotations

import csv
import io
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AuditLog, User
from ..security import ManagerUser, StaffUser, exam_for_user
from ..services.reports import exam_report

router = APIRouter(prefix="/api", tags=["reports"])
DB = Annotated[Session, Depends(get_db)]


@router.get("/exams/{exam_id}/report")
def report(exam_id: str, user: StaffUser, db: DB) -> dict:
    return exam_report(db, exam_for_user(db, exam_id, user))


@router.get("/exams/{exam_id}/report.csv")
def report_csv(exam_id: str, user: StaffUser, db: DB) -> Response:
    exam = exam_for_user(db, exam_id, user)
    data = exam_report(db, exam)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["admission_no", "name", "total", "maximum", "percent", "all_approved", "status"])
    for r in data["students"]:
        w.writerow([r["admission_no"], r["name"], r["total"], r["maximum"], r["percent"], r["approved"], r["status"]])
    name = "".join(c if c.isalnum() else "_" for c in exam.name)[:60]
    return Response("﻿" + buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}_results.csv"'})


@router.get("/audit")
def audit_log(user: ManagerUser, db: DB, entity_id: str | None = None, limit: int = 200) -> list[dict]:
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 1000))
    if entity_id:
        q = q.where(AuditLog.entity_id == entity_id)
    names = {u.id: u.name for u in db.scalars(select(User))}
    return [{"at": r.created_at.isoformat() + "Z", "user": names.get(r.user_id or "", r.user_id), "action": r.action,
             "entity": r.entity, "entity_id": r.entity_id, "details": r.details} for r in db.scalars(q)]
