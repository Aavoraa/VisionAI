"""Orchestrates image intake -> sanitisation -> model inference -> response formatting."""

import asyncio
import logging
import time
import uuid
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.security import fetch_image_from_url
from app.database.models import Job
from app.models.base import ModelError, ModelOutput, ModelTimeoutError, VisionModel
from app.schemas.vision import DEFAULT_MODEL_ALIAS, VisionRequest, VisionResponse, VisionResult, VisionUsage
from app.services.image_service import process_image
from app.services.usage_service import UsageContext, record_usage

logger = logging.getLogger(__name__)

_MB = 1024 * 1024
MODEL_TIMEOUT_MESSAGE = "Model inference timed out"
MODEL_FAILURE_MESSAGE = "Model inference failed"


class VisionService:
    def __init__(self, model: VisionModel, settings: Settings) -> None:
        self.model = model
        self._settings = settings

    @property
    def _max_size_mb(self) -> int:
        return max(1, self._settings.max_image_bytes // _MB)

    def validate(self, request: VisionRequest, upload_count: int) -> None:
        """Cheap request checks (model, options, image count); raises ``HTTPException(400)``."""
        if request.model not in (DEFAULT_MODEL_ALIAS, self.model.name):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown model {request.model!r}. Available: {DEFAULT_MODEL_ALIAS!r}, {self.model.name!r}",
            )
        if request.response_format is not None and not self.model.supports_structured_output:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"response_format is not supported by model {self.model.name!r}",
            )
        tasks = self.model.tasks
        if tasks is not None and request.prompt.strip() not in tasks:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Model {self.model.name!r} takes a task instead of a question. "
                    f"Set prompt to one of: {', '.join(tasks)}"
                ),
            )
        total = upload_count + (1 if request.image_url else 0)
        if total == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Provide at least one image (file upload or image_url)",
            )
        if total > self._settings.max_images_per_request:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"At most {self._settings.max_images_per_request} images per request",
            )

    async def _sanitise(self, raw: bytes) -> bytes:
        # Pillow work is CPU-bound; keep it off the event loop.
        return await asyncio.to_thread(process_image, raw, self._max_size_mb, self._settings.max_image_side)

    async def _collect_images(self, request: VisionRequest, uploaded_files: list[bytes]) -> list[bytes]:
        images = list(await asyncio.gather(*(self._sanitise(raw) for raw in uploaded_files)))
        if request.image_url:
            fetched = await fetch_image_from_url(
                request.image_url,
                max_size_mb=self._max_size_mb,
                timeout=self._settings.fetch_total_timeout,
            )
            images.append(await self._sanitise(fetched))
        return images

    async def _infer(self, images: list[bytes], request: VisionRequest) -> ModelOutput:
        options: dict[str, object] = {"temperature": request.temperature, "max_tokens": request.max_tokens}
        if request.response_format is not None:
            options["response_format"] = request.response_format
        try:
            async with asyncio.timeout(self._settings.model_timeout_seconds):
                output = await self.model.analyze(images, request.prompt, options)
        except TimeoutError as exc:
            raise ModelTimeoutError("inference timed out") from exc

        if (
            not isinstance(output.get("text"), str)
            or not isinstance(output.get("input_tokens"), int)
            or not isinstance(output.get("output_tokens"), int)
        ):
            raise ModelError("model returned a malformed result")
        return output

    async def process_request(
        self, request: VisionRequest, uploaded_files: list[bytes] | None = None
    ) -> VisionResponse:
        uploads = uploaded_files or []
        self.validate(request, len(uploads))
        images = await self._collect_images(request, uploads)

        started = time.perf_counter()
        output = await self._infer(images, request)
        logger.info(
            "inference complete",
            extra={
                "model": self.model.name,
                "image_count": len(images),
                "input_tokens": output["input_tokens"],
                "output_tokens": output["output_tokens"],
                "inference_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return VisionResponse(
            id=f"vis_{uuid.uuid4().hex}",
            model=self.model.name,
            created=int(time.time()),
            result=VisionResult(text=output["text"]),
            usage=VisionUsage(input_tokens=output["input_tokens"], output_tokens=output["output_tokens"]),
        )

    async def run_job(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        job_id: str,
        request: VisionRequest,
        uploaded_files: list[bytes],
        usage: UsageContext,
    ) -> None:
        """Background execution of an async job. Never raises: every outcome is persisted."""
        if not await _update_job(session_factory, job_id, status="processing"):
            return
        started = time.perf_counter()
        try:
            response = await self.process_request(request, uploaded_files or None)
        except Exception as exc:  # the job must reach a terminal state whatever went wrong
            message = public_error_message(exc)
            if not isinstance(exc, (HTTPException, ModelError)):
                logger.exception("async job crashed", extra={"job_id": job_id})
            await _update_job(session_factory, job_id, status="failed", error_message=message)
            await record_usage(
                session_factory, usage, status="error", error_message=message, processing_time_ms=_elapsed_ms(started)
            )
            return

        await _update_job(session_factory, job_id, status="completed", result_data=response.model_dump(mode="json"))
        await record_usage(
            session_factory,
            usage,
            status="success",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            processing_time_ms=_elapsed_ms(started),
        )


def public_error_message(exc: BaseException) -> str:
    """Client-safe description of a failure (internal details are only logged)."""
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    if isinstance(exc, ModelTimeoutError):
        return MODEL_TIMEOUT_MESSAGE
    if isinstance(exc, ModelError):
        return MODEL_FAILURE_MESSAGE
    return "Internal server error"


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


async def _update_job(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: str,
    *,
    status: str,
    result_data: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> bool:
    try:
        async with session_factory() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(status=status, result_data=result_data, error_message=error_message)
            )
            await session.commit()
        return True
    except Exception:  # background context: nothing upstream can handle it, so log and report
        logger.exception("failed to update job", extra={"job_id": job_id, "job_status": status})
        return False
