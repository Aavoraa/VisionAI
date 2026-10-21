"""API key hashing and an SSRF-hardened remote image fetcher.

API keys
--------
Keys have the form ``sk_live_<32 urlsafe chars>`` (192 bits of entropy) and are stored only
as their SHA-256 hex digest. Because keys are high-entropy random tokens, a fast hash is
not brute-forceable and allows an indexed O(1) lookup; a slow password hash would add
latency to every request without adding security.

SSRF protection
---------------
* Only allow-listed schemes and ports; no credentials in URLs; bounded URL length.
* The hostname is resolved once and *every* resolved address must be globally routable
  (IPv4-mapped / NAT64 / 6to4 / Teredo embedded addresses are checked too).
* The connection is pinned to the vetted IP (Host header + TLS SNI keep the original
  name) so DNS rebinding between check and connect is impossible.
* Redirects are followed manually and each hop is re-validated.
* Environment proxies/.netrc are ignored, bodies are streamed with a hard byte cap
  (applied after decompression) and the whole operation has a wall-clock deadline.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import re
import secrets
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, status

from app.core.config import Settings, settings

# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

API_KEY_PREFIX = "sk_live_"
API_KEY_SECRET_LENGTH = 32
# Number of leading characters stored in clear so users can tell their keys apart.
API_KEY_DISPLAY_PREFIX_LENGTH = len(API_KEY_PREFIX) + 4
_API_KEY_RE = re.compile(rf"^{API_KEY_PREFIX}[A-Za-z0-9_\-]{{{API_KEY_SECRET_LENGTH}}}$")


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str]:
    """Return ``(raw_key, sha256_hex_of_raw_key)``. The raw key must be shown once and discarded."""
    raw_key = API_KEY_PREFIX + secrets.token_urlsafe(24)  # 24 bytes -> exactly 32 urlsafe chars
    return raw_key, hash_api_key(raw_key)


def api_key_display_prefix(raw_key: str) -> str:
    return raw_key[:API_KEY_DISPLAY_PREFIX_LENGTH]


def is_valid_api_key_format(candidate: str) -> bool:
    return len(candidate) <= 128 and _API_KEY_RE.fullmatch(candidate) is not None


def verify_api_key(raw_key: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(raw_key), stored_hash)


def is_api_key_expired(expires_at: datetime | None) -> bool:
    """Naive datetimes (e.g. from SQLite) are interpreted as UTC."""
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Dashboard accounts: emails, passwords, session tokens
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}$")
PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128

# Argon2id with the library's RFC 9106 "low memory" profile (64 MiB, t=3, p=4).
_password_hasher = PasswordHasher()
# Verified against when the account does not exist, so a failed login costs the same time
# whether or not the email is registered (no user enumeration via timing).
_DUMMY_PASSWORD_HASH = _password_hasher.hash(secrets.token_urlsafe(32))


def normalize_email(email: str) -> str:
    """Trim + lowercase; raises ``ValueError`` for anything that is not a plausible address."""
    normalized = email.strip().lower()
    if not _EMAIL_RE.fullmatch(normalized):
        raise ValueError("invalid email address")
    return normalized


def validate_password(password: str) -> str:
    """Length-only policy (NIST SP 800-63B): long passphrases beat composition rules."""
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        raise ValueError(f"password must be {PASSWORD_MIN_LENGTH}-{PASSWORD_MAX_LENGTH} characters")
    if not password.strip():
        raise ValueError("password must not be blank")
    return password


def hash_password(password: str) -> str:
    """CPU-heavy (~50 ms): call via ``asyncio.to_thread`` from async code."""
    return _password_hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Constant-work check; ``password_hash=None`` (no account / no password) always fails."""
    try:
        if password_hash is None:
            _password_hasher.verify(_DUMMY_PASSWORD_HASH, password)  # same cost as a real check
            return False
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _password_hasher.check_needs_rehash(password_hash)


