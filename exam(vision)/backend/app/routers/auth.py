# backend/app/routers/auth.py
"""Sign-in, first-run setup, users and students."""

from __future__ import annotations

import csv
import io
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ROLES, Student, User
from ..security import (
    AdminUser, CurrentUser, ManagerUser, audit, create_session, end_session, hash_password,
    validate_new_password, verify_password,
)

router = APIRouter(prefix="/api", tags=["auth"])
DB = Annotated[Session, Depends(get_db)]


def user_out(u: User) -> dict:
    return {"id": u.id, "username": u.username, "name": u.name, "role": u.role, "active": u.active,
            "student_id": u.student_id}


@router.get("/auth/status")
def status(db: DB) -> dict:
    return {"needs_setup": db.scalar(select(func.count(User.id))) == 0}


class SetupIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    password: str


@router.post("/auth/setup")
def setup(body: SetupIn, db: DB) -> dict:
    """Creates the first administrator. Only possible while there are no users."""
    if db.scalar(select(func.count(User.id))):
        raise HTTPException(409, "Setup is already done. Sign in instead.")
    validate_new_password(body.password)
    user = User(username=body.username.strip().lower(), name=body.name.strip(), role="admin",
                password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    audit(db, user, "setup_admin", "user", user.id)
    db.commit()
    return {"token": create_session(db, user), "user": user_out(user)}


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
def login(body: LoginIn, db: DB) -> dict:
    user = db.scalars(select(User).where(User.username == body.username.strip().lower())).first()
    if user is None or not user.active or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Wrong username or password.")
    return {"token": create_session(db, user), "user": user_out(user)}


@router.post("/auth/logout")
def logout(request: Request, user: CurrentUser, db: DB) -> dict:
    end_session(db, request.headers.get("authorization", "")[7:].strip())
    return {"ok": True}


@router.get("/auth/me")
def me(user: CurrentUser) -> dict:
    return user_out(user)


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


@router.post("/auth/password")
def change_password(body: PasswordIn, user: CurrentUser, db: DB) -> dict:
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "The current password is wrong.")
    validate_new_password(body.new_password)
    user.password_hash = hash_password(body.new_password)
    audit(db, user, "password_changed", "user", user.id)
    db.commit()
    return {"ok": True}


# --- users (admin) -----------------------------------------------------------

class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    role: str
    password: str
    student_id: str | None = None


@router.get("/users")
def list_users(_: ManagerUser, db: DB) -> list[dict]:
    return [user_out(u) for u in db.scalars(select(User).order_by(User.role, User.name))]


@router.post("/users")
def create_user(body: UserIn, admin: AdminUser, db: DB) -> dict:
    if body.role not in ROLES:
        raise HTTPException(400, f"Role must be one of {', '.join(ROLES)}.")
    if body.role == "student" and not (body.student_id and db.get(Student, body.student_id)):
        raise HTTPException(400, "A student account must be linked to a student record.")
    validate_new_password(body.password)
    if db.scalars(select(User).where(User.username == body.username.strip().lower())).first():
        raise HTTPException(409, "That username is taken.")
    user = User(username=body.username.strip().lower(), name=body.name.strip(), role=body.role,
                password_hash=hash_password(body.password),
                student_id=body.student_id if body.role == "student" else None)
    db.add(user)
    db.flush()
    audit(db, admin, "user_created", "user", user.id, role=user.role)
    db.commit()
    return user_out(user)


class UserPatch(BaseModel):
    name: str | None = None
    role: str | None = None
    active: bool | None = None
    password: str | None = None


@router.patch("/users/{user_id}")
def update_user(user_id: str, body: UserPatch, admin: AdminUser, db: DB) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found.")
    if user.id == admin.id and (body.active is False or (body.role and body.role != "admin")):
        raise HTTPException(400, "You cannot disable or demote your own account.")
    if body.role:
        if body.role not in ROLES:
            raise HTTPException(400, "Unknown role.")
        user.role = body.role
    if body.name:
        user.name = body.name
    if body.active is not None:
        user.active = body.active
    if body.password:
        validate_new_password(body.password)
        user.password_hash = hash_password(body.password)
    audit(db, admin, "user_updated", "user", user.id, fields=[k for k, v in body.model_dump().items()
                                                            if v is not None and k != "password"]
          + (["password"] if body.password else []))
    db.commit()
    return user_out(user)


# --- students ----------------------------------------------------------------

def student_out(s: Student) -> dict:
    return {"id": s.id, "admission_no": s.admission_no, "name": s.name, "class_name": s.class_name,
            "section": s.section, "roll_no": s.roll_no}


class StudentIn(BaseModel):
    admission_no: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=120)
    class_name: str = Field(min_length=1, max_length=20)
    section: str = ""
    roll_no: str = ""


@router.get("/students")
def list_students(user: CurrentUser, db: DB, class_name: str | None = None) -> list[dict]:
    if user.role == "student":
        raise HTTPException(403, "You do not have permission for this.")
    q = select(Student).order_by(Student.class_name, Student.section, Student.name)
    if class_name:
        q = q.where(Student.class_name == class_name)
    return [student_out(s) for s in db.scalars(q)]


@router.post("/students")
def create_student(body: StudentIn, user: ManagerUser, db: DB) -> dict:
    if db.scalars(select(Student).where(Student.admission_no == body.admission_no.strip())).first():
        raise HTTPException(409, "A student with that admission number exists.")
    s = Student(**{k: v.strip() for k, v in body.model_dump().items()})
    db.add(s)
    db.flush()
    audit(db, user, "student_created", "student", s.id)
    db.commit()
    return student_out(s)


@router.post("/students/import")
def import_students(user: ManagerUser, db: DB, csv_text: str = Body(..., media_type="text/plain")) -> dict:
    """CSV with a header: admission_no,name,class_name,section,roll_no. Existing numbers are updated."""
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    created = updated = 0
    errors = []
    for line, row in enumerate(reader, start=2):
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        if not row.get("admission_no") or not row.get("name") or not row.get("class_name"):
            errors.append(f"line {line}: admission_no, name and class_name are required")
            continue
        s = db.scalars(select(Student).where(Student.admission_no == row["admission_no"])).first()
        if s is None:
            s = Student(admission_no=row["admission_no"], name=row["name"], class_name=row["class_name"])
            db.add(s)
            created += 1
        else:
            updated += 1
        s.name, s.class_name = row["name"], row["class_name"]
        s.section, s.roll_no = row.get("section", ""), row.get("roll_no", "")
    audit(db, user, "students_imported", "student", None, created=created, updated=updated)
    db.commit()
    return {"created": created, "updated": updated, "errors": errors}
