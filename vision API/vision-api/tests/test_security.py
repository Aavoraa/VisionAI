import ipaddress
import json
import logging

import httpx
import pytest

from app.core.config import Settings
from app.core.logging import REDACTED, JsonFormatter, redact, scrub
from app.core.security import (
    ImageFetchError,
    SafeImageFetcher,
    UnsafeURLError,
    generate_api_key,
    is_public_ip,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


# ---------------------------------------------------------------------------
# IP classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # CGNAT
        "0.0.0.0",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "fd00:ec2::254",  # AWS IPv6 metadata
        "::ffff:127.0.0.1",  # IPv4-mapped
        "::ffff:169.254.169.254",
        "64:ff9b::7f00:1",  # NAT64 -> 127.0.0.1
        "2002:7f00:1::",  # 6to4 -> 127.0.0.1
        "2001:0:4136:e378:8000:63bf:80ff:fffe",  # Teredo -> 127.0.0.1
    ],
)
def test_non_public_addresses_are_rejected(address: str) -> None:
    assert not is_public_ip(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_public_ip(ipaddress.ip_address(address))


# ---------------------------------------------------------------------------
# SSRF-safe fetcher
# ---------------------------------------------------------------------------


def make_resolver(mapping: dict[str, list[str]]):
    calls: list[str] = []

    async def resolver(host: str, port: int) -> list[str]:
        calls.append(host)
        if host not in mapping:
            raise OSError("NXDOMAIN")
        return mapping[host]

    resolver.calls = calls  # type: ignore[attr-defined]
    return resolver


def make_fetcher(
    settings: Settings,
    handler,
    dns: dict[str, list[str]] | None = None,
) -> SafeImageFetcher:
    return SafeImageFetcher(
        settings,
        resolver=make_resolver(dns or {"images.example.com": ["93.184.216.34"]}),
        transport=httpx.MockTransport(handler),
    )


def never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"transport must not be reached, got {request.url}")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://images.example.com/a.png",
        "file:///etc/passwd",
        "gopher://images.example.com/",
        "https://user:pass@images.example.com/a.png",
        "https://images.example.com:8080/a.png",
        "https://images.example.com:22/a.png",
        "https:///a.png",
        "https://images.example.com/a b.png",
        "https://images.example.com/\r\nX-Injected: 1",
        "https://images.example.com/" + "a" * 3000,
        "",
    ],
)
def test_validate_url_rejects_unsafe_urls(settings: Settings, url: str) -> None:
    fetcher = make_fetcher(settings, never_called)
    with pytest.raises(UnsafeURLError):
        fetcher.validate_url(url)


@pytest.mark.anyio
async def test_fetch_pins_connection_to_vetted_ip(settings: Settings) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG_BYTES)

    fetcher = make_fetcher(settings, handler)
    result = await fetcher.fetch("https://images.example.com/cat.png?size=large#frag")

    assert result.content == PNG_BYTES
    assert result.content_type == "image/png"
    request = seen[0]
    assert request.url.host == "93.184.216.34"
    assert request.url.path == "/cat.png"
    assert request.url.query == b"size=large"
    assert request.headers["host"] == "images.example.com"
    assert request.extensions["sni_hostname"] == "images.example.com"


@pytest.mark.anyio
async def test_fetch_keeps_non_default_port_in_host_header(settings: Settings) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG_BYTES)

    custom = settings.model_copy(update={"fetch_allowed_ports": frozenset({8443})})
    await make_fetcher(custom, handler).fetch("https://images.example.com:8443/a.png")
    assert seen[0].headers["host"] == "images.example.com:8443"
    assert seen[0].url.port == 8443


@pytest.mark.anyio
async def test_fetch_supports_ipv6_public_address(settings: Settings) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG_BYTES)

    fetcher = make_fetcher(settings, handler, {"v6.example.com": ["2606:4700:4700::1111"]})
    await fetcher.fetch("https://v6.example.com/a.png")
    assert seen[0].url.host == "2606:4700:4700::1111"
    assert seen[0].headers["host"] == "v6.example.com"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "resolved",
    [["127.0.0.1"], ["10.1.2.3"], ["169.254.169.254"], ["93.184.216.34", "192.168.0.1"], ["::1"]],
)
async def test_fetch_rejects_hosts_resolving_to_private_addresses(settings: Settings, resolved: list[str]) -> None:
    fetcher = make_fetcher(settings, never_called, {"evil.example.com": resolved})
    with pytest.raises(UnsafeURLError):
        await fetcher.fetch("https://evil.example.com/a.png")


@pytest.mark.anyio
@pytest.mark.parametrize("url", ["http://127.0.0.1/a.png", "http://[::1]/a.png", "http://169.254.169.254/latest/meta-data/"])
async def test_fetch_rejects_private_ip_literals_without_dns(settings: Settings, url: str) -> None:
    resolver = make_resolver({})
    fetcher = SafeImageFetcher(settings, resolver=resolver, transport=httpx.MockTransport(never_called))
    with pytest.raises(UnsafeURLError):
        await fetcher.fetch(url)
    assert resolver.calls == []  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_fetch_unresolvable_host(settings: Settings) -> None:
    fetcher = make_fetcher(settings, never_called, {})
    with pytest.raises(ImageFetchError, match="resolve"):
        await fetcher.fetch("https://nope.example.com/a.png")


