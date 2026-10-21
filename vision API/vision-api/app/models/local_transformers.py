"""In-process Hugging Face ``transformers`` backends (local MVP) and a weightless mock.

These backends load weights into the web process and serialise generation, so they are for
local validation only; production should use a dedicated inference server (vLLM/TGI).
``torch``/``transformers`` are imported lazily so mock mode needs neither.

Two model families are supported:

* chat models (SmolVLM, Qwen-VL, ...): the prompt is a free-form question;
* Florence-2: the prompt must be one of its task tokens (``<OCR>``, ``<MORE_DETAILED_CAPTION>``, ...).

``load_local_model`` picks the right backend from the checkpoint's configuration.
"""

import asyncio
import gc
import hashlib
import io
import json
import threading
from typing import Any

from PIL import Image

from app.models.base import ModelError, ModelOutput, ModelUnavailableError, VisionModel

_INSTALL_HINT = "Local inference requires extra packages: pip install torch torchvision transformers accelerate"


def _import_ml() -> tuple[Any, Any]:
    try:
        import torch
        import transformers
    except ImportError as exc:
        raise ModelUnavailableError(_INSTALL_HINT) from exc
    return torch, transformers


def load_local_model(model_name: str, device: str = "auto") -> VisionModel:
    """Load ``model_name`` with the backend matching its architecture. Blocking and slow."""
    _, transformers = _import_ml()
    try:
        model_type = transformers.AutoConfig.from_pretrained(model_name).model_type
    except (OSError, ValueError) as exc:
        raise ModelUnavailableError(f"could not load model {model_name!r}") from exc
    if model_type == "florence2":
        return Florence2VisionModel(model_name, device=device)
    return LocalTransformersVisionModel(model_name, device=device)


class LocalTransformersVisionModel(VisionModel):
    """Chat-style image-text-to-text models: the prompt is a free-form instruction or question."""

    supports_structured_output = False

    def __init__(self, model_name: str, device: str = "auto") -> None:
        """Load processor and weights. Blocking and slow: call once at startup, off the event loop."""
        torch, transformers = _import_ml()

        class StopWhenSet(transformers.StoppingCriteria):  # type: ignore[misc,name-defined]
            """Ends generation after the current token once ``event`` is set (checked every step)."""

            def __init__(self, event: threading.Event) -> None:
                self._event = event

            def __call__(self, input_ids: Any, scores: Any, **kwargs: Any) -> Any:
                return torch.full((input_ids.shape[0],), self._event.is_set(), dtype=torch.bool, device=input_ids.device)

        self.name = model_name
        self._torch = torch
        self._stop_criteria = lambda event: transformers.StoppingCriteriaList([StopWhenSet(event)])
        try:
            self._processor = transformers.AutoProcessor.from_pretrained(model_name)
            on_cpu = device == "cpu" or (device == "auto" and not torch.cuda.is_available())
            self._model = transformers.AutoModelForImageTextToText.from_pretrained(
                model_name,
                # Checkpoints are usually bfloat16, which most CPUs only emulate (many times
                # slower than float32); GPUs keep the checkpoint's own dtype.
                dtype=torch.float32 if on_cpu else "auto",
                device_map=device,
            )
        except (OSError, ValueError) as exc:
            raise ModelUnavailableError(f"could not load model {model_name!r}") from exc
        self._model.eval()
        # One generate() at a time: concurrent calls on one model would contend for (GPU) memory.
        self._lock = asyncio.Lock()

    async def analyze(self, images: list[bytes], prompt: str, options: dict[str, Any] | None = None) -> ModelOutput:
        if not images:
            raise ModelError("at least one image is required")
        opts = options or {}
        async with self._lock:
            stop = threading.Event()
            worker = asyncio.ensure_future(asyncio.to_thread(self._generate, images, prompt, opts, stop))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Timeout or disconnect: a thread cannot be killed, so ask generate() to stop and
                # keep the lock until it has. Otherwise the abandoned generation would run on at
                # full speed and slow down (or time out) every request queued behind it.
                stop.set()
                await asyncio.wait([worker])
                if not worker.cancelled():
                    worker.exception()  # retrieved: the caller is being cancelled anyway
                raise
            except ModelError:
                raise
            except Exception as exc:  # torch/transformers raise a wide variety of runtime errors
                raise ModelError("local inference failed") from exc

    def _to_model(self, batch: Any) -> Any:
        # Floating-point inputs (pixel values) must match the weights' dtype, e.g. bfloat16 on GPU.
        return batch.to(self._model.device, dtype=self._model.dtype)

    def _generate_kwargs(self, opts: dict[str, Any], stop: threading.Event | None) -> dict[str, Any]:
        temperature = float(opts.get("temperature", 0.0))
        kwargs: dict[str, Any] = {
            "max_new_tokens": int(opts.get("max_tokens", 1000)),
            "do_sample": temperature > 0,
        }
        if temperature > 0:
            kwargs["temperature"] = temperature
        if stop is not None:
            kwargs["stopping_criteria"] = self._stop_criteria(stop)
        return kwargs

    def _generate(
        self, images: list[bytes], prompt: str, opts: dict[str, Any], stop: threading.Event | None = None
    ) -> ModelOutput:
        pil_images = [Image.open(io.BytesIO(data)).convert("RGB") for data in images]
        content: list[dict[str, Any]] = [{"type": "image", "image": img} for img in pil_images]
        content.append({"type": "text", "text": prompt})
        messages = [{"role": "user", "content": content}]

        inputs = self._to_model(
            self._processor.apply_chat_template(
                messages,
                add_generation_prompt=True,
                tokenize=True,
                return_dict=True,
                return_tensors="pt",
            )
        )
        with self._torch.inference_mode():
            output_ids = self._model.generate(**inputs, **self._generate_kwargs(opts, stop))

        input_length = int(inputs["input_ids"].shape[1])
        generated = output_ids[:, input_length:]
        text = self._processor.batch_decode(generated, skip_special_tokens=True)[0].strip()
        return ModelOutput(text=text, input_tokens=input_length, output_tokens=int(generated.shape[1]))

    async def close(self) -> None:
        del self._model
        gc.collect()
        if self._torch.cuda.is_available():
            self._torch.cuda.empty_cache()


