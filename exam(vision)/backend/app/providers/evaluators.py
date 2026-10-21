# backend/app/providers/evaluators.py
"""Answer marking with Claude. Returns the raw marking JSON; services/scoring validates it."""

from __future__ import annotations

import time

import anthropic

from ..ai_config import AIConfig
from ..services.prompts import SYSTEM
from .base import HealthResult
from .claude_common import call_structured, make_client


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
