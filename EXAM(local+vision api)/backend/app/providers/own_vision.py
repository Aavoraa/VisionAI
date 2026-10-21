# backend/app/providers/own_vision.py
"""Provider A: our own Vision AI service (PaddleOCR-VL on the local GPU) via its /v1/vision API."""

from __future__ import annotations

import io
import time

import httpx
from PIL import Image

from ..ai_config import AIConfig
from ..services import ocr_guard
from ..services.documents import has_writing, split_bands
from .base import HealthResult, PageExtraction, ProviderError

# PaddleOCR-VL task prompt. (Its "Formula Recognition:" task is for cropped formulas; on a whole
# handwritten page it loops, so it is not used. Plain OCR reads written calculations correctly.)
OCR_PROMPT = "OCR:"


class OwnVisionProvider:
    name = "own"

    def __init__(self, cfg: AIConfig, client: httpx.Client | None = None):
        if not cfg.own_vision_enabled:
            raise ProviderError("Our Vision AI is disabled in AI Configuration.", "not_configured")
        if not cfg.own_vision_key:
            raise ProviderError("No Vision API key is set in AI Configuration.", "not_configured")
        self.cfg = cfg
        self.url = cfg.own_vision_url.rstrip("/")
        self.client = client or httpx.Client(timeout=float(cfg.own_vision_timeout))
        self._model: str | None = cfg.own_vision_model or None

    @property
    def model(self) -> str | None:
        return self._model

    def _call(self, image: bytes, prompt: str) -> str:
        data = {"prompt": prompt, "temperature": "0", "max_tokens": "2048"}
        if self.cfg.own_vision_model:
            data["model"] = self.cfg.own_vision_model
        try:
            resp = self.client.post(
                f"{self.url}/v1/vision",
                headers={"Authorization": f"Bearer {self.cfg.own_vision_key}"},
                data=data,
                files={"images": ("page.png", image, "image/png")},
            )
        except httpx.TimeoutException as exc:
            raise ProviderError("Our Vision AI timed out.", "timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not reach our Vision AI at {self.url}.", "unavailable", retryable=True) from exc
        if resp.status_code in (401, 403):
            raise ProviderError("Our Vision AI rejected the API key.", "auth")
        if resp.status_code == 429:
            raise ProviderError("Our Vision AI rate limit or quota reached.", "rate_limited", retryable=True)
        if resp.status_code >= 500:
            raise ProviderError(f"Our Vision AI returned {resp.status_code}.", "unavailable", retryable=True)
        if resp.status_code != 200:
            raise ProviderError(f"Our Vision AI returned {resp.status_code}: {resp.text[:200]}", "bad_response")
        body = resp.json()
        self._model = body.get("model") or self._model
        text = (body.get("result") or {}).get("text")
        if text is None:
            raise ProviderError("Our Vision AI returned no text.", "bad_response")
        return text

    def extract_page(self, image: bytes, page_no: int, options: dict | None = None) -> PageExtraction:
        started = time.perf_counter()
        img = Image.open(io.BytesIO(image))
        bands = split_bands(img, int(self.cfg.own_vision_bands or 1))
        texts, warnings = [], []
        skipped = 0
        for band in bands:
            if not has_writing(band):
                skipped += 1  # blank paper: sending it only invites the model to invent text
                continue
            buf = io.BytesIO()
            band.save(buf, format="PNG")
            cleaned, notes = ocr_guard.clean(self._call(buf.getvalue(), OCR_PROMPT))
            texts.append(cleaned)
            warnings += notes
        if skipped and skipped < len(bands):
            warnings.append(f"{skipped} blank area(s) of the page skipped")
        text = "\n".join(t for t in texts if t)
        blank = len(text.strip()) < 3
        if blank:
            warnings.append("no text read on this page (blank or unreadable)")
        return PageExtraction(
            page_no=page_no, text=text, warnings=warnings, uncertain=[], confidence=None, regions=None,
            blank=blank, duration_ms=int((time.perf_counter() - started) * 1000),
        )

    def health_check(self) -> HealthResult:
        started = time.perf_counter()
        try:
            resp = self.client.get(f"{self.url}/health", timeout=10)
            ok = resp.status_code == 200
            detail = "reachable" if ok else f"health returned {resp.status_code}"
        except httpx.HTTPError:
            ok, detail = False, f"cannot reach {self.url}"
        return HealthResult(ok=ok, provider=self.name, model=self.model, detail=detail,
                            duration_ms=int((time.perf_counter() - started) * 1000))
