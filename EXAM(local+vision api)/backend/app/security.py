# backend/app/security.py
"""Passwords, login sessions, role checks, encrypted secrets and the audit log."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from typing import Annotated

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import AuditLog, AuthSession, Exam, ExamTeacher, User, now

# --- passwords ---------------------------------------------------------------

_ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS).hex()
    return f"pbkdf2${_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
    return hmac.compare_digest(check, digest)


def validate_new_password(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")


# --- sessions ----------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    db.add(AuthSession(token_hash=_token_hash(token), user_id=user.id,
                       expires_at=now() + dt.timedelta(hours=settings.session_hours)))
    db.commit()
    return token


def end_session(db: Session, token: str) -> None:
    row = db.get(AuthSession, _token_hash(token))
    if row:
        db.delete(row)
        db.commit()


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User:
    token = _bearer(request)
    if not token:
        raise HTTPException(401, "Sign in first.")
    row = db.get(AuthSession, _token_hash(token))
    if row is None or row.expires_at < now():
        raise HTTPException(401, "Your session has expired. Sign in again.")
    user = db.get(User, row.user_id)
    if user is None or not user.active:
        raise HTTPException(401, "This account is disabled.")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def require_roles(*roles: str):
    def check(user: CurrentUser) -> User:
        if user.role not in roles:
            raise HTTPException(403, "You do not have permission for this.")
        return user
    return check


AdminUser = Annotated[User, Depends(require_roles("admin"))]
StaffUser = Annotated[User, Depends(require_roles("admin", "coordinator", "teacher"))]
ManagerUser = Annotated[User, Depends(require_roles("admin", "coordinator"))]


def can_manage(user: User) -> bool:
    return user.role in ("admin", "coordinator")


def exam_for_user(db: Session, exam_id: str, user: User) -> Exam:
    """The exam if ``user`` may work on it (admins/coordinators: all; teachers: assigned)."""
    exam = db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(404, "Exam not found.")
    if can_manage(user):
        return exam
    if user.role == "teacher":
        assigned = db.execute(select(ExamTeacher).where(
            ExamTeacher.exam_id == exam_id, ExamTeacher.user_id == user.id)).first()
        if assigned:
            return exam
    # 404, not 403: do not reveal that the exam exists
    raise HTTPException(404, "Exam not found.")


# --- secrets -----------------------------------------------------------------

def _fernet() -> Fernet:
    path = settings.secret_key_path
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(Fernet.generate_key())
    return Fernet(path.read_bytes())


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None


def mask(value: str | None) -> str | None:
    """What the UI may show of a secret: whether it is set and its last 4 characters."""
    if not value:
        return None
    return "•" * 8 + value[-4:]


# --- audit -------------------------------------------------------------------

def audit(db: Session, user: User | str | None, action: str, entity: str,
          entity_id: str | None = None, **details) -> None:
    """Record an action. Never pass answer text or secrets in ``details``."""
    user_id = user.id if isinstance(user, User) else user
    db.add(AuditLog(user_id=user_id, action=action, entity=entity, entity_id=entity_id, details=details))
