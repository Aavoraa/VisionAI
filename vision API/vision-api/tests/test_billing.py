"""Plans, per-request pricing, monthly quotas and billing overviews (D3)."""

import io
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import Job, Plan, UsageLog, User
from app.models.base import ModelOutput
from app.models.local_transformers import MockVisionModel
from app.services.billing_service import (
    DEFAULT_PLAN_NAME,
    BillingPeriod,
    PlanPricing,
    QuotaExceeded,
    billing_overview,
    check_quota,
    ensure_default_plan,
    micros_to_usd,
    usd_to_micros,
)
from conftest import ClientFactory
from test_dashboard import Account, add_key, bearer, sign_in

pytestmark = pytest.mark.anyio

VISION = "/v1/vision"
PLANS = "/v1/admin/plans"
USERS = "/v1/admin/users"
BILLING = "/v1/dashboard/billing"
MISSING_ID = "00000000-0000-4000-8000-000000000000"


def error(response: httpx.Response) -> dict[str, Any]:
    return response.json()["error"]


def png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buffer, format="PNG")
    return buffer.getvalue()


async def call_vision(client: httpx.AsyncClient, secret: str, images: int = 1) -> httpx.Response:
    return await client.post(
        VISION,
        headers=bearer(secret),
        data={"prompt": "describe this"},
        files=[("images", (f"{i}.png", png(), "image/png")) for i in range(images)],
    )


async def rows(sessionmaker: async_sessionmaker[AsyncSession], model: type[Any]) -> list[Any]:
    async with sessionmaker() as session:
        return list((await session.execute(select(model))).scalars())


async def add_plan(sessionmaker: async_sessionmaker[AsyncSession], **values: Any) -> str:
    defaults: dict[str, Any] = {
        "name": f"plan-{uuid.uuid4().hex[:8]}",
        "price_input_per_1k_micros": 1000,
        "price_output_per_1k_micros": 2000,
        "price_per_image_micros": 3000,
    }
    async with sessionmaker() as session:
        plan = Plan(**{**defaults, **values})
        session.add(plan)
        await session.commit()
        return plan.id


async def assign(sessionmaker: async_sessionmaker[AsyncSession], user_id: str, plan_id: str | None) -> None:
    async with sessionmaker() as session:
        user = await session.get(User, user_id)
        assert user is not None
        user.plan_id = plan_id
        await session.commit()


async def add_charge(
    sessionmaker: async_sessionmaker[AsyncSession],
    user_id: str,
    key_id: str,
    when: datetime,
    cost_micros: int,
    status: str = "success",
) -> None:
    async with sessionmaker() as session:
        session.add(
            UsageLog(
                user_id=user_id,
                api_key_id=key_id,
                request_id=uuid.uuid4().hex,
                model="m",
                input_tokens=100,
                output_tokens=10,
                image_count=1,
                status=status,
                processing_time_ms=1,
                cost_micros=cost_micros,
                created_at=when,
            )
        )
        await session.commit()


@pytest.fixture
async def alice(settings: Any, make_client: ClientFactory, db_sessionmaker: async_sessionmaker[AsyncSession]) -> Any:
    async with make_client(settings) as client:
        yield await sign_in(client, db_sessionmaker, "alice@example.com")


