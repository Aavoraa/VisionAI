"""FastAPI dependencies: API-key auth, dashboard session auth, services."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_api_key, is_api_key_expired, is_valid_api_key_format, tokens_match
from app.database.database import get_db
from app.database.models import APIKey, User, UserSession
from app.services.auth_service import MAX_TOKEN_LENGTH, SessionExpired, resolve_session
from app.services.billing_service import PlanPricing, QuotaExceeded, check_quota
from app.services.vision_service import VisionService

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}
_KEY_TOUCH_INTERVAL = timedelta(minutes=5)
_bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="APIKey",
    description="Send your API key as `Authorization: Bearer sk_live_...`.",
)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail, headers=_UNAUTHORIZED_HEADERS)


def _extract_bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization")
    if not authorization:
        raise _unauthorized("Missing API key")
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise _unauthorized("Invalid authorization header")
    if not is_valid_api_key_format(token):
        raise _unauthorized("Invalid API key")
    return token


async def get_current_api_key(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _bearer: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
) -> tuple[APIKey, User]:
    # ``_bearer`` only registers the scheme in OpenAPI (Swagger "Authorize" button);
    # the header is parsed and validated strictly below.
    token = _extract_bearer_token(request)

    result = await db.execute(
        select(APIKey, User).join(User, APIKey.user_id == User.id).where(APIKey.key_hash == hash_api_key(token))
    )
    row = result.first()
    if row is None:
        raise _unauthorized("Invalid API key")
    api_key, user = row

    if not api_key.is_active:
        raise _unauthorized("API key has been revoked")
    if is_api_key_expired(api_key.expires_at):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key has expired")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is inactive")

    now = datetime.now(UTC)
    last_used = api_key.last_used_at
    if last_used is not None and last_used.tzinfo is None:  # SQLite returns naive UTC
        last_used = last_used.replace(tzinfo=UTC)
    if last_used is None or now - last_used >= _KEY_TOUCH_INTERVAL:
        api_key.last_used_at = now
        await db.commit()

    # Exposed for downstream logging/usage tracking; never the raw key.
    request.state.api_key_id = api_key.id
    request.state.user_id = user.id
    return api_key, user


# ---------------------------------------------------------------------------
# Dashboard sessions (cookie auth). API keys never authenticate dashboard routes and
# session cookies never authenticate /v1/vision.
# ---------------------------------------------------------------------------

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_session_cookie_scheme = APIKeyCookie(
    name="vision_session",
    auto_error=False,
    scheme_name="SessionCookie",
    description=(
        "Dashboard session cookie set by `POST /v1/auth/login`. State-changing requests must "
        "also send the `X-CSRF-Token` header."
    ),
)


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    session: UserSession
    user: User


async def get_current_session(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _cookie: str | None = Security(_session_cookie_scheme),
) -> AuthenticatedSession:
    """Resolve the session cookie; on unsafe methods also enforce the CSRF token."""
    settings = request.app.state.settings
    try:
        resolved = await resolve_session(db, request.cookies.get(settings.session_cookie_name, ""), settings)
    except SessionExpired:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired; please sign in again"
        ) from None
    if resolved is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in")
    session, user = resolved
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is inactive")

    if request.method not in _SAFE_METHODS:
        csrf = request.headers.get("x-csrf-token", "")
        if not csrf or len(csrf) > MAX_TOKEN_LENGTH or not tokens_match(csrf, session.csrf_token_hash):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing or invalid CSRF token")

    request.state.user_id = user.id
    return AuthenticatedSession(session=session, user=user)


async def require_admin(auth: AuthenticatedSession = Depends(get_current_session)) -> AuthenticatedSession:
    if not auth.user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required")
    return auth


async def enforce_quota(
    auth: tuple[APIKey, User] = Depends(get_current_api_key),
    db: AsyncSession = Depends(get_db),
) -> PlanPricing:
    """402 when the caller's monthly plan allowance is used up; else the prices to bill at."""
    _, user = auth
    try:
        plan = await check_quota(db, user)
    except QuotaExceeded as exc:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc)) from exc
    return PlanPricing.from_plan(plan)


def get_vision_service(request: Request) -> VisionService:
    """The service (and its model) is created once in the app lifespan, never per request."""
    service: VisionService | None = getattr(request.app.state, "vision_service", None)
    if service is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Vision model is not loaded")
    return service
