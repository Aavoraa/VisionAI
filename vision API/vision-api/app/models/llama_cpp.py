"""Backend for GGUF models served by a local llama.cpp ``llama-server`` (OpenAI-compatible API).

llama.cpp runs quantised models much faster than PyTorch on a CPU and has a CUDA build for
GPUs. The server is a separate process (see README, "GGUF models with llama.cpp"); this
backend only sends it images over HTTP, normally on 127.0.0.1, so documents never leave
the machine.

PaddleOCR-VL is a document-parsing model driven by fixed task prompts (``OCR:``,
``Table Recognition:`` ...), so it is exposed as a task model; other GGUF vision models
(e.g. Qwen-VL) take free-form questions.
"""

import asyncio
import base64
from typing import Any

import httpx

from app.models.base import ModelError, ModelOutput, ModelUnavailableError, VisionModel

# llama.cpp's DRY sampler penalises repeating a sequence that already occurred: it stops the
# "DISPATCH DISPATCH DISPATCH ..." loops small OCR models sometimes fall into, and leaves
# normal output unchanged. Not for tables, where rows legitimately repeat.
DRY_SAMPLING = {"dry_multiplier": 0.8, "dry_base": 1.75, "dry_allowed_length": 2}

PADDLEOCR_VL_TASKS = {
    "OCR:": "Read all text (document order, tables as text)",
    "Table Recognition:": "Read the main table (structured)",
    "Chart Recognition:": "Convert a chart to a table",
    "Formula Recognition:": "Read formulas (LaTeX)",
    "Seal Recognition:": "Read text in stamps and seals",
}


def _is_paddleocr_vl(model_name: str) -> bool:
    return "paddleocr-vl" in model_name.lower().replace("_", "-")


class LlamaCppVisionModel(VisionModel):
    supports_structured_output = False

    def __init__(
        self,
        model_name: str,
        base_url: str,
        *,
        timeout_seconds: float,
        concurrency: int = 1,
        transport: httpx.MockTransport | None = None,  # tests only
    ) -> None:
        """Check that the server is up (blocking; runs at startup, off the event loop)."""
        self.name = model_name
        self.tasks = PADDLEOCR_VL_TASKS if _is_paddleocr_vl(model_name) else None
        # llama.cpp compiles a JSON schema into a grammar, so chat models can guarantee
        # schema-valid output; task models (PaddleOCR-VL) produce their own formats.
        self.supports_structured_output = self.tasks is None
        self._base_url = base_url.rstrip("/")
        try:
            with httpx.Client(transport=transport, timeout=5.0) as client:
                health = client.get(f"{self._base_url}/health")
        except httpx.HTTPError as exc:
            raise ModelUnavailableError(
                f"llama.cpp server not reachable at {self._base_url}; start llama-server first"
            ) from exc
        if health.status_code != 200:
            raise ModelUnavailableError(f"llama.cpp server at {self._base_url} is not ready ({health.status_code})")
        # The service enforces the inference deadline; this only stops a hung connection.
        self._client = httpx.AsyncClient(
            base_url=self._base_url, timeout=httpx.Timeout(timeout_seconds + 30), transport=transport
        )
        # Requests in flight at once. On a CPU, or a GPU already busy, 1 is fastest; a small model on
        # a GPU decodes 2 sequences together for little more than the cost of one (llama-server
        # has 4 slots). Set by VISION_LLAMA_CPP_CONCURRENCY after benchmarking.
        self._lock = asyncio.Semaphore(max(1, concurrency))

    async def analyze(self, images: list[bytes], prompt: str, options: dict[str, Any] | None = None) -> ModelOutput:
        if not images:
            raise ModelError("at least one image is required")
        opts = options or {}
        texts: list[str] = []
        input_tokens = output_tokens = 0
        async with self._lock:
            # One image per call: document models read a single page at a time. Cancelling
            # (timeout, client gone) closes the connection, which makes llama-server stop.
            for data in images:
                body = await self._complete(data, prompt.strip() if self.tasks else prompt, opts)
                try:
                    texts.append(str(body["choices"][0]["message"]["content"] or "").strip())
                    usage = body.get("usage") or {}
                    input_tokens += int(usage.get("prompt_tokens", 0))
                    output_tokens += int(usage.get("completion_tokens", 0))
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    raise ModelError("llama.cpp returned a malformed response") from exc

        text = texts[0] if len(texts) == 1 else "\n\n".join(f"Image {i}:\n{t}" for i, t in enumerate(texts, 1))
        return ModelOutput(text=text, input_tokens=input_tokens, output_tokens=output_tokens)

    async def _complete(self, image: bytes, prompt: str, opts: dict[str, Any]) -> dict[str, Any]:
        mime = "image/png" if image.startswith(b"\x89PNG") else "image/jpeg"
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image).decode()}"}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "temperature": 0.0 if self.tasks else float(opts.get("temperature", 0.0)),
            "max_tokens": int(opts.get("max_tokens", 1000)),
            "stream": False,
        }
        if opts.get("anti_repetition"):
            payload.update(DRY_SAMPLING)
        if schema := opts.get("response_format"):
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "response", "schema": schema}}
        try:
            response = await self._client.post("/v1/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise ModelUnavailableError("llama.cpp server request failed") from exc
        if response.status_code != 200:
            raise ModelError(f"llama.cpp server returned HTTP {response.status_code}")
        try:
            body: dict[str, Any] = response.json()
        except ValueError as exc:
            raise ModelError("llama.cpp returned a malformed response") from exc
        return body

    async def close(self) -> None:
        await self._client.aclose()
