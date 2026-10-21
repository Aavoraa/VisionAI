# backend/app/providers/claude_common.py
"""Shared Claude API call: client, structured output, refusal fallback, error mapping."""

from __future__ import annotations

import json
import logging

import anthropic

from ..ai_config import AIConfig
from .base import ProviderError

log = logging.getLogger(__name__)

# Models that accept the server-side refusal fallback ("fallbacks": "default").
_FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def make_client(cfg: AIConfig) -> anthropic.Anthropic:
    ok, why = cfg.claude_ready()
    if not ok:
        raise ProviderError(why, "not_configured")
    return anthropic.Anthropic(api_key=cfg.claude_api_key, timeout=float(cfg.claude_timeout),
                               max_retries=int(cfg.max_retries))


def _send(client, params: dict, use_fallback: bool, stream: bool):
    if stream:  # long outputs (a whole answer sheet): streaming avoids HTTP timeouts
        api = client.beta.messages if use_fallback else client.messages
        extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"} if use_fallback else {}
        with api.stream(**params, **extra) as s:
            return s.get_final_message()
    if use_fallback:
        return client.beta.messages.create(**params, betas=[FALLBACK_BETA], fallbacks="default")
    return client.messages.create(**params)


def call_structured(client: anthropic.Anthropic, cfg: AIConfig, *, model: str, system: str,
                    content: list, schema: dict, max_tokens: int = 16000,
                    stream: bool = False) -> tuple[dict, dict, str]:
    """One schema-constrained Messages call. Returns (parsed_json, usage, model_used)."""
    params = dict(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": cfg.claude_effort, "format": {"type": "json_schema", "schema": schema}},
    )
    use_fallback = bool(cfg.claude_refusal_fallback) and model in _FALLBACK_MODELS
    try:
        try:
            response = _send(client, params, use_fallback, stream)
        except anthropic.BadRequestError as exc:
            if use_fallback and "fallback" in str(exc).lower():
                log.warning("refusal fallback not accepted for %s; retrying without it", model)
                response = _send(client, params, False, stream)
            else:
                raise
    except anthropic.AuthenticationError as exc:
        raise ProviderError("Claude rejected the API key. Check it in AI Configuration.", "auth") from exc
    except anthropic.PermissionDeniedError as exc:
        raise ProviderError("The Claude API key lacks permission for this model.", "auth") from exc
    except anthropic.NotFoundError as exc:
        raise ProviderError(f"Claude model '{model}' was not found. Check AI Configuration.", "not_configured") from exc
    except anthropic.RateLimitError as exc:
        raise ProviderError("Claude rate limit reached. Retry shortly.", "rate_limited", retryable=True) from exc
    except anthropic.APITimeoutError as exc:
        raise ProviderError("Claude timed out.", "timeout", retryable=True) from exc
    except anthropic.APIConnectionError as exc:
        raise ProviderError("Could not reach the Claude API (network).", "unavailable", retryable=True) from exc
    except anthropic.BadRequestError as exc:
        raise ProviderError(f"Claude rejected the request: {exc.message}", "bad_response") from exc
    except anthropic.APIStatusError as exc:
        raise ProviderError(f"Claude API error {exc.status_code}.", "unavailable",
                            retryable=exc.status_code >= 500) from exc

    if response.stop_reason == "refusal":
        category = getattr(getattr(response, "stop_details", None), "category", None)
        raise ProviderError(f"Claude declined this request ({category or 'no category'}).", "refused")
    if response.stop_reason == "max_tokens":
        raise ProviderError("Claude's answer was cut off (max_tokens).", "bad_response", retryable=True)
    text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), None)
    if not text:
        raise ProviderError("Claude returned no content.", "bad_response", retryable=True)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderError("Claude returned malformed JSON.", "bad_response", retryable=True) from exc
    usage = {
        "input_tokens": getattr(response.usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(response.usage, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
    }
    return data, usage, response.model


# Published per-million-token prices (USD) for cost estimates in reports.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.0, 50.0),
}


def estimate_cost(model: str | None, usage: dict) -> float | None:
    price = PRICES.get(model or "")
    if not price or not usage:
        return None
    return round((usage.get("input_tokens", 0) * price[0] + usage.get("output_tokens", 0) * price[1]) / 1_000_000, 6)