def generate_token() -> tuple[str, str]:
    """Random 256-bit token for session cookies / CSRF: returns ``(raw, sha256_hex)``."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def tokens_match(raw: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_token(raw), stored_hash)


# ---------------------------------------------------------------------------
# SSRF-safe fetching
# ---------------------------------------------------------------------------

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]

_NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ACCEPTED_CONTENT_TYPES = ("image/", "application/octet-stream", "binary/octet-stream")


class UnsafeURLError(ValueError):
    """The URL is rejected by policy (scheme, port, credentials, or non-public address)."""


class ImageFetchError(Exception):
    """The remote image could not be retrieved. Messages are safe to return to clients."""


class FetchTooLargeError(ImageFetchError):
    """The remote resource exceeds the configured byte-size limit."""


@dataclass(frozen=True, slots=True)
class FetchedImage:
    content: bytes
    content_type: str | None
    final_url: str


@dataclass(frozen=True, slots=True)
class _Target:
    url: httpx.URL
    host: str
    port: int
    addresses: tuple[IPAddress, ...]


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64_PREFIX:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    return None


def is_public_ip(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None:
            return is_public_ip(embedded)
    return (
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
    )


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class SafeImageFetcher:
    def __init__(
        self,
        settings: Settings,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._resolver = resolver or system_resolver
        self._transport = transport

    def validate_url(self, url: str) -> tuple[httpx.URL, str, int]:
        """Static policy checks. Returns (parsed url, ascii host, port)."""
        if not url or len(url) > self._settings.fetch_max_url_length:
            raise UnsafeURLError("URL is empty or too long")
        if any(ch.isspace() or ord(ch) < 0x20 for ch in url):
            raise UnsafeURLError("URL contains whitespace or control characters")
        try:
            parsed = httpx.URL(url)
        except httpx.InvalidURL as exc:
            raise UnsafeURLError("URL is malformed") from exc

        if parsed.scheme not in self._settings.fetch_allowed_schemes:
            raise UnsafeURLError("URL scheme is not allowed")
        if parsed.userinfo:
            raise UnsafeURLError("URLs with embedded credentials are not allowed")
        host = parsed.raw_host.decode("ascii").strip("[]").rstrip(".").lower()
        if not host:
            raise UnsafeURLError("URL has no host")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port not in self._settings.fetch_allowed_ports:
            raise UnsafeURLError("URL port is not allowed")
        return parsed.copy_with(fragment=None), host, port

    async def _resolve(self, url: str) -> _Target:
        parsed, host, port = self.validate_url(url)
        try:
            literal: IPAddress | None = ipaddress.ip_address(host)
        except ValueError:
            literal = None

        if literal is not None:
            raw_addresses = [str(literal)]
        else:
            try:
                raw_addresses = await self._resolver(host, port)
            except (OSError, UnicodeError) as exc:
                raise ImageFetchError("could not resolve image host") from exc
        if not raw_addresses:
            raise ImageFetchError("could not resolve image host")

        addresses: list[IPAddress] = []
        for raw in raw_addresses:
            ip = ipaddress.ip_address(raw.split("%", 1)[0])
            if not is_public_ip(ip):
                raise UnsafeURLError("URL resolves to a non-public address")
            addresses.append(ip)
        return _Target(url=parsed, host=host, port=port, addresses=tuple(addresses))

    def _pinned_request(self, client: httpx.AsyncClient, target: _Target, ip: IPAddress) -> httpx.Request:
        # netloc is IDNA-encoded, bracketed for IPv6, and omits default ports (userinfo is already rejected).
        host_header = target.url.netloc.decode("ascii")
        extensions: dict[str, object] = {}
        if target.url.scheme == "https" and not _is_ip(target.host):
            extensions["sni_hostname"] = target.host
        return client.build_request(
            "GET",
            target.url.copy_with(host=str(ip)),
            headers={"Host": host_header, "Accept": "image/*"},
            extensions=extensions,
        )

    async def _send(self, client: httpx.AsyncClient, target: _Target) -> httpx.Response:
        last_error: httpx.ConnectError | None = None
        for ip in target.addresses:
            try:
                return await client.send(self._pinned_request(client, target, ip), stream=True)
            except httpx.ConnectError as exc:
                last_error = exc
        raise ImageFetchError("could not connect to image host") from last_error

    async def _read_body(self, response: httpx.Response) -> bytes:
        limit = self._settings.max_image_bytes
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            raise FetchTooLargeError("remote image exceeds the maximum allowed size")
        buffer = bytearray()
        async for chunk in response.aiter_bytes():
            buffer.extend(chunk)
            if len(buffer) > limit:
                raise FetchTooLargeError("remote image exceeds the maximum allowed size")
        return bytes(buffer)

    async def fetch(self, url: str) -> FetchedImage:
        timeout = httpx.Timeout(
            connect=self._settings.fetch_connect_timeout,
            read=self._settings.fetch_read_timeout,
            write=self._settings.fetch_read_timeout,
            pool=self._settings.fetch_connect_timeout,
        )
        try:
            async with asyncio.timeout(self._settings.fetch_total_timeout):
                async with httpx.AsyncClient(
                    transport=self._transport,
                    timeout=timeout,
                    follow_redirects=False,
                    trust_env=False,
                    headers={"User-Agent": self._settings.fetch_user_agent},
                ) as client:
                    return await self._fetch_with_redirects(client, url)
        except TimeoutError as exc:
            raise ImageFetchError("timed out fetching remote image") from exc
        except httpx.TimeoutException as exc:
            raise ImageFetchError("timed out fetching remote image") from exc
        except httpx.HTTPError as exc:
            raise ImageFetchError("failed to fetch remote image") from exc

    async def _fetch_with_redirects(self, client: httpx.AsyncClient, url: str) -> FetchedImage:
        current = url
        for _ in range(self._settings.fetch_max_redirects + 1):
            target = await self._resolve(current)
            response = await self._send(client, target)
            try:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise ImageFetchError("remote server sent a redirect without a location")
                    current = str(target.url.join(location))
                    continue
                if response.status_code != 200:
                    raise ImageFetchError(f"remote server responded with HTTP {response.status_code}")
                content_type = response.headers.get("content-type")
                if content_type is not None and not content_type.lower().startswith(_ACCEPTED_CONTENT_TYPES):
                    raise ImageFetchError("remote resource is not an image")
                body = await self._read_body(response)
                return FetchedImage(content=body, content_type=content_type, final_url=str(target.url))
            finally:
                await response.aclose()
        raise ImageFetchError("too many redirects")


async def fetch_image_from_url(
    url: str,
    max_size_mb: int = 10,
    timeout: float = 5.0,
    *,
    resolver: Resolver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bytes:
    """HTTP-facing wrapper around :class:`SafeImageFetcher`.

    Raises ``HTTPException`` 400 for disallowed URLs (non-http(s), private/loopback/
    link-local/metadata addresses, bad ports) or unfetchable resources, and 413 when the
    declared or streamed size exceeds ``max_size_mb``. ``timeout`` bounds the whole fetch,
    including redirects. ``resolver``/``transport`` exist for testing.
    """
    if max_size_mb <= 0 or timeout <= 0:
        raise ValueError("max_size_mb and timeout must be positive")
    config = settings.model_copy(
        update={
            "max_image_bytes": max_size_mb * 1024 * 1024,
            "fetch_total_timeout": timeout,
            "fetch_connect_timeout": min(settings.fetch_connect_timeout, timeout),
            "fetch_read_timeout": min(settings.fetch_read_timeout, timeout),
        }
    )
    fetcher = SafeImageFetcher(config, resolver=resolver, transport=transport)
    try:
        return (await fetcher.fetch(url)).content
    except UnsafeURLError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Image URL rejected: {exc}") from exc
    except FetchTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Remote image exceeds the {max_size_mb} MB limit",
        ) from exc
    except ImageFetchError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Could not fetch image: {exc}") from exc


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