@pytest.mark.anyio
async def test_redirect_to_private_address_is_blocked(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    fetcher = make_fetcher(settings, handler)
    with pytest.raises(UnsafeURLError):
        await fetcher.fetch("https://images.example.com/a.png")


@pytest.mark.anyio
async def test_redirect_to_host_resolving_privately_is_blocked(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "https://internal.example.com/secret"})

    fetcher = make_fetcher(
        settings,
        handler,
        {"images.example.com": ["93.184.216.34"], "internal.example.com": ["10.0.0.8"]},
    )
    with pytest.raises(UnsafeURLError):
        await fetcher.fetch("https://images.example.com/a.png")


@pytest.mark.anyio
async def test_safe_relative_redirect_is_followed(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old.png":
            return httpx.Response(302, headers={"location": "/new.png"})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG_BYTES)

    result = await make_fetcher(settings, handler).fetch("https://images.example.com/old.png")
    assert result.content == PNG_BYTES
    assert result.final_url == "https://images.example.com/new.png"


@pytest.mark.anyio
async def test_too_many_redirects(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/loop.png"})

    with pytest.raises(ImageFetchError, match="redirects"):
        await make_fetcher(settings, handler).fetch("https://images.example.com/loop.png")


@pytest.mark.anyio
async def test_declared_oversized_body_is_rejected(settings: Settings) -> None:
    small = settings.model_copy(update={"max_image_bytes": 100})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"x" * 101)

    with pytest.raises(ImageFetchError, match="size"):
        await make_fetcher(small, handler).fetch("https://images.example.com/a.png")


@pytest.mark.anyio
async def test_streamed_oversized_body_without_length_is_rejected(settings: Settings) -> None:
    small = settings.model_copy(update={"max_image_bytes": 100})

    async def chunks():
        for _ in range(10):
            yield b"x" * 50

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/png"}, content=chunks())

    with pytest.raises(ImageFetchError, match="size"):
        await make_fetcher(small, handler).fetch("https://images.example.com/a.png")


@pytest.mark.anyio
async def test_non_image_content_type_is_rejected(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html></html>")

    with pytest.raises(ImageFetchError, match="not an image"):
        await make_fetcher(settings, handler).fetch("https://images.example.com/a.png")


@pytest.mark.anyio
async def test_upstream_error_status(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(ImageFetchError, match="404"):
        await make_fetcher(settings, handler).fetch("https://images.example.com/a.png")


@pytest.mark.anyio
async def test_connect_error_is_wrapped(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ImageFetchError, match="connect"):
        await make_fetcher(settings, handler).fetch("https://images.example.com/a.png")


@pytest.mark.anyio
async def test_timeout_is_wrapped(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ImageFetchError, match="timed out"):
        await make_fetcher(settings, handler).fetch("https://images.example.com/a.png")


# ---------------------------------------------------------------------------
# Log redaction
# ---------------------------------------------------------------------------


def test_scrub_removes_api_keys_bearer_tokens_and_url_credentials() -> None:
    key, _ = generate_api_key()
    text = f"key={key} auth=Bearer abc.def.ghi dsn=postgresql://admin:hunter2@db:5432/x"
    cleaned = scrub(text)
    assert key not in cleaned
    assert "abc.def.ghi" not in cleaned
    assert "hunter2" not in cleaned
    assert REDACTED in cleaned


def test_redact_masks_sensitive_keys_recursively() -> None:
    data = {"Authorization": "Bearer x", "nested": {"api_key": "k", "ok": 1}, "items": [{"password": "p"}]}
    assert redact(data) == {
        "Authorization": REDACTED,
        "nested": {"api_key": REDACTED, "ok": 1},
        "items": [{"password": REDACTED}],
    }


def test_redact_keeps_token_counts_but_hides_tokens() -> None:
    data = {"input_tokens": 12, "output_tokens": 3, "max_tokens": 100, "token": "t", "access_token": "a", "x-auth-token": "x"}
    assert redact(data) == {
        "input_tokens": 12,
        "output_tokens": 3,
        "max_tokens": 100,
        "token": REDACTED,
        "access_token": REDACTED,
        "x-auth-token": REDACTED,
    }


def test_httpx_url_logging_is_suppressed() -> None:
    from app.core.logging import configure_logging

    configure_logging("DEBUG")
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpcore").isEnabledFor(logging.INFO)


def test_json_formatter_redacts_extras_and_message() -> None:
    key, _ = generate_api_key()
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "got key %s", (key,), None)
    record.api_key = "super-secret"
    record.path = "/v1/vision"
    payload = json.loads(JsonFormatter().format(record))
    assert key not in payload["msg"]
    assert payload["api_key"] == REDACTED
    assert payload["path"] == "/v1/vision"
    assert payload["level"] == "INFO"
