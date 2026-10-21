"""Dashboard accounts: sign up, sign in, sign out, current user, password change."""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AuthenticatedSession, get_current_session
from app.api.docs import NOT_SIGNED_IN, SESSION_FORBIDDEN, error_response
from app.core.config import Settings
from app.core.rate_limit import enforce_login_rate_limit
from app.core.security import hash_password, password_needs_rehash, verify_password
from app.database.database import get_db
from app.database.models import User
from app.schemas.auth import AuthResponse, LoginRequest, PasswordChangeRequest, SignupRequest, UserOut
from app.services.auth_service import (
    clear_session_cookies,
    create_session,
    revoke_other_sessions,
    revoke_session,
    set_password,
    set_session_cookies,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

_AUTH_EXAMPLE = AuthResponse.model_config["json_schema_extra"]["examples"][0]  # type: ignore[index]

_TOO_MANY_ATTEMPTS = error_response(
    "Too many attempts from this IP address or for this email. Wait `Retry-After` seconds.",
    {"throttled": ("Throttled", "rate_limit_error", "Too many sign-in attempts; try again later", None)},
    headers={"Retry-After": {"description": "Seconds to wait.", "schema": {"type": "integer"}}},
)


def _validation_error(examples: dict[str, tuple[str, str, str, str | None]]) -> dict[str, Any]:
    return error_response("A field failed validation; `param` names it.", examples)


_SESSION_COOKIE_HEADERS = {
    "Set-Cookie": {
        "description": (
            "`vision_session` (httpOnly session token) and `vision_csrf` (readable CSRF token); "
            "both `SameSite=Lax`, and `Secure` in production."
        ),
        "schema": {"type": "string"},
    }
}


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.post(
    "/signup",
    status_code=status.HTTP_201_CREATED,
    response_model=AuthResponse,
    summary="Create a dashboard account",
    description=(
        "Creates a `user`-role account and signs it in (sets the session and CSRF cookies). "
        "Enabled by default outside production and disabled in production unless "
        "`VISION_ALLOW_SIGNUP=true`; administrators are created with `scripts/create_admin.py`. "
        "Attempts are throttled per IP address and per email."
    ),
    response_description="Account created and signed in.",
    responses={
        201: {"headers": _SESSION_COOKIE_HEADERS, "content": {"application/json": {"example": _AUTH_EXAMPLE}}},
        403: error_response(
            "Self-service signup is turned off.",
            {"disabled": ("Signup disabled", "permission_error", "Signup is disabled", None)},
        ),
        409: error_response(
            "The email already belongs to an account.",
            {"exists": ("Email taken", "conflict_error", "An account with this email already exists", None)},
        ),
        422: _validation_error(
            {
                "short_password": (
                    "Password too short",
                    "invalid_request_error",
                    "password: String should have at least 10 characters",
                    "password",
                ),
                "bad_email": ("Invalid email", "invalid_request_error", "email: Value error, invalid email address", "email"),
            }
        ),
        429: _TOO_MANY_ATTEMPTS,
    },
)
async def signup(
    payload: SignupRequest,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AuthResponse:
    settings = _settings(request)
    if not settings.signup_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Signup is disabled")
    await enforce_login_rate_limit(request, payload.email)

    conflict = HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists")
    # Includes password-less users created for API keys: signing up must never take them over.
    if (await db.execute(select(User.id).where(User.email == payload.email))).first() is not None:
        raise conflict

    user = User(email=payload.email, role="user", password_hash=await asyncio.to_thread(hash_password, payload.password))
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:  # concurrent signup with the same email
        await db.rollback()
        raise conflict from None

    issued = await create_session(db, user, settings)
    set_session_cookies(response, issued, settings)
    return AuthResponse(user=UserOut.model_validate(user), csrf_token=issued.csrf_token)


@router.post(
    "/login",
    response_model=AuthResponse,
    summary="Sign in",
    description=(
        "Verifies the email and password and starts a session: sets an httpOnly `vision_session` "
        "cookie and a readable `vision_csrf` cookie, and returns the CSRF token. Send it as "
        "`X-CSRF-Token` on every POST/PUT/PATCH/DELETE. Wrong email and wrong password produce "
        "the same error. Attempts are throttled per IP address and per email."
    ),
    response_description="Signed in.",
    responses={
        200: {"headers": _SESSION_COOKIE_HEADERS, "content": {"application/json": {"example": _AUTH_EXAMPLE}}},
        401: error_response(
            "Unknown email or wrong password (deliberately indistinguishable).",
            {"invalid": ("Wrong credentials", "authentication_error", "Invalid email or password", None)},
        ),
        403: error_response(
            "Correct credentials, but the account is deactivated.",
            {"inactive": ("Account deactivated", "permission_error", "User account is inactive", None)},
        ),
        422: _validation_error(
            {"missing_password": ("Password missing", "invalid_request_error", "password: Field required", "password")}
        ),
        429: _TOO_MANY_ATTEMPTS,
    },
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AuthResponse:
    settings = _settings(request)
    await enforce_login_rate_limit(request, payload.email)

    user = (await db.execute(select(User).where(User.email == payload.email))).scalar_one_or_none()
    stored_hash = user.password_hash if user is not None else None
    if not await asyncio.to_thread(verify_password, stored_hash, payload.password) or user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is inactive")

    if stored_hash is not None and password_needs_rehash(stored_hash):
        user.password_hash = await asyncio.to_thread(hash_password, payload.password)

    issued = await create_session(db, user, settings)
    set_session_cookies(response, issued, settings)
    return AuthResponse(user=UserOut.model_validate(user), csrf_token=issued.csrf_token)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out",
    description=(
        "Ends the current session server-side (the token stops working immediately, even if a "
        "copy of the cookie survives) and clears both cookies. Requires `X-CSRF-Token`."
    ),
    response_description="Signed out.",
    responses={401: NOT_SIGNED_IN, 403: SESSION_FORBIDDEN},
)
async def logout(
    request: Request,
    response: Response,
    auth: Annotated[AuthenticatedSession, Depends(get_current_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await revoke_session(db, auth.session)
    clear_session_cookies(response, _settings(request))


@router.get(
    "/me",
    response_model=UserOut,
    summary="Current user",
    description=(
        "Returns the signed-in account. The dashboard calls this on load to decide between the "
        "sign-in page and the app, and to show admin-only navigation for `role=admin`."
    ),
    response_description="The signed-in account.",
    responses={
        200: {"content": {"application/json": {"example": _AUTH_EXAMPLE["user"]}}},
        401: NOT_SIGNED_IN,
        403: error_response(
            "The account was deactivated after signing in.",
            {"inactive": ("Account deactivated", "permission_error", "User account is inactive", None)},
        ),
    },
)
async def me(auth: Annotated[AuthenticatedSession, Depends(get_current_session)]) -> UserOut:
    return UserOut.model_validate(auth.user)


@router.post(
    "/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change password",
    description=(
        "Replaces the password after re-checking the current one. Every other session of the "
        "account is signed out; the current session stays valid. Requires `X-CSRF-Token`."
    ),
    response_description="Password changed.",
    responses={
        400: error_response(
            "The current password is wrong.",
            {"wrong_current": ("Wrong current password", "invalid_request_error", "Current password is incorrect", None)},
        ),
        401: NOT_SIGNED_IN,
        403: SESSION_FORBIDDEN,
        422: _validation_error(
            {
                "weak_new_password": (
                    "New password too short",
                    "invalid_request_error",
                    "new_password: String should have at least 10 characters",
                    "new_password",
                )
            }
        ),
    },
)
async def change_password(
    payload: PasswordChangeRequest,
    auth: Annotated[AuthenticatedSession, Depends(get_current_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    if not await asyncio.to_thread(verify_password, auth.user.password_hash, payload.current_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    await set_password(db, auth.user, await asyncio.to_thread(hash_password, payload.new_password))
    await revoke_other_sessions(db, auth.user.id, keep_session_id=auth.session.id)
