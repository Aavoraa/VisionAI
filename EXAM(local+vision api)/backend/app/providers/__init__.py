# backend/app/providers/__init__.py
"""Provider registry. Tests replace the factories with fakes."""

from __future__ import annotations

from collections.abc import Callable

from ..ai_config import AIConfig
from .base import ProviderError

VISION_FACTORIES: dict[str, Callable] = {}
EVALUATOR_FACTORIES: dict[str, Callable] = {}


def _defaults() -> None:
    from .claude_vision import ClaudeVisionProvider
    from .evaluators import ClaudeEvaluator, OwnLLMEvaluator
    from .own_vision import OwnVisionProvider

    VISION_FACTORIES.setdefault("own", OwnVisionProvider)
    VISION_FACTORIES.setdefault("claude", ClaudeVisionProvider)
    EVALUATOR_FACTORIES.setdefault("claude", ClaudeEvaluator)
    EVALUATOR_FACTORIES.setdefault("own_llm", OwnLLMEvaluator)


def vision_provider(name: str, cfg: AIConfig):
    _defaults()
    factory = VISION_FACTORIES.get(name)
    if factory is None:
        raise ProviderError(f"Unknown vision provider '{name}'.", "not_configured")
    return factory(cfg)


def evaluator(name: str, cfg: AIConfig):
    _defaults()
    factory = EVALUATOR_FACTORIES.get(name)
    if factory is None:
        raise ProviderError(f"Unknown evaluation provider '{name}'.", "not_configured")
    return factory(cfg)
