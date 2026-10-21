# backend/app/ai_config.py
"""AI Configuration: provider choices, endpoints, models and keys (stored in the database)."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from .models import Exam, Setting
from .security import decrypt_secret, encrypt_secret, mask

VISION_PROVIDERS = ("own", "claude")
EVALUATION_PROVIDERS = ("claude", "own_llm")
SECRET_KEYS = ("own_vision_key", "claude_api_key")

DEFAULTS: dict = {
    "vision_provider": "own",
    "evaluation_provider": "own_llm",  # the local LLM (own_llm_url); Claude is off by default
    "own_vision_enabled": True,
    "own_vision_url": "http://127.0.0.1:8002",
    "own_vision_model": "",  # empty = the Vision API's default model
    "own_vision_timeout": 180,
    "own_vision_bands": 2,  # read each page in N horizontal bands for more resolution
    "own_llm_url": "http://127.0.0.1:8091",
    "own_llm_timeout": 180,
    "claude_enabled": False,
    "claude_vision_model": "claude-opus-5-5",
    "claude_eval_model": "claude-opus-5-5",
    "claude_effort": "medium",
    "claude_timeout": 180,
    "claude_refusal_fallback": True,
    "max_retries": 2,
    # Sending student pages to an external provider needs an administrator's explicit
    # acknowledgement (consent and data-processing duties are the school's).
    "external_processing_acknowledged": False,
    "external_processing_acknowledged_by": None,
    "redact_top_percent": 12,  # blank the top of page 1 (name / roll no) before external vision
    "auto_fallback_to_claude": False,  # off: never switch providers silently
    "comparison_enabled": True,
}


@dataclass
class AIConfig:
    values: dict = field(default_factory=dict)
    secrets: dict = field(default_factory=dict)

    def __getattr__(self, item):
        values = self.__dict__.get("values", {})
        if item in values:
            return values[item]
        raise AttributeError(item)

    @property
    def own_vision_key(self) -> str | None:
        return self.secrets.get("own_vision_key")

    @property
    def claude_api_key(self) -> str | None:
        return self.secrets.get("claude_api_key")

    def claude_ready(self) -> tuple[bool, str]:
        if not self.claude_enabled:
            return False, "Claude is disabled in AI Configuration."
        if not self.claude_api_key:
            return False, "No Claude API key is set in AI Configuration."
        return True, ""

    def claude_vision_ready(self) -> tuple[bool, str]:
        ok, why = self.claude_ready()
        if not ok:
            return ok, why
        if not self.external_processing_acknowledged:
            return False, ("An administrator must acknowledge external processing of student pages "
                           "in AI Configuration before Claude Vision can be used.")
        return True, ""


def load(db: Session) -> AIConfig:
    values = dict(DEFAULTS)
    secrets: dict = {}
    for row in db.query(Setting).all():
        if row.key in SECRET_KEYS:
            secrets[row.key] = decrypt_secret(row.secret)
        elif row.key in DEFAULTS:
            values[row.key] = row.value
    return AIConfig(values=values, secrets=secrets)


def public_view(cfg: AIConfig) -> dict:
    """The configuration as the UI may see it: secrets masked, never returned."""
    out = dict(cfg.values)
    out["own_vision_key"] = mask(cfg.own_vision_key)
    out["claude_api_key"] = mask(cfg.claude_api_key)
    return out


def save(db: Session, changes: dict, user_id: str | None) -> list[str]:
    """Apply ``changes``; returns the keys changed. Secret values: "" clears, None keeps."""
    changed = []
    for key, value in changes.items():
        if key in SECRET_KEYS:
            if value is None:
                continue
            row = db.get(Setting, key) or Setting(key=key)
            row.secret = encrypt_secret(value) if value else None
            row.value = None
        elif key in DEFAULTS:
            row = db.get(Setting, key) or Setting(key=key)
            row.value = value
        else:
            continue
        row.updated_by = user_id
        db.add(row)
        changed.append(key)
    return changed


def vision_provider_for(cfg: AIConfig, exam: Exam | None) -> str:
    return (exam.vision_provider if exam and exam.vision_provider else None) or cfg.vision_provider


def evaluation_provider_for(cfg: AIConfig, exam: Exam | None) -> str:
    return (exam.evaluation_provider if exam and exam.evaluation_provider else None) or cfg.evaluation_provider