@pytest.fixture
async def admin_and_alice(
    settings: Any, make_client: ClientFactory, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> Any:
    async with make_client(settings) as a, make_client(settings) as b:
        root = await sign_in(a, db_sessionmaker, "root@example.com", admin=True)
        alice = await sign_in(b, db_sessionmaker, "alice@example.com")
        yield root, alice


# ===========================================================================
# Money and periods
# ===========================================================================


def test_cost_is_computed_from_per_1k_and_per_image_prices() -> None:
    pricing = PlanPricing(plan_id="p", input_per_1k_micros=500, output_per_1k_micros=1500, per_image_micros=2000)
    assert pricing.cost_micros(1000, 1000, 2) == 500 + 1500 + 4000
    assert pricing.cost_micros(0, 0, 0) == 0
    assert pricing.cost_micros(1, 0, 0) == 1  # 0.5 micro-USD rounds half-up
    assert pricing.cost_micros(259, 11, 1) == (259 * 500 + 11 * 1500 + 500) // 1000 + 2000


@pytest.mark.parametrize(
    ("usd", "micros"),
    [(Decimal("0.0005"), 500), (Decimal("25"), 25_000_000), (Decimal("0.000001"), 1), (Decimal("0"), 0)],
)
def test_usd_micro_conversion_is_exact(usd: Decimal, micros: int) -> None:
    assert usd_to_micros(usd) == micros
    assert micros_to_usd(micros) == usd
    assert str(micros_to_usd(500)) == "0.000500"


@pytest.mark.parametrize(
    ("moment", "start", "end", "resets"),
    [
        (datetime(2026, 9, 24, 12, tzinfo=UTC), date(2026, 9, 1), date(2026, 9, 30), datetime(2026, 10, 1, tzinfo=UTC)),
        (datetime(2026, 12, 31, 23, 59, tzinfo=UTC), date(2026, 12, 1), date(2026, 12, 31), datetime(2027, 1, 1, tzinfo=UTC)),
        (datetime(2028, 2, 10, tzinfo=UTC), date(2028, 2, 1), date(2028, 2, 29), datetime(2028, 3, 1, tzinfo=UTC)),
    ],
)
def test_billing_period_is_the_utc_calendar_month(moment: datetime, start: date, end: date, resets: datetime) -> None:
    period = BillingPeriod.containing(moment)
    assert (period.start, period.end, period.resets_at) == (start, end, resets)


def test_previous_period_crosses_years() -> None:
    assert BillingPeriod.containing(datetime(2027, 1, 5, tzinfo=UTC)).previous().start == date(2026, 12, 1)


# ===========================================================================
# Default plan
# ===========================================================================


async def test_default_plan_is_created_once(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with db_sessionmaker() as session:
        first = await ensure_default_plan(session)
        second = await ensure_default_plan(session)
    assert first.id == second.id and first.name == DEFAULT_PLAN_NAME and first.is_default
    assert len(await rows(db_sessionmaker, Plan)) == 1


async def test_lost_default_flag_is_restored(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    plan_id = await add_plan(db_sessionmaker, name=DEFAULT_PLAN_NAME, is_default=False)
    async with db_sessionmaker() as session:
        restored = await ensure_default_plan(session)
    assert restored.id == plan_id and restored.is_default


async def test_only_one_default_plan_can_exist(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    from sqlalchemy.exc import IntegrityError

    await add_plan(db_sessionmaker, is_default=True)
    with pytest.raises(IntegrityError):
        await add_plan(db_sessionmaker, is_default=True)


# ===========================================================================
# Charging requests
# ===========================================================================


async def test_successful_request_is_charged_at_plan_prices(
    alice: Account, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    secret = (await alice.create_key())["secret"]
    response = await call_vision(alice.client, secret, images=2)
    assert response.status_code == 200
    usage = response.json()["usage"]

    (log,) = await rows(db_sessionmaker, UsageLog)
    (plan,) = await rows(db_sessionmaker, Plan)  # the default plan, created on first use
    expected = PlanPricing.from_plan(plan).cost_micros(usage["input_tokens"], usage["output_tokens"], 2)
    assert log.cost_micros == expected > 0


async def test_failed_requests_are_free(alice: Account, db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    secret = (await alice.create_key())["secret"]
    response = await alice.client.post(
        VISION, headers=bearer(secret), data={"prompt": "x"}, files=[("images", ("a.jpg", b"not an image", "image/jpeg"))]
    )
    assert response.status_code == 400
    (log,) = await rows(db_sessionmaker, UsageLog)
    assert log.status == "error" and log.cost_micros == 0


async def test_async_jobs_are_charged_too(alice: Account, db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    secret = (await alice.create_key())["secret"]
    response = await alice.client.post(
        VISION, headers=bearer(secret), data={"prompt": "x", "mode": "async"}, files=[("images", ("a.png", png(), "image/png"))]
    )
    assert response.status_code == 202
    (log,) = await rows(db_sessionmaker, UsageLog)
    assert log.status == "success" and log.cost_micros > 0


async def test_price_changes_do_not_rewrite_past_charges(
    admin_and_alice: tuple[Account, Account], db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    root, alice = admin_and_alice
    secret = (await alice.create_key())["secret"]
    await call_vision(alice.client, secret)
    (before,) = await rows(db_sessionmaker, UsageLog)

    default_id = (await root.client.get(PLANS)).json()["items"][0]["id"]
    changed = await root.client.patch(
        f"{PLANS}/{default_id}", headers=root.csrf, json={"price_per_image_usd": "1.000000"}
    )
    assert changed.status_code == 200
    await call_vision(alice.client, secret)

    logs = sorted(await rows(db_sessionmaker, UsageLog), key=lambda log: log.created_at)
    assert logs[0].cost_micros == before.cost_micros  # unchanged
    assert logs[1].cost_micros == before.cost_micros - 2000 + 1_000_000  # new image price


# ===========================================================================
# Quotas
# ===========================================================================


async def test_request_quota_blocks_with_402_and_does_not_bill(
    alice: Account, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    class CountingModel(MockVisionModel):
        calls = 0

        async def analyze(self, images: list[bytes], prompt: str, options: dict[str, Any] | None = None) -> ModelOutput:
            CountingModel.calls += 1
            return await super().analyze(images, prompt, options)

    await assign(db_sessionmaker, alice.user_id, await add_plan(db_sessionmaker, monthly_request_limit=2))
    secret = (await alice.create_key())["secret"]
    assert (await call_vision(alice.client, secret)).status_code == 200
    assert (await call_vision(alice.client, secret)).status_code == 200
    blocked = await call_vision(alice.client, secret)

    assert blocked.status_code == 402
    err = error(blocked)
    resets = BillingPeriod.containing(datetime.now(UTC)).resets_at.date().isoformat()
    assert err == {
        "type": "quota_exceeded_error",
        "message": f"Monthly request quota of 2 reached; it resets on {resets}",
        "param": None,
    }
    assert len(await rows(db_sessionmaker, UsageLog)) == 2  # the blocked request was not recorded


async def test_spend_limit_blocks(alice: Account, db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    await assign(db_sessionmaker, alice.user_id, await add_plan(db_sessionmaker, monthly_spend_limit_micros=1_500_000))
    key_id = await add_key(db_sessionmaker, alice.user_id, "k")
    await add_charge(db_sessionmaker, alice.user_id, key_id, datetime.now(UTC) - timedelta(seconds=5), 1_500_000)
    secret = (await alice.create_key())["secret"]
    blocked = await call_vision(alice.client, secret)
    assert blocked.status_code == 402
    assert error(blocked)["message"].startswith("Monthly spend limit of $1.5 reached; it resets on ")


async def test_quota_counts_only_the_current_month(
    alice: Account, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await assign(db_sessionmaker, alice.user_id, await add_plan(db_sessionmaker, monthly_request_limit=1))
    key_id = await add_key(db_sessionmaker, alice.user_id, "k")
    last_month = BillingPeriod.containing(datetime.now(UTC)).since - timedelta(days=1)
    for _ in range(3):
        await add_charge(db_sessionmaker, alice.user_id, key_id, last_month, 10)
    secret = (await alice.create_key())["secret"]
    assert (await call_vision(alice.client, secret)).status_code == 200


async def test_in_flight_async_jobs_count_towards_the_request_quota(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    plan_id = await add_plan(db_sessionmaker, monthly_request_limit=2)
    async with db_sessionmaker() as session:
        user = User(email="q@example.com", plan_id=plan_id)
        session.add(user)
        await session.commit()
    key_id = await add_key(db_sessionmaker, user.id, "k")
    async with db_sessionmaker() as session:
        session.add_all([Job(user_id=user.id, api_key_id=key_id, status=s) for s in ("pending", "processing", "completed")])
        await session.commit()
        with pytest.raises(QuotaExceeded):
            await check_quota(session, user)


async def test_unlimited_plans_never_block(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    plan_id = await add_plan(db_sessionmaker)  # both limits NULL
    async with db_sessionmaker() as session:
        user = User(email="u@example.com", plan_id=plan_id)
        session.add(user)
        await session.commit()
    key_id = await add_key(db_sessionmaker, user.id, "k")
    for _ in range(5):
        await add_charge(db_sessionmaker, user.id, key_id, datetime.now(UTC), 10**9)
    async with db_sessionmaker() as session:
        assert (await check_quota(session, user)).id == plan_id


# ===========================================================================
# Billing overview
# ===========================================================================


async def test_billing_overview_totals_projection_and_history(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    plan_id = await add_plan(db_sessionmaker, monthly_request_limit=10, monthly_spend_limit_micros=5_000_000)
    async with db_sessionmaker() as session:
        user = User(email="b@example.com", plan_id=plan_id)
        session.add(user)
        await session.commit()
    key_id = await add_key(db_sessionmaker, user.id, "k")
    now = datetime(2026, 9, 16, tzinfo=UTC)  # exactly halfway through September
    await add_charge(db_sessionmaker, user.id, key_id, datetime(2026, 9, 2, tzinfo=UTC), 1_000_000)
    await add_charge(db_sessionmaker, user.id, key_id, datetime(2026, 9, 3, tzinfo=UTC), 0, status="error")
    await add_charge(db_sessionmaker, user.id, key_id, datetime(2026, 7, 10, tzinfo=UTC), 2_500_000)

    async with db_sessionmaker() as session:
        overview = await billing_overview(session, user, now=now)

    assert overview["period"]["start"] == date(2026, 9, 1)
    assert overview["usage"]["requests"] == 2
    assert overview["cost"]["total_usd"] == Decimal("1.000000")
    assert overview["cost"]["projected_usd"] == Decimal("2.000000")
    assert overview["quota"] == {
        "request_limit": 10,
        "requests_used": 2,
        "requests_remaining": 8,
        "spend_limit_usd": Decimal("5.000000"),
        "spend_used_usd": Decimal("1.000000"),
        "spend_remaining_usd": Decimal("4.000000"),
    }
    assert [m["month"] for m in overview["history"]] == ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
    assert {m["month"]: m["cost_usd"] for m in overview["history"]}["2026-07"] == Decimal("2.500000")


async def test_billing_endpoints_for_dashboard_and_api_clients(alice: Account) -> None:
    secret = (await alice.create_key())["secret"]
    await call_vision(alice.client, secret)

    dashboard = await alice.client.get(BILLING)
    api = await alice.client.get("/v1/usage", headers=bearer(secret))
    assert dashboard.status_code == api.status_code == 200
    body = dashboard.json()
    assert body == api.json()
    assert body["plan"]["name"] == DEFAULT_PLAN_NAME
    assert body["plan"]["price_per_image_usd"] == "0.002000"  # exact decimal strings
    assert body["usage"]["requests"] == 1
    assert Decimal(body["cost"]["total_usd"]) > 0
    assert body["quota"]["requests_remaining"] == 4999
    assert len(body["history"]) == 6 and body["history"][-1]["requests"] == 1
    assert datetime.fromisoformat(body["period"]["resets_at"]).day == 1


async def test_usage_endpoint_requires_an_api_key(alice: Account) -> None:
    assert (await alice.client.get("/v1/usage")).status_code == 401  # session cookie is not enough


async def test_usage_reports_include_cost(alice: Account) -> None:
    secret = (await alice.create_key())["secret"]
    await call_vision(alice.client, secret)
    report = (await alice.client.get("/v1/dashboard/usage", params={"days": 1})).json()
    billing = (await alice.client.get(BILLING)).json()
    assert report["totals"]["cost_usd"] == billing["cost"]["total_usd"]
    assert report["daily"][-1]["cost_usd"] == billing["cost"]["total_usd"]


# ===========================================================================
# Admin: plans
# ===========================================================================

PRO = {
    "name": "Pro",
    "price_per_1k_input_tokens_usd": "0.0004",
    "price_per_1k_output_tokens_usd": "0.0012",
    "price_per_image_usd": "0.0015",
    "monthly_request_limit": 100000,
}


async def test_plans_list_starts_with_the_default(admin_and_alice: tuple[Account, Account]) -> None:
    root, _ = admin_and_alice
    items = (await root.client.get(PLANS)).json()["items"]
    assert [(p["name"], p["is_default"], p["users"]) for p in items] == [(DEFAULT_PLAN_NAME, True, 2)]
    assert items[0]["monthly_spend_limit_usd"] == "25.000000"


async def test_create_assign_and_bill_a_plan(
    admin_and_alice: tuple[Account, Account], db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    root, alice = admin_and_alice
    created = await root.client.post(PLANS, headers=root.csrf, json=PRO)
    assert created.status_code == 201
    plan = created.json()
    assert plan["price_per_1k_input_tokens_usd"] == "0.000400" and plan["users"] == 0

    assigned = await root.client.patch(f"{USERS}/{alice.user_id}", headers=root.csrf, json={"plan_id": plan["id"]})
    assert assigned.status_code == 200
    assert (assigned.json()["plan_id"], assigned.json()["plan_name"]) == (plan["id"], "Pro")
    assert (await alice.client.get(BILLING)).json()["plan"]["name"] == "Pro"

    secret = (await alice.create_key())["secret"]
    usage = (await call_vision(alice.client, secret)).json()["usage"]
    (log,) = await rows(db_sessionmaker, UsageLog)
    pro = PlanPricing(plan_id=plan["id"], input_per_1k_micros=400, output_per_1k_micros=1200, per_image_micros=1500)
    assert log.cost_micros == pro.cost_micros(usage["input_tokens"], usage["output_tokens"], 1)

    reset = await root.client.patch(f"{USERS}/{alice.user_id}", headers=root.csrf, json={"plan_id": None})
    assert (reset.json()["plan_id"], reset.json()["plan_name"]) == (None, DEFAULT_PLAN_NAME)


async def test_switching_the_default_plan(admin_and_alice: tuple[Account, Account]) -> None:
    root, alice = admin_and_alice
    pro = (await root.client.post(PLANS, headers=root.csrf, json={**PRO, "is_default": True})).json()
    items = (await root.client.get(PLANS)).json()["items"]
    assert [(p["name"], p["is_default"]) for p in items] == [("Pro", True), (DEFAULT_PLAN_NAME, False)]
    assert pro["users"] == 2  # everyone without an explicit plan follows the default
    assert (await alice.client.get(BILLING)).json()["plan"]["name"] == "Pro"


async def test_plan_update_rules(admin_and_alice: tuple[Account, Account]) -> None:
    root, _ = admin_and_alice
    default_id = (await root.client.get(PLANS)).json()["items"][0]["id"]
    pro = (await root.client.post(PLANS, headers=root.csrf, json=PRO)).json()
    url = f"{PLANS}/{pro['id']}"

    lifted = await root.client.patch(url, headers=root.csrf, json={"monthly_request_limit": None})
    assert lifted.status_code == 200 and lifted.json()["monthly_request_limit"] is None
    assert lifted.json()["price_per_image_usd"] == "0.001500"  # untouched fields unchanged

    unset_default = await root.client.patch(f"{PLANS}/{default_id}", headers=root.csrf, json={"is_default": False})
    assert unset_default.status_code == 409
    rename_clash = await root.client.patch(url, headers=root.csrf, json={"name": "default"})
    assert rename_clash.status_code == 409 and error(rename_clash)["message"] == "A plan with this name already exists"
    null_price = await root.client.patch(url, headers=root.csrf, json={"price_per_image_usd": None})
    assert null_price.status_code == 422
    empty = await root.client.patch(url, headers=root.csrf, json={})
    assert empty.status_code == 422
    missing = await root.client.patch(f"{PLANS}/{MISSING_ID}", headers=root.csrf, json={"name": "x"})
    assert missing.status_code == 404


@pytest.mark.parametrize(
    ("override", "param"),
    [
        ({"price_per_image_usd": "-1"}, "price_per_image_usd"),
        ({"price_per_image_usd": "0.0000001"}, "price_per_image_usd"),
        ({"monthly_request_limit": -5}, "monthly_request_limit"),
        ({"name": ""}, "name"),
    ],
)
async def test_plan_validation(admin_and_alice: tuple[Account, Account], override: dict[str, Any], param: str) -> None:
    root, _ = admin_and_alice
    response = await root.client.post(PLANS, headers=root.csrf, json={**PRO, **override})
    assert response.status_code == 422
    assert error(response)["param"] == param


async def test_duplicate_plan_names_are_rejected(admin_and_alice: tuple[Account, Account]) -> None:
    root, _ = admin_and_alice
    assert (await root.client.post(PLANS, headers=root.csrf, json=PRO)).status_code == 201
    duplicate = await root.client.post(PLANS, headers=root.csrf, json={**PRO, "name": "PRO"})
    assert duplicate.status_code == 409


async def test_assigning_an_unknown_plan(admin_and_alice: tuple[Account, Account]) -> None:
    root, alice = admin_and_alice
    response = await root.client.patch(f"{USERS}/{alice.user_id}", headers=root.csrf, json={"plan_id": MISSING_ID})
    assert response.status_code == 404 and error(response)["message"] == "Plan not found"


async def test_plan_endpoints_are_admin_only(admin_and_alice: tuple[Account, Account]) -> None:
    _, alice = admin_and_alice
    assert (await alice.client.get(PLANS)).status_code == 403
    assert (await alice.client.post(PLANS, headers=alice.csrf, json=PRO)).status_code == 403
    assert (await alice.client.patch(f"{PLANS}/{MISSING_ID}", headers=alice.csrf, json={"name": "x"})).status_code == 403


async def test_customers_cannot_change_their_own_plan(admin_and_alice: tuple[Account, Account]) -> None:
    root, alice = admin_and_alice
    pro = (await root.client.post(PLANS, headers=root.csrf, json=PRO)).json()
    response = await alice.client.patch(f"{USERS}/{alice.user_id}", headers=alice.csrf, json={"plan_id": pro["id"]})
    assert response.status_code == 403


# ===========================================================================
# Documentation matches behaviour
# ===========================================================================


async def test_documented_quota_errors_have_the_real_shape(
    alice: Account, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    spec = (await alice.client.get("/openapi.json")).json()
    examples = spec["paths"][VISION]["post"]["responses"]["402"]["content"]["application/json"]["examples"]
    shapes = {
        "requests": r"^Monthly request quota of \d+ reached; it resets on \d{4}-\d{2}-\d{2}$",
        "spend": r"^Monthly spend limit of \$[\d.]+ reached; it resets on \d{4}-\d{2}-\d{2}$",
    }
    for name, shape in shapes.items():
        assert examples[name]["value"]["error"]["type"] == "quota_exceeded_error"
        assert re.match(shape, examples[name]["value"]["error"]["message"]), name

    await assign(db_sessionmaker, alice.user_id, await add_plan(db_sessionmaker, monthly_request_limit=0))
    secret = (await alice.create_key())["secret"]
    real = error(await call_vision(alice.client, secret))
    assert real["type"] == "quota_exceeded_error" and re.match(shapes["requests"], real["message"])

    await assign(db_sessionmaker, alice.user_id, await add_plan(db_sessionmaker, monthly_spend_limit_micros=0))
    real = error(await call_vision(alice.client, secret))
    assert re.match(shapes["spend"], real["message"]), real["message"]

