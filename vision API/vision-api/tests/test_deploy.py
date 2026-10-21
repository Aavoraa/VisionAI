"""Deployment wiring: the client-IP chain (nginx -> dashboard -> API) must stay intact.

The API throttles sign-in attempts per client IP. That is only meaningful if the API trusts
X-Forwarded-For from exactly one peer (the dashboard container) and that header is written by
a proxy that overwrites, rather than appends to, whatever the browser sent.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV = re.compile(r"^\$\{[A-Z_]+:-(?P<default>[^}]+)\}$")


def _resolve(value: str) -> str:
    """Resolve a ``${VAR:-default}`` compose value to its default."""
    match = DEFAULT_ENV.match(value)
    return match.group("default") if match else value


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def nginx_conf() -> str:
    return (ROOT / "deploy" / "nginx" / "dashboard.conf").read_text(encoding="utf-8")


def test_api_trusts_forwarded_headers_only_from_dashboard(compose: dict[str, Any]) -> None:
    services = compose["services"]
    dashboard_ip = _resolve(services["dashboard"]["networks"]["default"]["ipv4_address"])
    trusted = _resolve(services["api"]["environment"]["FORWARDED_ALLOW_IPS"])

    assert trusted == dashboard_ip
    assert "*" not in trusted


def test_dashboard_ip_is_inside_compose_subnet(compose: dict[str, Any]) -> None:
    import ipaddress

    subnet = ipaddress.ip_network(_resolve(compose["networks"]["default"]["ipam"]["config"][0]["subnet"]))
    dashboard_ip = ipaddress.ip_address(
        _resolve(compose["services"]["dashboard"]["networks"]["default"]["ipv4_address"])
    )
    assert dashboard_ip in subnet
    # .1 is the bridge gateway, which is the peer address of connections to published ports.
    assert dashboard_ip != next(subnet.hosts())


def test_dashboard_is_reachable_only_through_proxy(compose: dict[str, Any]) -> None:
    services = compose["services"]
    assert "ports" not in services["dashboard"]
    assert services["dashboard-proxy"]["ports"]
    mounts = services["dashboard-proxy"]["volumes"]
    assert any(m.startswith("./deploy/nginx/dashboard.conf:") and m.endswith(":ro") for m in mounts)


def test_proxy_overwrites_forwarded_for(nginx_conf: str) -> None:
    directives = re.findall(r"proxy_set_header\s+X-Forwarded-For\s+(\S+);", nginx_conf)
    assert directives == ["$remote_addr"]
    # $proxy_add_x_forwarded_for would append to a client-supplied (spoofable) header.
    assert "$proxy_add_x_forwarded_for" not in nginx_conf