class Florence2VisionModel(LocalTransformersVisionModel):
    """Microsoft Florence-2: strong OCR and captioning, driven by task tokens instead of questions.

    Florence-2 sees every image at 768x768, so small print on a whole page becomes unreadable.
    ``<OCR>`` therefore reads tall images in overlapping horizontal strips (each about 0.4x as
    tall as the image is wide) and joins the text. ``max_tokens`` applies to each strip.
    Region tasks return the model's structured result (labels and pixel boxes) as JSON text.
    """

    tasks = {
        "<OCR>": "Read all text in the image",
        "<OCR_WITH_REGION>": "Read text with bounding boxes (JSON)",
        "<CAPTION>": "Short description",
        "<DETAILED_CAPTION>": "Detailed description",
        "<MORE_DETAILED_CAPTION>": "Very detailed description",
        "<OD>": "Detect objects with bounding boxes (JSON)",
    }
    STRIP_HEIGHT_RATIO = 0.4
    STRIP_OVERLAP_RATIO = 0.04

    def _generate(
        self, images: list[bytes], prompt: str, opts: dict[str, Any], stop: threading.Event | None = None
    ) -> ModelOutput:
        task = prompt.strip()
        if task not in self.tasks:  # the service validates first; this guards direct callers
            raise ModelError(f"unsupported Florence-2 task {task!r}")
        # Greedy decoding: as accurate as beam search on our OCR tests, at half the cost.
        kwargs = self._generate_kwargs({**opts, "temperature": 0.0}, stop)

        texts: list[str] = []
        input_tokens = output_tokens = 0
        for data in images:
            image = Image.open(io.BytesIO(data)).convert("RGB")
            regions = self.ocr_strips(image.size) if task == "<OCR>" else [(0, 0, *image.size)]
            parts: list[str] = []
            for box in regions:
                if stop is not None and stop.is_set():
                    break
                crop = image if box == (0, 0, *image.size) else image.crop(box)
                inputs = self._to_model(self._processor(text=task, images=crop, return_tensors="pt"))
                with self._torch.inference_mode():
                    output_ids = self._model.generate(**inputs, **kwargs)
                raw = self._processor.batch_decode(output_ids, skip_special_tokens=False)[0]
                parsed = self._processor.post_process_generation(raw, task=task, image_size=crop.size)[task]
                parts.append(parsed.strip() if isinstance(parsed, str) else json.dumps(parsed, ensure_ascii=False))
                input_tokens += int(inputs["input_ids"].shape[1])
                output_tokens += int(output_ids.shape[1])
            texts.append("\n".join(part for part in parts if part))

        text = texts[0] if len(texts) == 1 else "\n\n".join(f"Image {i}:\n{t}" for i, t in enumerate(texts, 1))
        return ModelOutput(text=text, input_tokens=input_tokens, output_tokens=output_tokens)

    @classmethod
    def ocr_strips(cls, size: tuple[int, int]) -> list[tuple[int, int, int, int]]:
        """Full-width, slightly overlapping horizontal strips (left, top, right, bottom) covering the image."""
        width, height = size
        strip = max(1, round(width * cls.STRIP_HEIGHT_RATIO))
        if height <= strip * 1.25:  # close to the strip shape already: one pass reads it fine
            return [(0, 0, width, height)]
        overlap = round(width * cls.STRIP_OVERLAP_RATIO)
        boxes: list[tuple[int, int, int, int]] = []
        top = 0
        while True:
            bottom = min(height, top + strip)
            if height - bottom < strip * 0.25:  # fold a thin remainder into this strip
                bottom = height
            boxes.append((0, top, width, bottom))
            if bottom == height:
                return boxes
            top = bottom - overlap


class MockVisionModel(VisionModel):
    """Deterministic, instant fake for tests and local development. Loads no weights."""

    name = "mock-vision-1"
    supports_structured_output = False
    IMAGE_TOKENS = 256  # rough per-image token cost, mimicking real VLM accounting

    def __init__(self, latency_seconds: float = 0.0) -> None:
        self._latency_seconds = latency_seconds

    async def analyze(self, images: list[bytes], prompt: str, options: dict[str, Any] | None = None) -> ModelOutput:
        if not images:
            raise ModelError("at least one image is required")
        if self._latency_seconds > 0:
            await asyncio.sleep(self._latency_seconds)

        opts = options or {}
        digests = ", ".join(hashlib.sha256(data).hexdigest()[:12] for data in images)
        prompt_words = prompt.split()
        words = (
            f"Mock analysis of {len(images)} image(s) [{digests}] for prompt: {' '.join(prompt_words[:50])}"
        ).split()
        words = words[: int(opts.get("max_tokens", 1000))]
        return ModelOutput(
            text=" ".join(words),
            input_tokens=len(prompt_words) + self.IMAGE_TOKENS * len(images),
            output_tokens=len(words),
        )
