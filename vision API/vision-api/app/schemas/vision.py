"""Request/response schemas for the vision API (Pydantic V2)."""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_MODEL_ALIAS = "vision-default"
_MAX_RESPONSE_FORMAT_BYTES = 16 * 1024


class VisionRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {
                    "prompt": "Describe this image in one sentence.",
                    "image_url": "https://httpbin.org/image/png",
                    "temperature": 0.2,
                    "max_tokens": 300,
                },
                {
                    "prompt": "List every UI element visible in this screenshot.",
                    "image_url": "https://example.com/screenshot.png",
                    "mode": "async",
                },
            ]
        },
    )

    prompt: str = Field(
        min_length=1,
        max_length=8000,
        description=(
            "Instruction or question about the image(s). Plain text, 1-8000 characters. Task models "
            "(Florence-2) take one of their tasks instead, e.g. `<OCR>` to read all text; see "
            "`GET /v1/dashboard/model`."
        ),
        examples=["Describe this image in one sentence."],
    )
    image_url: str | None = Field(
        default=None,
        max_length=2048,
        description=(
            "Public `http(s)` URL (port 80/443) of a JPEG, PNG, WebP or GIF image, fetched "
            "server-side with SSRF protection (private, loopback and cloud-metadata addresses "
            "are refused). Can be combined with uploaded files; at least one image is required."
        ),
        examples=["https://httpbin.org/image/png"],
    )
    model: str = Field(
        default=DEFAULT_MODEL_ALIAS,
        pattern=r"^[A-Za-z0-9._:/-]{1,200}$",
        description="`vision-default`, or the exact name of the loaded model (returned as `model` in responses).",
        examples=[DEFAULT_MODEL_ALIAS],
    )
    mode: Literal["sync", "async"] = Field(
        default="sync",
        description=(
            "`sync`: wait for the result (200). `async`: return 202 immediately with a `job_id` "
            "to poll at `GET /v1/jobs/{job_id}`."
        ),
        examples=["sync"],
    )
    temperature: float = Field(
        default=0.2,
        ge=0.0,
        le=2.0,
        description="Sampling temperature, 0-2. `0` gives deterministic (greedy) output; higher is more varied.",
        examples=[0.2],
    )
    max_tokens: int = Field(
        default=1000,
        ge=1,
        le=4096,
        description="Maximum number of tokens to generate, 1-4096.",
        examples=[300],
    )
    response_format: dict[str, Any] | None = Field(
        default=None,
        description=(
            "JSON schema the output must conform to (structured output): `result.text` is then a "
            "JSON document valid against it. Supported by chat models served through llama.cpp "
            "(VISION_MODEL_BACKEND=llama_cpp); rejected with 400 by backends that cannot enforce it "
            "(the mock, the transformers backends and task models such as PaddleOCR-VL)."
        ),
    )

    @field_validator("image_url")
    @classmethod
    def _empty_url_is_none(cls, value: str | None) -> str | None:
        return value or None

    @field_validator("response_format")
    @classmethod
    def _bounded_response_format(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and len(json.dumps(value)) > _MAX_RESPONSE_FORMAT_BYTES:
            raise ValueError(f"response_format must be at most {_MAX_RESPONSE_FORMAT_BYTES} bytes")
        return value


class VisionResult(BaseModel):
    text: str = Field(description="The model's answer.")


class VisionUsage(BaseModel):
    input_tokens: int = Field(ge=0, description="Tokens consumed by the prompt and images.")
    output_tokens: int = Field(ge=0, description="Tokens generated in the answer.")


class VisionResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "vis_3f2a9c1e7b4d4e0f9a6b8c2d1e0f3a4b",
                    "object": "vision.response",
                    "model": "mock-vision-1",
                    "created": 1767225600,
                    "result": {"text": "A cartoon pig on a white background."},
                    "usage": {"input_tokens": 263, "output_tokens": 11},
                }
            ]
        }
    )

    id: str = Field(description="Unique response id: `vis_` followed by 32 hex characters.")
    object: Literal["vision.response"] = Field(default="vision.response", description="Always `vision.response`.")
    model: str = Field(description="Name of the model that produced the result.")
    created: int = Field(description="Unix timestamp (seconds) when the response was created.")
    result: VisionResult = Field(description="Model output.")
    usage: VisionUsage = Field(description="Token accounting for this request (also recorded for billing).")


JobStatus = Literal["pending", "processing", "completed", "failed"]


class JobResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"job_id": "8d0c6f4e-2b1a-4c3d-9e8f-7a6b5c4d3e2f", "status": "pending", "result": None, "error": None},
                {
                    "job_id": "8d0c6f4e-2b1a-4c3d-9e8f-7a6b5c4d3e2f",
                    "status": "failed",
                    "result": None,
                    "error": "Model inference failed",
                },
            ]
        }
    )

    job_id: str = Field(description="Job id (UUID). Poll `GET /v1/jobs/{job_id}` for progress.")
    status: JobStatus = Field(description="Lifecycle: `pending` -> `processing` -> `completed` | `failed`.")
    result: VisionResponse | None = Field(
        default=None, description="The same payload as a synchronous response; present once `completed`."
    )
    error: str | None = Field(default=None, description="Client-safe failure reason; present when `failed`.")


class ErrorDetail(BaseModel):
    type: str = Field(description="Error category, e.g. invalid_request_error, authentication_error.")
    message: str = Field(description="Human-readable explanation, safe to show to end users.")
    param: str | None = Field(default=None, description="Offending request field, when applicable.")


class VisionErrorResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "error": {"type": "rate_limit_error", "message": "Rate limit exceeded", "param": None},
                    "request_id": "0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e",
                }
            ]
        }
    )

    error: ErrorDetail
    request_id: str | None = Field(
        default=None, description="Same value as the `X-Request-ID` header; quote it when reporting problems."
    )
