# backend/app/routers/settings.py
"""Settings -> AI Configuration (administrators). Keys go in, never come back out."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from PIL import Image, ImageDraw
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import ai_config, providers
from ..db import get_db
from ..models import now
from ..providers.base import ProviderError
from ..security import AdminUser, StaffUser, audit
from ..services.documents import to_png

router = APIRouter(prefix="/api/settings", tags=["settings"])
DB = Annotated[Session, Depends(get_db)]


@router.get("/ai")
def get_ai(_: AdminUser, db: DB) -> dict:
    cfg = ai_config.load(db)
    return {"config": ai_config.public_view(cfg),
            "options": {"vision_providers": ai_config.VISION_PROVIDERS,
                        "evaluation_providers": ai_config.EVALUATION_PROVIDERS,
                        "efforts": ["low", "medium", "high", "xhigh", "max"]}}


@router.get("/ai/status")
def ai_status(_: StaffUser, db: DB) -> dict:
    """What staff screens need to know (no URLs, no keys)."""
    cfg = ai_config.load(db)
    return {"vision_provider": cfg.vision_provider, "evaluation_provider": cfg.evaluation_provider,
            "claude_vision_available": cfg.claude_vision_ready()[0],
            "claude_vision_reason": cfg.claude_vision_ready()[1],
            "claude_marking_available": cfg.claude_ready()[0],
            "comparison_enabled": bool(cfg.comparison_enabled) and cfg.claude_vision_ready()[0],
            "external_processing_acknowledged": bool(cfg.external_processing_acknowledged)}


class AIUpdate(BaseModel):
    changes: dict[str, Any]


@router.put("/ai")
def put_ai(body: AIUpdate, admin: AdminUser, db: DB) -> dict:
    changes = dict(body.changes)
    if "vision_provider" in changes and changes["vision_provider"] not in ai_config.VISION_PROVIDERS:
        raise HTTPException(400, "Unknown vision provider.")
    if "evaluation_provider" in changes and changes["evaluation_provider"] not in ai_config.EVALUATION_PROVIDERS:
        raise HTTPException(400, "Unknown evaluation provider.")
    if "claude_effort" in changes and changes["claude_effort"] not in ("low", "medium", "high", "xhigh", "max"):
        raise HTTPException(400, "Unknown effort level.")
    if changes.get("external_processing_acknowledged") is True:
        changes["external_processing_acknowledged_by"] = {"user": admin.name, "at": now().isoformat() + "Z"}
    elif changes.get("external_processing_acknowledged") is False:
        changes["external_processing_acknowledged_by"] = None
    cfg_before = ai_config.load(db)
    if changes.get("vision_provider") == "claude" or changes.get("auto_fallback_to_claude"):
        acknowledged = changes.get("external_processing_acknowledged", cfg_before.external_processing_acknowledged)
        if not acknowledged:
            raise HTTPException(400, "Acknowledge external processing of student pages first.")
    changed = ai_config.save(db, changes, admin.id)
    # never log secret values; record only which keys changed
    audit(db, admin, "ai_configuration_changed", "settings", None, keys=changed)
    db.commit()
    return get_ai(admin, db)


def _sample_page() -> bytes:
    img = Image.new("RGB", (900, 260), "white")
    d = ImageDraw.Draw(img)
    d.text((40, 60), "Q1. Plants use sunlight to make food.", fill="black")
    d.text((40, 140), "Q2. 120 / 2 = 60 km/h", fill="black")
    return to_png(img.resize((1800, 520)))


@router.post("/ai/test/{target}")
def test_provider(target: str, admin: AdminUser, db: DB, sample: bool = False) -> dict:
    """Connection test; with sample=true also a sample extraction (vision) on a generated page."""
    cfg = ai_config.load(db)
    try:
        if target in ("own", "claude"):
            provider = providers.vision_provider(target, cfg)
        elif target in ("claude_eval", "own_llm"):
            provider = providers.evaluator("claude" if target == "claude_eval" else "own_llm", cfg)
        else:
            raise HTTPException(404, "Unknown provider.")
        health = provider.health_check()
        out = {"ok": health.ok, "provider": target, "model": health.model, "detail": health.detail,
               "duration_ms": health.duration_ms}
        if sample and health.ok and target in ("own", "claude"):
            page = provider.extract_page(_sample_page(), 1, {})
            out["sample_text"] = page.text
            out["sample_duration_ms"] = page.duration_ms
    except ProviderError as exc:
        out = {"ok": False, "provider": target, "detail": str(exc), "kind": exc.kind}
    audit(db, admin, "provider_tested", "settings", None, provider=target, ok=out["ok"])
    db.commit()
    return out
