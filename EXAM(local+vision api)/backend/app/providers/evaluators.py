# backend/app/providers/evaluators.py
"""Answer-evaluation providers: Claude (recommended) and our own local LLM (experimental).

Both return the raw marking JSON; the scoring service validates it deterministically.
"""

from __future__ import annotations

import json
import time
from typing import Protocol

import anthropic
import httpx

from ..ai_config import AIConfig
from ..services.prompts import SYSTEM
from .base import HealthResult, ProviderError
from .claude_common import call_structured, make_client


class Evaluator(Protocol):
    name: str
    model: str | None

    def evaluate(self, user_message: str, schema: dict) -> tuple[dict, dict]: ...

    def health_check(self) -> HealthResult: ...


class ClaudeEvaluator:
    name = "claude"

    def __init__(self, cfg: AIConfig, client: anthropic.Anthropic | None = None):
        self.cfg = cfg
        self.client = client or make_client(cfg)
        self.model: str | None = cfg.claude_eval_model

    def evaluate(self, user_message: str, schema: dict) -> tuple[dict, dict]:
        data, usage, model = call_structured(
            self.client, self.cfg, model=self.cfg.claude_eval_model, system=SYSTEM, schema=schema,
            content=[{"type": "text", "text": user_message}], max_tokens=16000,
        )
        self.model = model
        return data, usage

    def health_check(self) -> HealthResult:
        started = time.perf_counter()
        try:
            info = self.client.models.retrieve(self.cfg.claude_eval_model)
        except anthropic.AuthenticationError:
            return HealthResult(False, self.name, self.model, "Claude rejected the API key.")
        except anthropic.NotFoundError:
            return HealthResult(False, self.name, self.model, f"model {self.cfg.claude_eval_model} not found")
        except anthropic.APIError as exc:
            return HealthResult(False, self.name, self.model, f"Claude API error: {exc.__class__.__name__}")
        return HealthResult(True, self.name, info.id, f"{info.display_name} available",
                            int((time.perf_counter() - started) * 1000))


class OwnLLMEvaluator:
    """Our local Qwen3-VL-2B (llama.cpp server). Experimental: too small for reliable marking;
    every result it produces is sent for teacher review."""

    name = "own_llm"

    def __init__(self, cfg: AIConfig, client: httpx.Client | None = None):
        self.cfg = cfg
        self.url = cfg.own_llm_url.rstrip("/")
        self.client = client or httpx.Client(timeout=float(cfg.own_llm_timeout))
        self.model: str | None = None

    def evaluate(self, user_message: str, schema: dict) -> tuple[dict, dict]:
        body = {
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_message}],
            "temperature": 0,
            "max_tokens": 2048,
            "response_format": {"type": "json_schema", "json_schema": {"name": "marking", "schema": schema}},
        }
        try:
            resp = self.client.post(f"{self.url}/v1/chat/completions", json=body)
        except httpx.TimeoutException as exc:
            raise ProviderError("Our local LLM timed out.", "timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not reach our local LLM at {self.url}.", "unavailable", retryable=True) from exc
        if resp.status_code != 200:
            raise ProviderError(f"Our local LLM returned {resp.status_code}.", "unavailable",
                                retryable=resp.status_code >= 500)
        payload = resp.json()
        self.model = payload.get("model")
        try:
            data = json.loads(payload["choices"][0]["message"]["content"])
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise ProviderError("Our local LLM returned malformed JSON.", "bad_response", retryable=True) from exc
        data["requires_teacher_review"] = True
        data.setdefault("review_reasons", []).append("marked by the experimental local model")
        usage = payload.get("usage") or {}
        return data, {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)}

    def health_check(self) -> HealthResult:
        started = time.perf_counter()
        try:
            ok = self.client.get(f"{self.url}/health", timeout=10).status_code == 200
        except httpx.HTTPError:
            ok = False
        return HealthResult(ok, self.name, self.model, "reachable" if ok else f"cannot reach {self.url}",
                            int((time.perf_counter() - started) * 1000))
