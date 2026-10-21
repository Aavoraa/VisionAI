"""Dashboard sessions: creation, lookup (absolute + idle expiry), revocation, cookies.

The session token lives only in an httpOnly cookie; the database stores its SHA-256.
CSRF protection is synchronizer-token style: a second random token is returned to the
client (body + readable cookie) and must come back in the ``X-CSRF-Token`` header; its hash
is bound to the session row, so a token from another session never validates.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Response
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import generate_token, hash_token
from app.database.models import User, UserSession

# Refresh last_seen_at at most this often, so authenticated reads do not all become writes.
_TOUCH_INTERVAL = timedelta(minutes=5)
MAX_TOKEN_LENGTH = 128


@dataclass(frozen=True, slots=True)
class IssuedSession:
    session: UserSession
    token: str
    csrf_token: str


class SessionExpired(Exception):
    """The session existed but is past its absolute or idle deadline (it has been deleted)."""


def _aware(value: datetime) -> datetime:
    # SQLite returns naive datetimes; everything is stored in UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def create_session(db: AsyncSession, user: User, settings: Settings) -> IssuedSession:
    now = datetime.now(UTC)
    token, token_hash = generate_token()
    csrf_token, csrf_hash = generate_token()
    # Opportunistic cleanup of this user's dead sessions.
    await db.execute(delete(UserSession).where(UserSession.user_id == user.id, UserSession.expires_at <= now))
    session = UserSession(
        user_id=user.id,
        token_hash=token_hash,
        csrf_token_hash=csrf_hash,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=settings.session_ttl_hours),
    )
    db.add(session)
    user.last_login_at = now
    await db.commit()
    return IssuedSession(session=session, token=token, csrf_token=csrf_token)


async def resolve_session(db: AsyncSession, token: str, settings: Settings) -> tuple[UserSession, User] | None:
    """Return the live session for ``token``; ``None`` if unknown; raise ``SessionExpired``."""
    if not token or len(token) > MAX_TOKEN_LENGTH:
        return None
    row = (
        await db.execute(
            select(UserSession, User)
            .join(User, UserSession.user_id == User.id)
            .where(UserSession.token_hash == hash_token(token))
        )
    ).first()
    if row is None:
        return None
    session, user = row

    now = datetime.now(UTC)
    last_seen = _aware(session.last_seen_at)
    idle_deadline = last_seen + timedelta(hours=settings.session_idle_timeout_hours)
    if _aware(session.expires_at) <= now or idle_deadline <= now:
        await db.delete(session)
        await db.commit()
        raise SessionExpired

    if now - last_seen >= _TOUCH_INTERVAL:
        session.last_seen_at = now
        await db.commit()
    return session, user


async def revoke_session(db: AsyncSession, session: UserSession) -> None:
    await db.delete(session)
    await db.commit()


async def revoke_other_sessions(db: AsyncSession, user_id: str, keep_session_id: str) -> None:
    await db.execute(delete(UserSession).where(UserSession.user_id == user_id, UserSession.id != keep_session_id))
    await db.commit()


async def set_password(db: AsyncSession, user: User, password_hash: str) -> None:
    await db.execute(update(User).where(User.id == user.id).values(password_hash=password_hash))
    await db.commit()
    user.password_hash = password_hash


def set_session_cookies(response: Response, issued: IssuedSession, settings: Settings) -> None:
    max_age = settings.session_ttl_hours * 3600
    response.set_cookie(
        settings.session_cookie_name,
        issued.token,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    # Readable by browser code by design (it is echoed in a header, which other sites cannot do).
    response.set_cookie(
        settings.csrf_cookie_name,
        issued.csrf_token,
        max_age=max_age,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookies(response: Response, settings: Settings) -> None:
    for name, httponly in ((settings.session_cookie_name, True), (settings.csrf_cookie_name, False)):
        response.delete_cookie(name, path="/", secure=settings.cookie_secure, httponly=httponly, samesite="lax")
