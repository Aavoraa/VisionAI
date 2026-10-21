"""Redis-backed fixed-window rate limiting, keyed per user.

Each (user, window) bucket is a Redis counter incremented with ``INCR`` and given a TTL
in the same MULTI/EXEC transaction, so the check costs one round trip and counters can
never be left without an expiry. A fixed window can admit up to 2x ``limit`` across a
window boundary; that trade-off buys O(1) memory and a single atomic operation.
"""

import logging
import time
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response, status
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.api.dependencies import get_current_api_key
from app.core.config import Settings
from app.core.security import hash_token
from app.database.models import APIKey, User

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_after: int  # seconds until the current window ends


def create_redis_client(settings: Settings) -> Redis:
    """Create the shared client; no connection is opened until the first command."""
    return Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_timeout=settings.redis_socket_timeout,
        socket_connect_timeout=settings.redis_socket_timeout,
        health_check_interval=30,
    )


async def consume_rate_limit(redis: Redis, user_id: str, limit: int, window_seconds: int) -> RateLimitResult:
    if limit < 1 or window_seconds < 1:
        raise ValueError("limit and window_seconds must be positive")
    now = time.time()
    bucket = int(now // window_seconds)
    key = f"ratelimit:{user_id}:{window_seconds}:{bucket}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, window_seconds + 1)
        count, _ = await pipe.execute()
    count = int(count)
    return RateLimitResult(
        allowed=count <= limit,
        limit=limit,
        remaining=max(0, limit - count),
        reset_after=max(1, window_seconds - int(now % window_seconds)),
    )


async def check_rate_limit(redis: Redis, user_id: str, limit: int = 10, window_seconds: int = 60) -> bool:
    """Count one request for ``user_id``; ``True`` if it is within the limit."""
    return (await consume_rate_limit(redis, user_id, limit, window_seconds)).allowed


async def enforce_login_rate_limit(request: Request, email: str) -> None:
    """Throttle sign-in/sign-up attempts per client IP (3x budget) and per target email.

    Always active, independent of ``rate_limit_enabled`` (that switch governs API traffic);
    Redis outages follow ``rate_limit_fail_open``.
    """
    settings: Settings = request.app.state.settings
    redis: Redis | None = getattr(request.app.state, "redis", None)
    client_ip = request.client.host if request.client else "unknown"
    attempts = settings.login_rate_limit_attempts
    checks = (
        (f"login-ip:{client_ip}", attempts * 3),
        # Hashed so raw email addresses never appear in Redis keys.
        (f"login-email:{hash_token(email)[:32]}", attempts),
    )
    for identity, limit in checks:
        try:
            if redis is None:
                raise RedisError("redis client is not initialised")
            result = await consume_rate_limit(redis, identity, limit, settings.login_rate_limit_window_seconds)
        except (RedisError, OSError) as exc:
            logger.error("login rate limiter unavailable", extra={"error": str(exc)})
            if settings.rate_limit_fail_open:
                return
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Sign-in is temporarily unavailable"
            ) from exc
        if not result.allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many sign-in attempts; try again later",
                headers={"Retry-After": str(result.reset_after)},
            )


async def get_rate_limiter(
    request: Request,
    response: Response,
    auth: Annotated[tuple[APIKey, User], Depends(get_current_api_key)],
) -> None:
    """Dependency: 429 when the authenticated user exceeds the configured limit."""
    settings: Settings = request.app.state.settings
    if not settings.rate_limit_enabled:
        return
    _, user = auth
    redis: Redis | None = getattr(request.app.state, "redis", None)
    try:
        if redis is None:
            raise RedisError("redis client is not initialised")
        result = await consume_rate_limit(
            redis, user.id, settings.rate_limit_requests, settings.rate_limit_window_seconds
        )
    except (RedisError, OSError) as exc:
        logger.error("rate limiter unavailable", extra={"fail_open": settings.rate_limit_fail_open, "error": str(exc)})
        if settings.rate_limit_fail_open:
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Rate limiting is temporarily unavailable"
        ) from exc

    headers = {
        "X-RateLimit-Limit": str(result.limit),
        "X-RateLimit-Remaining": str(result.remaining),
        "X-RateLimit-Reset": str(result.reset_after),
    }
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
            headers={**headers, "Retry-After": str(result.reset_after)},
        )
    response.headers.update(headers)
