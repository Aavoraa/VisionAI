import io
import uuid
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.database.database import get_session_factory
from app.database.models import Job, UsageLog
from app.models.base import ModelError, ModelOutput, VisionModel
from app.models.local_transformers import MockVisionModel
from app.services.usage_service import UsageContext, log_usage, record_usage
from conftest import ClientFactory, create_api_key

pytestmark = pytest.mark.anyio

VISION = "/v1/vision"


def png_bytes(color: str = "red") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buffer, format="PNG")
    return buffer.getvalue()


def image_files(count: int = 1) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [("images", (f"{i}.png", png_bytes(), "image/png")) for i in range(count)]


async def all_rows(sessionmaker: async_sessionmaker[AsyncSession], model: type[Any]) -> list[Any]:
    async with sessionmaker() as session:
        return list((await session.execute(select(model))).scalars())


class FailingModel(VisionModel):
    name = "failing-model"

    async def analyze(self, images: list[bytes], prompt: str, options: dict[str, Any] | None = None) -> ModelOutput:
        raise ModelError("CUDA out of memory at 0xdeadbeef")


# ===========================================================================
# Usage tracking
# ===========================================================================


async def test_sync_vision_logs_usage(
    client: Any, auth_headers: dict[str, str], db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    response = await client.post(VISION, headers=auth_headers, data={"prompt": "describe it"}, files=image_files(2))
    assert response.status_code == 200
    body = response.json()

    logs = await all_rows(db_sessionmaker, UsageLog)
    assert len(logs) == 1
    log = logs[0]
    assert log.status == "success"
    assert log.error_message is None
    assert log.model == MockVisionModel.name
    assert log.image_count == 2
    assert log.input_tokens == body["usage"]["input_tokens"]
    assert log.output_tokens == body["usage"]["output_tokens"]
    assert log.request_id == response.headers["x-request-id"]
    assert log.processing_time_ms >= 0


async def test_usage_log_is_attributed_to_the_caller(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    headers, api_key, user = await create_api_key(db_sessionmaker, "billing@example.com")
    assert (await client.post(VISION, headers=headers, data={"prompt": "x"}, files=image_files())).status_code == 200
    (log,) = await all_rows(db_sessionmaker, UsageLog)
    assert (log.user_id, log.api_key_id) == (user.id, api_key.id)


async def test_failed_validation_is_logged_as_error(
    client: Any, auth_headers: dict[str, str], db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    response = await client.post(
        VISION,
        headers=auth_headers,
        data={"prompt": "x"},
        files=[("images", ("photo.jpg", b"not an image at all", "image/jpeg"))],
    )
    assert response.status_code == 400
    (log,) = await all_rows(db_sessionmaker, UsageLog)
    assert log.status == "error"
    assert "Unsupported image type" in (log.error_message or "")
    assert (log.input_tokens, log.output_tokens) == (0, 0)


async def test_model_failure_is_logged_without_internal_details(
    settings: Settings,
    make_client: ClientFactory,
    auth_headers: dict[str, str],
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with make_client(settings, model=FailingModel()) as client:
        response = await client.post(VISION, headers=auth_headers, data={"prompt": "x"}, files=image_files())
    assert response.status_code == 502
    (log,) = await all_rows(db_sessionmaker, UsageLog)
    assert log.status == "error"
    assert log.model == "failing-model"
    assert log.error_message == "Model inference failed"
    assert "deadbeef" not in (log.error_message or "")


class BrokenSession:
    """Session double whose writes always fail; usable as ``async with factory() as s``."""

    def __init__(self) -> None:
        self.rolled_back = False

    async def __aenter__(self) -> "BrokenSession":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def add(self, entry: object) -> None:
        return None

    async def commit(self) -> None:
        raise OperationalError("INSERT", {}, Exception("database is down"))

    async def rollback(self) -> None:
        self.rolled_back = True


async def test_log_usage_handles_db_errors_gracefully() -> None:
    session = BrokenSession()
    ok = await log_usage(
        session,  # type: ignore[arg-type]
        user_id="u",
        api_key_id="k",
        request_id="r",
        model="m",
        input_tokens=1,
        output_tokens=1,
        image_count=1,
        status="success",
        error_message=None,
        processing_time_ms=5,
    )
    assert ok is False
    assert session.rolled_back is True


async def test_log_usage_rejects_invalid_status() -> None:
    with pytest.raises(ValueError):
        await log_usage(
            BrokenSession(),  # type: ignore[arg-type]
            user_id="u",
            api_key_id="k",
            request_id="r",
            model="m",
            input_tokens=0,
            output_tokens=0,
            image_count=0,
            status="maybe",
            error_message=None,
            processing_time_ms=0,
        )


async def test_record_usage_survives_session_factory_failure() -> None:
    def broken_factory() -> Any:
        raise OSError("connection refused")

    context = UsageContext(user_id="u", api_key_id="k", request_id="r", model="m", image_count=1)
    assert await record_usage(broken_factory, context, status="success", processing_time_ms=1) is False  # type: ignore[arg-type]


async def test_usage_write_failure_does_not_break_the_response(
    settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str]
) -> None:
    # Every usage write fails (DB down for that pool); the served request must still succeed.
    async with make_client(settings, overrides={get_session_factory: lambda: BrokenSession}) as client:
        response = await client.post(VISION, headers=auth_headers, data={"prompt": "x"}, files=image_files())
    assert response.status_code == 200
    assert "Mock analysis" in response.json()["result"]["text"]


# ===========================================================================
# Async mode
# ===========================================================================


async def test_async_job_creation(
    client: Any, auth_headers: dict[str, str], db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    response = await client.post(
        VISION, headers=auth_headers, data={"prompt": "describe", "mode": "async"}, files=image_files()
    )
    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"job_id", "status", "result", "error"}
    assert body["status"] == "pending"
    assert body["result"] is None and body["error"] is None
    assert str(uuid.UUID(body["job_id"])) == body["job_id"]
    assert response.headers["location"] == f"/v1/jobs/{body['job_id']}"

    jobs = await all_rows(db_sessionmaker, Job)
    assert [job.id for job in jobs] == [body["job_id"]]


async def test_async_job_via_json_image_url(
    client: Any, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import vision_service as vision_service_module

    async def fake_fetch(url: str, max_size_mb: int = 10, timeout: float = 5.0) -> bytes:
        return png_bytes("blue")

    monkeypatch.setattr(vision_service_module, "fetch_image_from_url", fake_fetch)
    response = await client.post(
        VISION,
        headers=auth_headers,
        json={"prompt": "what is it", "image_url": "https://images.example.com/a.png", "mode": "async"},
    )
    assert response.status_code == 202
    job = (await client.get(f"/v1/jobs/{response.json()['job_id']}", headers=auth_headers)).json()
    assert job["status"] == "completed"


async def test_async_job_completes_with_result_and_usage(
    client: Any, auth_headers: dict[str, str], db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    created = await client.post(
        VISION, headers=auth_headers, data={"prompt": "describe", "mode": "async"}, files=image_files()
    )
    job_id = created.json()["job_id"]

    # The background task has run by the time the ASGI call returns.
    polled = await client.get(f"/v1/jobs/{job_id}", headers=auth_headers)
    assert polled.status_code == 200
    body = polled.json()
    assert body["status"] == "completed"
    assert body["error"] is None
    assert body["result"]["object"] == "vision.response"
    assert body["result"]["model"] == MockVisionModel.name
    assert "1 image(s)" in body["result"]["result"]["text"]

    (log,) = await all_rows(db_sessionmaker, UsageLog)
    assert log.status == "success"
    assert log.request_id == created.headers["x-request-id"]
    assert log.output_tokens == body["result"]["usage"]["output_tokens"]


async def test_async_job_passes_through_processing_state(
    settings: Settings,
    make_client: ClientFactory,
    auth_headers: dict[str, str],
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    observed: list[str] = []

    class ProbeModel(MockVisionModel):
        async def analyze(
            self, images: list[bytes], prompt: str, options: dict[str, Any] | None = None
        ) -> ModelOutput:
            (job,) = await all_rows(db_sessionmaker, Job)
            observed.append(job.status)
            return await super().analyze(images, prompt, options)

    async with make_client(settings, model=ProbeModel()) as client:
        created = await client.post(
            VISION, headers=auth_headers, data={"prompt": "x", "mode": "async"}, files=image_files()
        )
        assert created.json()["status"] == "pending"
        final = (await client.get(f"/v1/jobs/{created.json()['job_id']}", headers=auth_headers)).json()

    assert observed == ["processing"]
    assert final["status"] == "completed"


async def test_async_job_model_failure_marks_job_failed(
    settings: Settings,
    make_client: ClientFactory,
    auth_headers: dict[str, str],
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with make_client(settings, model=FailingModel()) as client:
        created = await client.post(
            VISION, headers=auth_headers, data={"prompt": "x", "mode": "async"}, files=image_files()
        )
        assert created.status_code == 202
        body = (await client.get(f"/v1/jobs/{created.json()['job_id']}", headers=auth_headers)).json()

    assert body == {"job_id": created.json()["job_id"], "status": "failed", "result": None, "error": "Model inference failed"}
    (log,) = await all_rows(db_sessionmaker, UsageLog)
    assert log.status == "error"


async def test_async_job_with_invalid_image_fails_in_background(client: Any, auth_headers: dict[str, str]) -> None:
    created = await client.post(
        VISION,
        headers=auth_headers,
        data={"prompt": "x", "mode": "async"},
        files=[("images", ("photo.jpg", b"text pretending to be a jpeg", "image/jpeg"))],
    )
    assert created.status_code == 202
    body = (await client.get(f"/v1/jobs/{created.json()['job_id']}", headers=auth_headers)).json()
    assert body["status"] == "failed"
    assert "Unsupported image type" in body["error"]


async def test_async_request_without_image_is_rejected_without_creating_a_job(
    client: Any, auth_headers: dict[str, str], db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    response = await client.post(VISION, headers=auth_headers, json={"prompt": "no image at all", "mode": "async"})
    assert response.status_code == 400
    assert "at least one image" in response.json()["error"]["message"]
    assert await all_rows(db_sessionmaker, Job) == []


async def test_async_request_for_unknown_model_is_rejected_without_creating_a_job(
    client: Any, auth_headers: dict[str, str], db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    response = await client.post(
        VISION, headers=auth_headers, data={"prompt": "x", "mode": "async", "model": "unknown-model"}, files=image_files()
    )
    assert response.status_code == 400
    assert await all_rows(db_sessionmaker, Job) == []


async def test_invalid_mode_is_a_validation_error(client: Any, auth_headers: dict[str, str]) -> None:
    response = await client.post(
        VISION, headers=auth_headers, data={"prompt": "x", "mode": "later"}, files=image_files()
    )
    assert response.status_code == 422
    assert response.json()["error"]["param"] == "mode"


# ===========================================================================
# GET /v1/jobs/{job_id}
# ===========================================================================


async def insert_job(sessionmaker: async_sessionmaker[AsyncSession], user_id: str, api_key_id: str, **values: Any) -> Job:
    async with sessionmaker() as session:
        job = Job(user_id=user_id, api_key_id=api_key_id, **values)
        session.add(job)
        await session.commit()
        return job


async def test_async_job_polling(client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    owner_headers, owner_key, owner = await create_api_key(db_sessionmaker, "owner@example.com")
    intruder_headers, _, _ = await create_api_key(db_sessionmaker, "intruder@example.com")
    job = await insert_job(db_sessionmaker, owner.id, owner_key.id, status="processing")

    response = await client.get(f"/v1/jobs/{job.id}", headers=owner_headers)
    assert response.status_code == 200
    assert response.json() == {"job_id": job.id, "status": "processing", "result": None, "error": None}

    # Another user cannot see the job, nor learn that it exists.
    foreign = await client.get(f"/v1/jobs/{job.id}", headers=intruder_headers)
    missing = await client.get(f"/v1/jobs/{uuid.uuid4()}", headers=intruder_headers)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"] == "Job not found"


async def test_polling_reflects_db_state_changes(client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    headers, api_key, user = await create_api_key(db_sessionmaker, "owner@example.com")
    job = await insert_job(db_sessionmaker, user.id, api_key.id)
    assert (await client.get(f"/v1/jobs/{job.id}", headers=headers)).json()["status"] == "pending"

    result = {
        "id": "vis_abc",
        "object": "vision.response",
        "model": "mock-vision-1",
        "created": 1_700_000_000,
        "result": {"text": "a red square"},
        "usage": {"input_tokens": 10, "output_tokens": 3},
    }
    async with db_sessionmaker() as session:
        stored = await session.get(Job, job.id)
        assert stored is not None
        stored.status = "completed"
        stored.result_data = result
        await session.commit()

    body = (await client.get(f"/v1/jobs/{job.id}", headers=headers)).json()
    assert body["status"] == "completed"
    assert body["result"] == result


async def test_uppercase_job_id_is_accepted(client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    headers, api_key, user = await create_api_key(db_sessionmaker, "owner@example.com")
    job = await insert_job(db_sessionmaker, user.id, api_key.id)
    assert (await client.get(f"/v1/jobs/{job.id.upper()}", headers=headers)).status_code == 200


@pytest.mark.parametrize("job_id", ["not-a-uuid", "1' OR '1'='1", "a" * 36])
async def test_malformed_job_id_is_rejected(client: Any, auth_headers: dict[str, str], job_id: str) -> None:
    response = await client.get(f"/v1/jobs/{job_id}", headers=auth_headers)
    assert response.status_code == 422


async def test_job_polling_requires_auth(client: Any) -> None:
    response = await client.get(f"/v1/jobs/{uuid.uuid4()}")
    assert response.status_code == 401


async def test_job_status_is_constrained(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    _, api_key, user = await create_api_key(db_sessionmaker, "owner@example.com")
    with pytest.raises(IntegrityError):
        await insert_job(db_sessionmaker, user.id, api_key.id, status="exploded")


async def test_job_count_is_one_per_async_request(
    client: Any, auth_headers: dict[str, str], db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    for _ in range(3):
        await client.post(VISION, headers=auth_headers, data={"prompt": "x", "mode": "async"}, files=image_files())
    async with db_sessionmaker() as session:
        count = (await session.execute(select(func.count()).select_from(Job))).scalar_one()
    assert count == 3
