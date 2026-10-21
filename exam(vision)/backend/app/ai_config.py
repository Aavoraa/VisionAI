# backend/app/ai_config.py
"""AI Configuration: Claude reads and marks. Models, effort and the key are stored in the
database (the key encrypted); nothing here depends on a local model or GPU."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from .models import Setting
from .security import decrypt_secret, encrypt_secret, mask

SECRET_KEYS = ("claude_api_key",)
MODELS = (
    ("claude-opus-5-5", "Claude Opus 5.5 — most accurate"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5 — about half the cost"),
)
EFFORTS = ("low", "medium", "high", "xhigh", "max")

DEFAULTS: dict = {
    "claude_enabled": False,
    "claude_vision_model": "claude-opus-5-5",
    "claude_eval_model": "claude-opus-5-5",
    "claude_effort": "medium",
    "claude_timeout": 600,
    "claude_refusal_fallback": True,
    "max_retries": 2,
    # Every sheet is read by Anthropic, so an administrator must acknowledge that the school's
    # consent and data-processing arrangements cover sending student pages there.
    "external_processing_acknowledged": False,
    "external_processing_acknowledged_by": None,
    # Off by default: Claude reads the name on the sheet to check it against the chosen student.
    "redact_top_percent": 0,
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
            return False, ("An administrator must acknowledge, in AI Configuration, that student pages are "
                           "sent to Anthropic before sheets can be read.")
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
    """The configuration as the UI may see it: the key masked, never returned."""
    out = dict(cfg.values)
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
