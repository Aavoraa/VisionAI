# backend/app/routers/settings.py
"""Settings -> AI Configuration (administrators). The Claude key goes in, never comes back out."""

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
            "options": {"models": [{"id": m, "label": label} for m, label in ai_config.MODELS],
                        "efforts": list(ai_config.EFFORTS)}}


@router.get("/ai/status")
def ai_status(_: StaffUser, db: DB) -> dict:
    """What staff screens need to know (no keys)."""
    cfg = ai_config.load(db)
    reading_ok, reading_why = cfg.claude_vision_ready()
    marking_ok, marking_why = cfg.claude_ready()
    return {"reading_available": reading_ok, "reading_reason": reading_why,
            "marking_available": marking_ok, "marking_reason": marking_why,
            "vision_model": cfg.claude_vision_model, "eval_model": cfg.claude_eval_model}


class AIUpdate(BaseModel):
    changes: dict[str, Any]


@router.put("/ai")
def put_ai(body: AIUpdate, admin: AdminUser, db: DB) -> dict:
    changes = dict(body.changes)
    for key in ("claude_vision_model", "claude_eval_model"):
        if key in changes and not str(changes[key] or "").startswith("claude-"):
            raise HTTPException(400, "Choose a Claude model.")
    if "claude_effort" in changes and changes["claude_effort"] not in ai_config.EFFORTS:
        raise HTTPException(400, "Unknown effort level.")
    if "redact_top_percent" in changes:
        try:
            if not 0 <= float(changes["redact_top_percent"] or 0) <= 50:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(400, "Blanking must be between 0 and 50 %.") from None
    if changes.get("external_processing_acknowledged") is True:
        changes["external_processing_acknowledged_by"] = {"user": admin.name, "at": now().isoformat() + "Z"}
    elif changes.get("external_processing_acknowledged") is False:
        changes["external_processing_acknowledged_by"] = None
    changed = ai_config.save(db, changes, admin.id)
    audit(db, admin, "ai_configuration_changed", "settings", None, keys=changed)  # never the values
    db.commit()
    return get_ai(admin, db)


def _sample_page() -> bytes:
    img = Image.new("RGB", (900, 300), "white")
    d = ImageDraw.Draw(img)
    d.text((40, 40), "Name: Sample", fill="black")
    d.text((40, 110), "1. Plants use sunlight to make food.", fill="black")
    d.text((40, 180), "2. 120 / 2 = 60 km/h", fill="black")
    return to_png(img.resize((1800, 600)))


@router.post("/ai/test/{target}")
def test_claude(target: str, admin: AdminUser, db: DB, sample: bool = False) -> dict:
    """target: reading | marking. With sample=true, reading also reads a generated sample page."""
    cfg = ai_config.load(db)
    try:
        if target == "reading":
            provider = providers.reader(cfg)
        elif target == "marking":
            provider = providers.evaluator(cfg)
        else:
            raise HTTPException(404, "Unknown target.")
        health = provider.health_check()
        out = {"ok": health.ok, "target": target, "model": health.model, "detail": health.detail,
               "duration_ms": health.duration_ms}
        if sample and health.ok and target == "reading":
            r = provider.read_sheet([_sample_page()], [{"number": "1", "text": "Why do plants need sunlight?"},
                                                       {"number": "2", "text": "Find the speed."}])
            out["sample_text"] = "\n".join(f"Q{n}: {a['text']}" for n, a in r.answers.items())
            out["sample_name"] = r.name_on_sheet
            out["sample_duration_ms"] = r.duration_ms
    except ProviderError as exc:
        out = {"ok": False, "target": target, "detail": str(exc), "kind": exc.kind}
    audit(db, admin, "claude_tested", "settings", None, target=target, ok=out["ok"])
    db.commit()
    return out
