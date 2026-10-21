# backend/app/services/rubric_draft.py
"""AI-drafted rubrics. A draft is a starting point: it is saved only when a teacher (or an
explicit "draft all" request) asks, it is marked as an AI draft, and every mark it produces
still needs teacher approval."""

from __future__ import annotations

from ..ai_config import AIConfig
from ..models import Exam, Question
from ..providers.base import ProviderError
from ..providers.claude_common import call_structured, make_client
from .scoring import increments_ok

SYSTEM = """You write marking rubrics for school examinations.
Given a question, its maximum marks and the mark step, write criteria a teacher can apply
consistently: each criterion is one concept or step that earns marks. Criteria marks must be
multiples of the mark step and add up exactly to the maximum marks. Prefer few, clear criteria
(usually one per mark). Write a short model answer at the level of the class, list valid
alternative answers and common misconceptions. Do not assess spelling or grammar unless the
subject is a language."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["criteria", "reference_answer", "expected_concepts", "alternatives", "misconceptions",
                 "partial_credit_rules"],
    "properties": {
        "criteria": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "max_marks", "description"],
            "properties": {"name": {"type": "string"}, "max_marks": {"type": "number"},
                           "description": {"type": "string"}}}},
        "reference_answer": {"type": "string"},
        "expected_concepts": {"type": "array", "items": {"type": "string"}},
        "alternatives": {"type": "array", "items": {"type": "string"}},
        "misconceptions": {"type": "array", "items": {"type": "string"}},
        "partial_credit_rules": {"type": "string"},
    },
}


def draft(cfg: AIConfig, exam: Exam, q: Question, client=None) -> dict:
    """Returns a rubric dict ready for the rubric form (criteria ids C1..Cn). Raises ProviderError."""
    client = client or make_client(cfg)
    prompt = (f"Subject: {exam.subject}. Class: {exam.class_name} {exam.grade_level}".strip()
              + f"\nQuestion {q.number} ({q.qtype}): {q.text}\nMaximum marks: {q.max_marks:g}; "
              f"mark step: {q.mark_increment:g}"
              + (f"\nExpected final answer: {q.numeric_answer} {q.unit or ''}" if q.numeric_answer else ""))
    data, _usage, _model = call_structured(client, cfg, model=cfg.claude_eval_model, system=SYSTEM, schema=SCHEMA,
                                           content=[{"type": "text", "text": prompt}], max_tokens=4000)
    criteria = [c for c in data.get("criteria") or [] if c.get("name") and float(c.get("max_marks") or 0) > 0]
    if not criteria:
        raise ProviderError("The AI returned no usable criteria.", "bad_response")
    step = q.mark_increment
    for c in criteria:  # snap to the mark step
        c["max_marks"] = max(step, round(float(c["max_marks"]) / step) * step)
    total = sum(c["max_marks"] for c in criteria)
    if abs(total - q.max_marks) > 1e-9:
        # rescale proportionally, then put any remainder on the last criterion
        scaled = [max(step, round(c["max_marks"] * q.max_marks / total / step) * step) for c in criteria]
        diff = q.max_marks - sum(scaled)
        scaled[-1] = scaled[-1] + diff
        if scaled[-1] <= 0 or not all(increments_ok(v, step) for v in scaled):
            raise ProviderError("The AI's criteria could not be fitted to the question's marks.", "bad_response")
        for c, v in zip(criteria, scaled):
            c["max_marks"] = v
    return {
        "criteria": [{"id": f"C{i}", "name": c["name"].strip(), "max_marks": c["max_marks"],
                      "description": (c.get("description") or "").strip()} for i, c in enumerate(criteria, 1)],
        "reference_answer": (data.get("reference_answer") or "").strip(),
        "expected_concepts": [s for s in data.get("expected_concepts") or [] if s],
        "alternatives": [s for s in data.get("alternatives") or [] if s],
        "misconceptions": [s for s in data.get("misconceptions") or [] if s],
        "partial_credit_rules": (data.get("partial_credit_rules") or "").strip(),
        "required_elements": "", "ignore_criteria": "Spelling and grammar are not assessed.", "subject_rules": "",
    }
