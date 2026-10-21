import asyncio
import functools
import io
import json
import sys
import threading
import time
import types
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import security
from app.core.config import Settings
from app.main import build_vision_model, create_app
from app.models.base import ModelError, ModelOutput, ModelUnavailableError, VisionModel
from app.models.local_transformers import (
    Florence2VisionModel,
    LocalTransformersVisionModel,
    MockVisionModel,
    load_local_model,
)
from app.schemas.vision import VisionRequest
from app.services import vision_service as vision_service_module
from app.services.vision_service import VisionService
from conftest import ClientFactory

pytestmark = pytest.mark.anyio

VISION = "/v1/vision"


def png_bytes(size: tuple[int, int] = (32, 24), color: str = "red") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def error_of(response: httpx.Response) -> dict[str, Any]:
    body = response.json()
    assert set(body) == {"error", "request_id"}
    assert body["request_id"] == response.headers["x-request-id"]
    return body["error"]


@pytest.fixture
def fake_remote_image(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Route the real SSRF-safe fetcher through a mocked network: public DNS + canned image."""
    seen: list[httpx.Request] = []
    image = png_bytes((40, 30), "blue")

    async def resolver(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=image)

    patched = functools.partial(
        security.fetch_image_from_url, resolver=resolver, transport=httpx.MockTransport(handler)
    )
    monkeypatch.setattr(vision_service_module, "fetch_image_from_url", patched)
    return seen


# ===========================================================================
# App-level behaviour
# ===========================================================================


async def test_health_returns_ok(client: httpx.AsyncClient, settings: Settings) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": settings.app_version}


async def test_security_headers_are_set(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]


async def test_valid_request_id_is_echoed(client: httpx.AsyncClient) -> None:
    response = await client.get("/health", headers={"X-Request-ID": "abc-123_XYZ.789"})
    assert response.headers["x-request-id"] == "abc-123_XYZ.789"


@pytest.mark.parametrize("bad_id", ["short", "has space in it!", "<script>alert(1)</script>", "x" * 65])
async def test_invalid_request_id_is_replaced(client: httpx.AsyncClient, bad_id: str) -> None:
    response = await client.get("/health", headers={"X-Request-ID": bad_id})
    generated = response.headers["x-request-id"]
    assert generated != bad_id
    assert len(generated) == 32


async def test_unknown_route_returns_standard_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/does-not-exist")
    assert response.status_code == 404
    assert error_of(response) == {"type": "not_found_error", "message": "Not Found", "param": None}


async def test_unhandled_errors_do_not_leak_details(settings: Settings) -> None:
    app = create_app(settings)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("db password is hunter2")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        response = await http_client.get("/boom")

    assert response.status_code == 500
    assert "hunter2" not in response.text
    assert error_of(response) == {"type": "api_error", "message": "Internal server error", "param": None}


async def test_docs_disabled_in_production(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"environment": "production"}))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        assert (await http_client.get("/docs")).status_code == 404
        assert (await http_client.get("/openapi.json")).status_code == 404


async def test_openapi_documents_both_content_types(client: httpx.AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/health" in paths
    content = paths[VISION]["post"]["requestBody"]["content"]
    assert set(content) == {"application/json", "multipart/form-data"}


async def test_openapi_documents_auth_and_examples(client: httpx.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()

    # Swagger "Authorize" button: bearer scheme registered and applied to /v1 endpoints.
    assert spec["components"]["securitySchemes"]["APIKey"] == {
        "type": "http",
        "scheme": "bearer",
        "description": "Send your API key as `Authorization: Bearer sk_live_...`.",
    }
    assert {"APIKey": []} in spec["paths"][VISION]["post"]["security"]
    assert {"APIKey": []} in spec["paths"]["/v1/jobs/{job_id}"]["get"]["security"]
    assert "security" not in spec["paths"]["/health"]["get"]

    # Request and response bodies carry concrete examples.
    json_body = spec["paths"][VISION]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert json_body["examples"][0]["prompt"]
    schemas = spec["components"]["schemas"]
    for name in ("VisionResponse", "JobResponse", "VisionErrorResponse"):
        assert schemas[name]["examples"], name
    assert set(spec["paths"][VISION]["post"]["responses"]) >= {"200", "202", "401", "429"}
    assert "Authentication" in spec["info"]["description"]


async def test_schema_examples_are_valid_instances() -> None:
    from app.schemas.vision import JobResponse, VisionErrorResponse, VisionResponse

    for model in (VisionRequest, VisionResponse, JobResponse, VisionErrorResponse):
        for example in model.model_config["json_schema_extra"]["examples"]:  # type: ignore[index]
            model.model_validate(example)


def test_settings_have_local_dev_database_default() -> None:
    defaults = Settings(_env_file=None)  # type: ignore[call-arg]
    assert defaults.database_url.get_secret_value() == (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/vision_api"
    )
    assert defaults.use_mock_model is True


def test_env_example_template_parses() -> None:
    """`cp .env.example .env` must yield a working configuration (empty = default)."""
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / ".env.example"
    parsed = Settings(_env_file=example)  # type: ignore[call-arg]
    assert parsed.allow_signup is None and parsed.session_cookie_secure is None
    assert parsed.signup_enabled is True and parsed.cookie_secure is False  # development defaults


def test_settings_reject_unsupported_schemes_and_formats() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, fetch_allowed_schemes=frozenset({"file"}))  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        Settings(_env_file=None, allowed_image_formats=frozenset({"SVG"}))  # type: ignore[call-arg]


def test_secrets_are_masked_in_repr(settings: Settings) -> None:
    assert "test:test@" not in repr(settings)


# ===========================================================================
# POST /v1/vision: the four required scenarios
# ===========================================================================


async def test_vision_endpoint_with_json(
    client: httpx.AsyncClient, auth_headers: dict[str, str], fake_remote_image: list[httpx.Request]
) -> None:
    response = await client.post(
        VISION,
        headers=auth_headers,
        json={"prompt": "What colour is this?", "image_url": "https://images.example.com/blue.png"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "vision.response"
    assert body["id"].startswith("vis_")
    assert body["model"] == MockVisionModel.name
    assert isinstance(body["created"], int)
    assert "1 image(s)" in body["result"]["text"]
    assert "What colour is this?" in body["result"]["text"]
    # "Mock analysis of 1 image(s) [<hash>] for prompt: What colour is this?" -> 12 words
    assert body["usage"] == {"input_tokens": 4 + MockVisionModel.IMAGE_TOKENS, "output_tokens": 12}

    # The image was fetched through the SSRF-safe path: pinned IP, original Host header.
    assert len(fake_remote_image) == 1
    assert fake_remote_image[0].url.host == "93.184.216.34"
    assert fake_remote_image[0].headers["host"] == "images.example.com"


async def test_vision_endpoint_with_upload(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.post(
        VISION,
        headers=auth_headers,
        data={"prompt": "Describe the image", "temperature": "0.5", "max_tokens": "200"},
        files=[("images", ("cat.png", png_bytes(), "image/png"))],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"] == MockVisionModel.name
    assert "1 image(s)" in body["result"]["text"]
    assert body["usage"]["input_tokens"] == 3 + MockVisionModel.IMAGE_TOKENS


async def test_invalid_image_rejection(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.post(
        VISION,
        headers=auth_headers,
        data={"prompt": "Describe the image"},
        files=[("images", ("photo.jpg", b"I am a text file, not a JPEG", "image/jpeg"))],
    )
    assert response.status_code == 400
    error = error_of(response)
    assert error["type"] == "invalid_request_error"
    assert "Unsupported image type" in error["message"]


async def test_unauthorized_access(client: httpx.AsyncClient) -> None:
    response = await client.post(VISION, json={"prompt": "hi", "image_url": "https://images.example.com/a.png"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert error_of(response)["type"] == "authentication_error"


# ===========================================================================
# POST /v1/vision: additional edge cases
# ===========================================================================


async def test_invalid_api_key_is_rejected_before_body_is_parsed(client: httpx.AsyncClient) -> None:
    response = await client.post(
        VISION, headers={"Authorization": "Bearer sk_live_fake", "Content-Type": "text/plain"}, content=b"junk"
    )
    assert response.status_code == 401


async def test_multiple_uploads_plus_url(
    client: httpx.AsyncClient, auth_headers: dict[str, str], fake_remote_image: list[httpx.Request]
) -> None:
    response = await client.post(
        VISION,
        headers=auth_headers,
        data={"prompt": "Compare", "image_url": "https://images.example.com/blue.png"},
        files=[
            ("images", ("a.png", png_bytes(color="red"), "image/png")),
            ("images", ("b.png", png_bytes(color="green"), "image/png")),
        ],
    )
    assert response.status_code == 200, response.text
    assert "3 image(s)" in response.json()["result"]["text"]
    assert len(fake_remote_image) == 1


async def test_ssrf_url_is_blocked_through_the_api(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.post(
        VISION,
        headers=auth_headers,
        json={"prompt": "steal creds", "image_url": "http://169.254.169.254/latest/meta-data/"},
    )
    assert response.status_code == 400
    assert "non-public" in error_of(response)["message"]


async def test_request_without_any_image(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.post(VISION, headers=auth_headers, json={"prompt": "hello"})
    assert response.status_code == 400
    assert "at least one image" in error_of(response)["message"]


async def test_too_many_images(client: httpx.AsyncClient, auth_headers: dict[str, str], settings: Settings) -> None:
    files = [("images", (f"{i}.png", png_bytes(), "image/png")) for i in range(settings.max_images_per_request + 1)]
    response = await client.post(VISION, headers=auth_headers, data={"prompt": "many"}, files=files)
    assert response.status_code == 400


async def test_oversized_upload_returns_413(
    settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str]
) -> None:
    small = settings.model_copy(update={"max_image_bytes": 1024 * 1024})
    big = png_bytes() + b"\x00" * (1024 * 1024)
    async with make_client(small) as client:
        response = await client.post(
            VISION, headers=auth_headers, data={"prompt": "big"}, files=[("images", ("big.png", big, "image/png"))]
        )
    assert response.status_code == 413
    assert error_of(response)["type"] == "request_too_large"


async def test_oversized_json_body_returns_413(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.post(
        VISION,
        headers={**auth_headers, "Content-Type": "application/json"},
        content=b'{"prompt": "' + b"a" * (300 * 1024) + b'"}',
    )
    assert response.status_code == 413


async def test_chunked_body_without_content_length_is_still_capped(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"prompt": "'
        for _ in range(40):
            yield b"a" * 8192
        yield b'"}'

    response = await client.post(
        VISION, headers={**auth_headers, "Content-Type": "application/json"}, content=chunks()
    )
    assert response.status_code == 413


@pytest.mark.parametrize(
    ("payload", "param"),
    [
        ({"image_url": "https://images.example.com/a.png"}, "prompt"),
        ({"prompt": "", "image_url": "https://images.example.com/a.png"}, "prompt"),
        ({"prompt": "x", "temperature": 5}, "temperature"),
        ({"prompt": "x", "max_tokens": 0}, "max_tokens"),
        ({"prompt": "x", "max_tokens": 100000}, "max_tokens"),
        ({"prompt": "x", "model": "../../etc/passwd; rm -rf"}, "model"),
        ({"prompt": "x", "unexpected": True}, "unexpected"),
    ],
)
async def test_schema_validation_errors(
    client: httpx.AsyncClient, auth_headers: dict[str, str], payload: dict[str, Any], param: str
) -> None:
    response = await client.post(VISION, headers=auth_headers, json=payload)
    assert response.status_code == 422
    error = error_of(response)
    assert error["type"] == "invalid_request_error"
    assert error["param"] == param


async def test_validation_errors_do_not_echo_input(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.post(
        VISION, headers=auth_headers, json={"prompt": "x", "temperature": "<script>alert(1)</script>"}
    )
    assert response.status_code == 422
    assert "<script>" not in response.text


@pytest.mark.parametrize("content", [b"{not json", b"[1, 2, 3]", b'"string"', b"\xff\xfe"])
async def test_malformed_json_body(client: httpx.AsyncClient, auth_headers: dict[str, str], content: bytes) -> None:
    response = await client.post(
        VISION, headers={**auth_headers, "Content-Type": "application/json"}, content=content
    )
    assert response.status_code == 400


async def test_unsupported_content_type(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.post(
        VISION, headers={**auth_headers, "Content-Type": "text/plain"}, content=b"prompt=hi"
    )
    assert response.status_code == 415


async def test_unexpected_file_field_is_rejected(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.post(
        VISION,
        headers=auth_headers,
        data={"prompt": "x"},
        files=[("avatar", ("a.png", png_bytes(), "image/png"))],
    )
    assert response.status_code == 400
    assert "images" in error_of(response)["message"]


async def test_unknown_form_field_is_rejected(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.post(
        VISION,
        headers=auth_headers,
        data={"prompt": "x", "is_admin": "true"},
        files=[("images", ("a.png", png_bytes(), "image/png"))],
    )
    assert response.status_code == 422
    assert error_of(response)["param"] == "is_admin"


async def test_response_format_is_rejected_until_supported(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        VISION,
        headers=auth_headers,
        data={"prompt": "x", "response_format": json.dumps({"type": "object"})},
        files=[("images", ("a.png", png_bytes(), "image/png"))],
    )
    assert response.status_code == 400
    assert "response_format is not supported" in error_of(response)["message"]


async def test_unknown_model_is_rejected(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.post(
        VISION,
        headers=auth_headers,
        data={"prompt": "x", "model": "gpt-99"},
        files=[("images", ("a.png", png_bytes(), "image/png"))],
    )
    assert response.status_code == 400
    assert "Unknown model" in error_of(response)["message"]


async def test_model_can_be_selected_by_name(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.post(
        VISION,
        headers=auth_headers,
        data={"prompt": "x", "model": MockVisionModel.name},
        files=[("images", ("a.png", png_bytes(), "image/png"))],
    )
    assert response.status_code == 200


class FailingModel(VisionModel):
    name = "failing-model"

    async def analyze(self, images: list[bytes], prompt: str, options: dict[str, Any] | None = None) -> ModelOutput:
        raise ModelError("CUDA out of memory at 0xdeadbeef")


class SlowModel(VisionModel):
    name = "slow-model"

    async def analyze(self, images: list[bytes], prompt: str, options: dict[str, Any] | None = None) -> ModelOutput:
        await asyncio.sleep(5)
        return ModelOutput(text="late", input_tokens=1, output_tokens=1)


class MalformedModel(VisionModel):
    name = "malformed-model"

    async def analyze(self, images: list[bytes], prompt: str, options: dict[str, Any] | None = None) -> ModelOutput:
        return {"text": None, "input_tokens": "many"}  # type: ignore[typeddict-item]


@pytest.mark.parametrize(
    ("model", "expected_status", "expected_type"),
    [
        (FailingModel(), 502, "model_error"),
        (MalformedModel(), 502, "model_error"),
        (SlowModel(), 504, "timeout_error"),
    ],
)
async def test_model_failures_map_to_standard_errors(
    settings: Settings,
    make_client: ClientFactory,
    auth_headers: dict[str, str],
    model: VisionModel,
    expected_status: int,
    expected_type: str,
) -> None:
    fast_timeout = settings.model_copy(update={"model_timeout_seconds": 0.1})
    async with make_client(fast_timeout, model=model) as client:
        response = await client.post(
            VISION, headers=auth_headers, data={"prompt": "x"}, files=[("images", ("a.png", png_bytes(), "image/png"))]
        )
    assert response.status_code == expected_status
    error = error_of(response)
    assert error["type"] == expected_type
    assert "deadbeef" not in response.text  # internal details never reach the client


async def test_service_unavailable_before_startup(
    settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str]
) -> None:
    async with make_client(settings, run_lifespan=False) as client:
        response = await client.post(VISION, headers=auth_headers, json={"prompt": "x"})
    assert response.status_code == 503
    assert error_of(response)["type"] == "service_unavailable"


# ===========================================================================
# Lifespan / model selection
# ===========================================================================


async def test_lifespan_loads_mock_model_once(settings: Settings) -> None:
    app = create_app(settings)
    assert app.state.vision_service is None
    async with app.router.lifespan_context(app):
        service = app.state.vision_service
        assert isinstance(service, VisionService)
        assert isinstance(service.model, MockVisionModel)
    assert app.state.vision_service is None


def test_mock_model_is_refused_in_production(settings: Settings) -> None:
    with pytest.raises(RuntimeError, match="production"):
        build_vision_model(settings.model_copy(update={"environment": "production"}))


def test_real_model_without_ml_packages_fails_clearly(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "torch", None)  # forces ImportError
    with pytest.raises(ModelUnavailableError, match="pip install torch torchvision transformers accelerate"):
        build_vision_model(settings.model_copy(update={"use_mock_model": False}))


# ===========================================================================
# MockVisionModel
# ===========================================================================


async def test_mock_model_is_deterministic() -> None:
    model = MockVisionModel()
    first = await model.analyze([b"img-a", b"img-b"], "what is this", {"max_tokens": 100})
    second = await model.analyze([b"img-a", b"img-b"], "what is this", {"max_tokens": 100})
    assert first == second
    assert first["input_tokens"] == 3 + 2 * MockVisionModel.IMAGE_TOKENS


async def test_mock_model_respects_max_tokens() -> None:
    output = await MockVisionModel().analyze([b"img"], "describe " * 40, {"max_tokens": 5})
    assert output["output_tokens"] == 5
    assert len(output["text"].split()) == 5


async def test_mock_model_requires_an_image() -> None:
    with pytest.raises(ModelError):
        await MockVisionModel().analyze([], "hi")


async def test_vision_service_directly(settings: Settings) -> None:
    service = VisionService(MockVisionModel(), settings)
    response = await service.process_request(VisionRequest(prompt="hello there"), [png_bytes()])
    assert response.usage.input_tokens == 2 + MockVisionModel.IMAGE_TOKENS
    assert response.model == MockVisionModel.name


# ===========================================================================
# LocalTransformersVisionModel (exercised against stand-in torch/transformers
# modules so no weights are downloaded)
# ===========================================================================


class FakeTensor:
    device = "cpu"

    def __init__(self, rows: int, cols: int) -> None:
        self.shape = (rows, cols)

    def __getitem__(self, key: tuple[slice, slice]) -> "FakeTensor":
        _, col_slice = key
        return FakeTensor(self.shape[0], len(range(*col_slice.indices(self.shape[1]))))


class FakeBatch(dict[str, Any]):
    def to(self, device: str, dtype: str | None = None) -> "FakeBatch":
        self["moved_to"] = (device, dtype)
        return self


class FakeProcessor:
    calls: list[dict[str, Any]] = []

    @classmethod
    def from_pretrained(cls, name: str) -> "FakeProcessor":
        return cls()

    def apply_chat_template(self, messages: list[dict[str, Any]], **kwargs: Any) -> FakeBatch:
        FakeProcessor.calls.append({"messages": messages, **kwargs})
        return FakeBatch(input_ids=FakeTensor(1, 7))

    def batch_decode(self, generated: FakeTensor, skip_special_tokens: bool) -> list[str]:
        return [f"  decoded {generated.shape[1]} tokens  "]


class FakeModel:
    device = "cpu"
    dtype = "float32"
    generate_kwargs: dict[str, Any] = {}
    load_kwargs: dict[str, Any] = {}

    @classmethod
    def from_pretrained(cls, name: str, **kwargs: Any) -> "FakeModel":
        FakeModel.load_kwargs = kwargs
        return cls()

    def eval(self) -> None:
        return None

    def generate(self, **kwargs: Any) -> FakeTensor:
        FakeModel.generate_kwargs = kwargs
        return FakeTensor(1, 7 + 4)


class FakeConfig:
    model_type = "smolvlm"

    @classmethod
    def from_pretrained(cls, name: str) -> types.SimpleNamespace:
        return types.SimpleNamespace(model_type=cls.model_type)


class FakeFlorenceProcessor(FakeProcessor):
    crops: list[tuple[int, int]] = []

    def __call__(self, text: str, images: Image.Image, return_tensors: str) -> FakeBatch:
        FakeFlorenceProcessor.crops.append(images.size)
        return FakeBatch(input_ids=FakeTensor(1, 5))

    def batch_decode(self, generated: FakeTensor, skip_special_tokens: bool) -> list[str]:
        return ["</s><s>raw</s>"]

    def post_process_generation(self, raw: str, task: str, image_size: tuple[int, int]) -> dict[str, Any]:
        if task == "<OD>":
            return {task: {"bboxes": [[1.0, 2.0, 3.0, 4.0]], "labels": ["cat"]}}
        return {task: f" text of {image_size[0]}x{image_size[1]} "}


@contextmanager
def _no_grad() -> Iterator[None]:
    yield


@pytest.fixture
def fake_ml_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    torch = types.ModuleType("torch")
    torch.inference_mode = _no_grad  # type: ignore[attr-defined]
    torch.float32 = "float32"  # type: ignore[attr-defined]
    torch.bool = "bool"  # type: ignore[attr-defined]
    torch.full = lambda shape, value, **kwargs: value  # type: ignore[attr-defined]
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)  # type: ignore[attr-defined]
    transformers = types.ModuleType("transformers")
    transformers.AutoProcessor = FakeProcessor  # type: ignore[attr-defined]
    transformers.AutoModelForImageTextToText = FakeModel  # type: ignore[attr-defined]
    transformers.StoppingCriteria = object  # type: ignore[attr-defined]
    transformers.StoppingCriteriaList = list  # type: ignore[attr-defined]
    transformers.AutoConfig = FakeConfig  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    FakeProcessor.calls = []
    FakeFlorenceProcessor.crops = []
    FakeConfig.model_type = "smolvlm"


@pytest.mark.usefixtures("fake_ml_stack")
async def test_local_transformers_model_generates() -> None:
    model = LocalTransformersVisionModel("org/tiny-vlm", device="cpu")
    output = await model.analyze([png_bytes(), png_bytes(color="blue")], "Describe", {"temperature": 0.7, "max_tokens": 64})

    assert output == {"text": "decoded 4 tokens", "input_tokens": 7, "output_tokens": 4}
    content = FakeProcessor.calls[0]["messages"][0]["content"]
    assert [part["type"] for part in content] == ["image", "image", "text"]
    assert all(isinstance(part["image"], Image.Image) for part in content[:2])
    assert FakeModel.generate_kwargs["max_new_tokens"] == 64
    assert FakeModel.generate_kwargs["do_sample"] is True
    assert FakeModel.generate_kwargs["temperature"] == 0.7
    await model.close()


@pytest.mark.usefixtures("fake_ml_stack")
@pytest.mark.parametrize(
    ("device", "cuda", "dtype"),
    [("cpu", True, "float32"), ("auto", False, "float32"), ("auto", True, "auto"), ("cuda", True, "auto")],
)
def test_local_transformers_uses_float32_on_cpu(device: str, cuda: bool, dtype: str) -> None:
    # bfloat16 checkpoints run many times slower on CPUs without native bf16 support.
    sys.modules["torch"].cuda = types.SimpleNamespace(is_available=lambda: cuda)  # type: ignore[attr-defined]
    LocalTransformersVisionModel("org/tiny-vlm", device=device)
    assert FakeModel.load_kwargs == {"dtype": dtype, "device_map": device}


@pytest.mark.usefixtures("fake_ml_stack")
async def test_local_transformers_greedy_when_temperature_zero() -> None:
    model = LocalTransformersVisionModel("org/tiny-vlm", device="cpu")
    await model.analyze([png_bytes()], "Describe", {"temperature": 0.0})
    assert FakeModel.generate_kwargs["do_sample"] is False
    assert "temperature" not in FakeModel.generate_kwargs


@pytest.mark.usefixtures("fake_ml_stack")
async def test_local_transformers_wraps_runtime_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    model = LocalTransformersVisionModel("org/tiny-vlm", device="cpu")

    def explode(**kwargs: Any) -> FakeTensor:
        raise RuntimeError("CUDA error: device-side assert")

    monkeypatch.setattr(model._model, "generate", explode)
    with pytest.raises(ModelError, match="local inference failed"):
        await model.analyze([png_bytes()], "Describe")


@pytest.mark.usefixtures("fake_ml_stack")
async def test_local_transformers_stops_abandoned_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    # A timed-out request must not leave generate() running: it would slow every later request.
    model = LocalTransformersVisionModel("org/tiny-vlm", device="cpu")
    stopped = threading.Event()

    def generate_until_stopped(**kwargs: Any) -> FakeTensor:
        criteria = kwargs["stopping_criteria"]
        deadline = time.monotonic() + 5
        while not all(criterion(FakeTensor(1, 7), None) for criterion in criteria):
            if time.monotonic() > deadline:
                raise AssertionError("generation was never asked to stop")
            time.sleep(0.01)
        stopped.set()
        return FakeTensor(1, 8)

    monkeypatch.setattr(model._model, "generate", generate_until_stopped)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            await model.analyze([png_bytes()], "Describe")

    assert stopped.is_set()  # stopped before analyze() let go of the model
    assert not model._lock.locked()
    monkeypatch.setattr(model._model, "generate", FakeModel().generate)  # a normal model again
    output = await model.analyze([png_bytes()], "Describe")
    assert output["output_tokens"] == 4


def test_florence_ocr_strips_cover_tall_images() -> None:
    assert Florence2VisionModel.ocr_strips((1000, 300)) == [(0, 0, 1000, 300)]
    assert Florence2VisionModel.ocr_strips((1000, 500)) == [(0, 0, 1000, 500)]  # within 1.25 strips
    strips = Florence2VisionModel.ocr_strips((1071, 1600))
    assert strips[0] == (0, 0, 1071, 428)
    assert strips[-1][3] == 1600
    for (_, _, _, bottom), (_, next_top, _, _) in zip(strips, strips[1:]):
        assert bottom - next_top == 43  # overlap, so no text line is cut in half everywhere
    assert all(bottom - top >= 428 * 0.25 for _, top, _, bottom in strips)  # no sliver at the end


@pytest.fixture
def florence(fake_ml_stack: None, monkeypatch: pytest.MonkeyPatch) -> Florence2VisionModel:
    monkeypatch.setattr(sys.modules["transformers"], "AutoProcessor", FakeFlorenceProcessor)
    return Florence2VisionModel("org/florence", device="cpu")


async def test_florence_reads_tall_images_in_strips(florence: Florence2VisionModel) -> None:
    output = await florence.analyze([png_bytes(size=(100, 250))], " <OCR> ", {"max_tokens": 64})

    strips = Florence2VisionModel.ocr_strips((100, 250))
    assert len(strips) > 1
    assert FakeFlorenceProcessor.crops == [(r - l, b - t) for l, t, r, b in strips]
    assert output["text"].splitlines() == [f"text of {w}x{h}" for w, h in FakeFlorenceProcessor.crops]
    assert output["input_tokens"] == 5 * len(strips)
    assert output["output_tokens"] == 11 * len(strips)
    assert FakeModel.generate_kwargs["max_new_tokens"] == 64
    assert FakeModel.generate_kwargs["do_sample"] is False


async def test_florence_region_task_returns_json_for_the_whole_image(florence: Florence2VisionModel) -> None:
    output = await florence.analyze([png_bytes(size=(100, 250)), png_bytes()], "<OD>")
    assert FakeFlorenceProcessor.crops == [(100, 250), (32, 24)]  # no strips outside <OCR>
    first, second = output["text"].split("\n\n")
    assert json.loads(first.removeprefix("Image 1:\n")) == {"bboxes": [[1.0, 2.0, 3.0, 4.0]], "labels": ["cat"]}
    assert second.startswith("Image 2:\n")


async def test_florence_refuses_free_form_prompts(florence: Florence2VisionModel) -> None:
    assert florence.tasks is not None and "<OCR>" in florence.tasks
    with pytest.raises(ModelError):
        await florence.analyze([png_bytes()], "What is the invoice number?")


@pytest.mark.usefixtures("fake_ml_stack")
@pytest.mark.parametrize(("model_type", "backend"), [("florence2", Florence2VisionModel), ("qwen3_vl", LocalTransformersVisionModel)])
def test_load_local_model_picks_backend_from_config(model_type: str, backend: type[VisionModel]) -> None:
    FakeConfig.model_type = model_type
    assert type(load_local_model("org/any", device="cpu")) is backend


class TaskMock(MockVisionModel):
    name = "task-mock"
    tasks = {"<OCR>": "Read all text", "<CAPTION>": "Short description"}


async def test_task_models_reject_questions_with_the_valid_tasks(
    settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str]
) -> None:
    files = [("images", ("page.png", png_bytes(), "image/png"))]
    async with make_client(settings, model=TaskMock()) as client:
        response = await client.post(VISION, headers=auth_headers, data={"prompt": "What is this?"}, files=files)
        assert response.status_code == 400
        assert error_of(response)["message"] == (
            "Model 'task-mock' takes a task instead of a question. Set prompt to one of: <OCR>, <CAPTION>"
        )
        response = await client.post(VISION, headers=auth_headers, data={"prompt": "<OCR>"}, files=files)
        assert response.status_code == 200, response.text


@pytest.mark.usefixtures("fake_ml_stack")
async def test_lifespan_uses_real_backend_when_mock_disabled(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"use_mock_model": False, "model_name": "org/tiny-vlm"}))
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.vision_service.model, LocalTransformersVisionModel)
        assert app.state.vision_service.model.name == "org/tiny-vlm"


# ===========================================================================
# OpenAPI documentation completeness
# ===========================================================================


@pytest.fixture
async def openapi_spec(client: httpx.AsyncClient) -> dict[str, Any]:
    return (await client.get("/openapi.json")).json()


def _operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [(path, method, op) for path, item in spec["paths"].items() for method, op in item.items()]


def _example_values(media: dict[str, Any]) -> list[Any]:
    values = [example["value"] for example in media.get("examples", {}).values()]
    if "example" in media:
        values.append(media["example"])
    return values


async def test_every_endpoint_has_summary_description_and_documented_responses(
    openapi_spec: dict[str, Any],
) -> None:
    operations = _operations(openapi_spec)
    assert {("/health", "get"), (VISION, "post"), ("/v1/jobs/{job_id}", "get")} <= {(p, m) for p, m, _ in operations}
    for path, method, op in operations:  # applies to every endpoint, including future ones
        assert op.get("summary"), (path, method)
        assert len(op.get("description", "")) > 80, (path, method)
        for code, response in op["responses"].items():
            assert response.get("description"), (path, method, code)
            if code == "204":
                continue  # no body, nothing to exemplify
            content = response.get("content", {})
            if content and "application/json" not in content:  # binary (e.g. a page image): a schema is enough
                assert all(media.get("schema") for media in content.values()), (path, method, code)
                continue
            media = content.get("application/json", {})
            assert _example_values(media), f"{method.upper()} {path} {code} has no example"


def _response_model(media: dict[str, Any]) -> type[Any]:
    """The Pydantic model a documented response refers to (via its ``$ref``)."""
    import inspect

    from pydantic import BaseModel

    from app.api.routes import health
    from app.schemas import auth, dashboard, invoice, usage, vision

    models = {
        name: obj
        for module in (health, auth, dashboard, invoice, usage, vision)
        for name, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, BaseModel)
    }
    return models[media["schema"]["$ref"].rsplit("/", 1)[-1]]


async def test_every_documented_example_is_valid(openapi_spec: dict[str, Any]) -> None:
    checked = 0
    for _path, _method, op in _operations(openapi_spec):
        for response in op["responses"].values():
            media = response.get("content", {}).get("application/json", {})
            for value in _example_values(media):
                _response_model(media).model_validate(value)
                checked += 1
    assert checked >= 40


async def test_request_body_examples_for_both_content_types(openapi_spec: dict[str, Any]) -> None:
    body = openapi_spec["paths"][VISION]["post"]["requestBody"]["content"]

    json_examples = body["application/json"]["examples"]
    assert {"image_url", "tuned", "async"} <= set(json_examples)
    for example in json_examples.values():
        VisionRequest.model_validate(example["value"])

    multipart = body["multipart/form-data"]
    assert len(multipart["examples"]) >= 2
    assert multipart["schema"]["required"] == ["prompt"]
    assert multipart["schema"]["properties"]["images"] == {
        "type": "array",
        "items": {"type": "string", "format": "binary"},
        "title": "Images",
        "description": multipart["schema"]["properties"]["images"]["description"],
    }
    assert "image/jpeg" in multipart["encoding"]["images"]["contentType"]


async def test_multipart_form_mirrors_the_json_schema(openapi_spec: dict[str, Any]) -> None:
    properties = openapi_spec["paths"][VISION]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"][
        "properties"
    ]
    # Every JSON field is also a form field (plus the file field), so the two cannot drift apart.
    assert set(properties) == set(VisionRequest.model_fields) | {"images"}
    for name, schema in properties.items():
        assert schema.get("description"), name
        assert "anyOf" not in schema, name  # form fields are flat types for Swagger's form editor
    assert properties["temperature"]["maximum"] == 2
    assert properties["mode"]["enum"] == ["sync", "async"]


def test_every_request_field_is_described() -> None:
    for name, field in VisionRequest.model_fields.items():
        assert field.description and len(field.description) > 20, name


async def test_parameters_and_headers_are_documented(openapi_spec: dict[str, Any]) -> None:
    (job_id,) = openapi_spec["paths"]["/v1/jobs/{job_id}"]["get"]["parameters"]
    assert job_id["description"]
    assert job_id["schema"]["examples"]

    responses = openapi_spec["paths"][VISION]["post"]["responses"]
    assert {"X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset", "X-Request-ID"} <= set(
        responses["200"]["headers"]
    )
    assert "Location" in responses["202"]["headers"]
    assert "Retry-After" in responses["429"]["headers"]
    assert "WWW-Authenticate" in responses["401"]["headers"]


def _documented_error(spec: dict[str, Any], path: str, method: str, code: int, name: str) -> dict[str, Any]:
    examples = spec["paths"][path][method]["responses"][str(code)]["content"]["application/json"]["examples"]
    return examples[name]["value"]["error"]


def _without_nulls(error: dict[str, Any]) -> dict[str, Any]:
    # FastAPI renders the OpenAPI document with exclude_none, which also drops null values
    # inside examples (e.g. "param": null), so compare the non-null fields.
    return {key: value for key, value in error.items() if value is not None}


@pytest.mark.parametrize(
    ("name", "code", "request_kwargs", "authenticated"),
    [
        ("missing_key", 401, {"json": {"prompt": "x"}}, False),
        ("invalid_key", 401, {"json": {"prompt": "x"}, "headers": {"Authorization": "Bearer sk_live_fake"}}, False),
        ("no_image", 400, {"json": {"prompt": "x"}}, True),
        ("unknown_model", 400, {"data": {"prompt": "x", "model": "gpt-99"}, "files": "image"}, True),
        ("invalid_image", 400, {"data": {"prompt": "x"}, "files": "text"}, True),
        ("blocked_url", 400, {"json": {"prompt": "x", "image_url": "http://169.254.169.254/latest/meta-data/"}}, True),
        ("invalid_json", 400, {"content": b"{oops", "headers": {"Content-Type": "application/json"}}, True),
        ("unsupported", 415, {"content": b"x", "headers": {"Content-Type": "text/plain"}}, True),
        ("missing_prompt", 422, {"json": {"image_url": "https://httpbin.org/image/png"}}, True),
        ("out_of_range", 422, {"json": {"prompt": "x", "temperature": 5}}, True),
        ("unknown_field", 422, {"json": {"prompt": "x", "is_admin": True}}, True),
    ],
)
async def test_documented_vision_errors_match_real_responses(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    openapi_spec: dict[str, Any],
    name: str,
    code: int,
    request_kwargs: dict[str, Any],
    authenticated: bool,
) -> None:
    kwargs = dict(request_kwargs)
    headers = {**(auth_headers if authenticated else {}), **kwargs.pop("headers", {})}
    files = kwargs.pop("files", None)
    if files == "image":
        kwargs["files"] = [("images", ("a.png", png_bytes(), "image/png"))]
    elif files == "text":
        kwargs["files"] = [("images", ("photo.jpg", b"just text", "image/jpeg"))]

    response = await client.post(VISION, headers=headers, **kwargs)
    assert response.status_code == code
    assert _without_nulls(response.json()["error"]) == _documented_error(openapi_spec, VISION, "post", code, name)


async def test_documented_rate_limit_error_matches(
    settings: Settings, make_client: ClientFactory, auth_headers: dict[str, str]
) -> None:
    async with make_client(settings.model_copy(update={"rate_limit_requests": 1})) as client:
        spec = (await client.get("/openapi.json")).json()
        await client.post(VISION, headers=auth_headers, json={"prompt": "x"})
        limited = await client.post(VISION, headers=auth_headers, json={"prompt": "x"})
    assert limited.status_code == 429
    assert _without_nulls(limited.json()["error"]) == _documented_error(spec, VISION, "post", 429, "rate_limited")


@pytest.mark.parametrize(
    ("job_id", "code", "name"),
    [("8d0c6f4e-2b1a-4c3d-9e8f-7a6b5c4d3e2f", 404, "not_found"), ("not-a-uuid", 422, "malformed_id")],
)
async def test_documented_job_errors_match_real_responses(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    openapi_spec: dict[str, Any],
    job_id: str,
    code: int,
    name: str,
) -> None:
    response = await client.get(f"/v1/jobs/{job_id}", headers=auth_headers)
    assert response.status_code == code
    assert _without_nulls(response.json()["error"]) == _documented_error(openapi_spec, "/v1/jobs/{job_id}", "get", code, name)
