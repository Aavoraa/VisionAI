# backend/app/services/scoring.py
"""Deterministic validation of AI marking, independent of the model.

The model proposes; this module decides whether the proposal is acceptable:
criterion ids, ranges, increments, totals, evidence, and arithmetic for numerical answers.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from dataclasses import dataclass, field


@dataclass
class Validation:
    ok: bool
    errors: list[str] = field(default_factory=list)  # make the result unusable (retry / teacher)
    review_reasons: list[str] = field(default_factory=list)  # usable, but a teacher must look
    checks: list[dict] = field(default_factory=list)
    result: dict | None = None  # the cleaned result (marks recomputed from the criteria)


def _is_step(value: float, step: float) -> bool:
    if step <= 0:
        return True
    ratio = value / step
    return abs(ratio - round(ratio)) < 1e-6


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text.lower())


def evidence_found(evidence: str, answer: str) -> bool:
    """Is the quoted evidence really in the answer? Exact (normalised) or 70% of its words."""
    ev = _words(evidence)
    if not ev:
        return False
    ans_words = _words(answer)
    joined = " ".join(ans_words)
    if " ".join(ev) in joined:
        return True
    present = sum(1 for w in ev if w in set(ans_words))
    return present / len(ev) >= 0.7


def validate(raw: dict, *, question: dict, rubric: dict, answer_text: str, ocr_status: str) -> Validation:
    errors: list[str] = []
    reasons: list[str] = []
    checks: list[dict] = []
    crit_by_id = {c["id"]: c for c in rubric["criteria"]}
    step = float(question.get("mark_increment") or 0.5)
    max_marks = float(question["max_marks"])

    if not crit_by_id:
        return Validation(False, ["the question has no rubric criteria"], checks=checks)
    if str(raw.get("question_id")) != str(question["number"]):
        errors.append(f"question_id is {raw.get('question_id')!r}, expected {question['number']!r}")

    seen: dict[str, dict] = {}
    for item in raw.get("criteria") or []:
        cid = item.get("criterion_id")
        if cid not in crit_by_id:
            errors.append(f"unknown criterion {cid!r}")
            continue
        if cid in seen:
            errors.append(f"criterion {cid} appears twice")
            continue
        seen[cid] = item
    missing = [cid for cid in crit_by_id if cid not in seen]
    if missing:
        errors.append("criteria not marked: " + ", ".join(missing))

    clean_criteria = []
    total = 0.0
    for cid, crit in crit_by_id.items():
        item = seen.get(cid)
        if item is None:
            continue
        cmax = float(crit["max_marks"])
        try:
            awarded = float(item.get("awarded_marks"))
        except (TypeError, ValueError):
            errors.append(f"{cid}: awarded_marks is not a number")
            continue
        if math.isnan(awarded) or awarded < 0:
            errors.append(f"{cid}: negative marks are not allowed")
            continue
        if awarded > cmax + 1e-9:
            errors.append(f"{cid}: {awarded} exceeds the criterion maximum {cmax}")
            continue
        if not _is_step(awarded, step):
            errors.append(f"{cid}: {awarded} is not in steps of {step}")
            continue
        evidence = (item.get("evidence") or "").strip()
        if awarded > 0:
            if not evidence:
                errors.append(f"{cid}: marks awarded without evidence")
                continue
            if not evidence_found(evidence, answer_text):
                reasons.append(f"{cid}: the quoted evidence was not found in the answer text")
        total += awarded
        clean_criteria.append({
            "criterion_id": cid, "criterion_name": crit["name"], "maximum_marks": cmax,
            "awarded_marks": awarded, "evidence": evidence, "explanation": (item.get("explanation") or "").strip(),
        })

    criteria_max = sum(float(c["max_marks"]) for c in rubric["criteria"])
    if abs(criteria_max - max_marks) > 1e-9:
        reasons.append(f"rubric criteria add up to {criteria_max}, the question maximum is {max_marks}")
    if total > max_marks + 1e-9:
        errors.append(f"total {total} exceeds the question maximum {max_marks}")
    claimed = raw.get("suggested_marks")
    if isinstance(claimed, (int, float)) and abs(float(claimed) - total) > 1e-6:
        checks.append({"check": "total", "ok": False,
                       "detail": f"model said {claimed}, criteria add up to {total}; using {total}"})
    else:
        checks.append({"check": "total", "ok": True, "detail": f"criteria add up to {total}"})

    if ocr_status == "needs_verification":
        reasons.append("the answer text has uncertain or illegible words; verify the transcription")
    if not answer_text.strip() and total > 0:
        errors.append("marks awarded to an empty answer")

    # numerical answers: checked by code, not by the model
    if question.get("numeric_answer"):
        numeric = check_numeric_answer(answer_text, question["numeric_answer"],
                                       float(question.get("numeric_tolerance") or 0.01), question.get("unit"))
        checks.append(numeric)
        full = abs(total - max_marks) < 1e-9
        if numeric["ok"] is False and full:
            reasons.append(f"full marks, but the final answer check failed: {numeric['detail']}")
        if numeric["ok"] is True and total == 0:
            reasons.append(f"zero marks, but the final answer is correct: {numeric['detail']}")
    for slip in arithmetic_slips(answer_text):
        checks.append({"check": "arithmetic", "ok": False, "detail": slip})

    model_reasons = [r for r in raw.get("review_reasons") or [] if r]
    if raw.get("requires_teacher_review"):
        reasons += model_reasons or ["the marker asked for teacher review"]

    if errors:
        return Validation(False, errors, reasons, checks)
    result = {
        "question_id": question["number"],
        "maximum_marks": max_marks,
        "suggested_marks": total,
        "criteria": clean_criteria,
        "strengths": list(raw.get("strengths") or []),
        "missing_concepts": list(raw.get("missing_concepts") or []),
        "misconceptions": list(raw.get("misconceptions") or []),
        "student_final_answer": raw.get("student_final_answer"),
        "ocr_status": ocr_status,
        "evaluation_confidence": None,  # model confidence is not calibrated: never reported as such
        "requires_teacher_review": bool(reasons),
        "review_reasons": list(dict.fromkeys(reasons)),
    }
    return Validation(True, [], result["review_reasons"], checks, result)


# --- numbers -----------------------------------------------------------------

_NUM = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")


def _numbers(text: str) -> list[float]:
    return [float(n.replace(",", "")) for n in _NUM.findall(text)]


def check_numeric_answer(answer: str, expected: str, tolerance: float, unit: str | None) -> dict:
    """The student's final number (last number in the answer) against the expected value."""
    try:
        target = float(expected)
    except ValueError:
        return {"check": "final_answer", "ok": None, "detail": f"expected value {expected!r} is not a number"}
    numbers = _numbers(answer)
    if not numbers:
        return {"check": "final_answer", "ok": False, "detail": "no number found in the answer"}
    final = numbers[-1]
    ok = abs(final - target) <= max(tolerance, abs(target) * 1e-9)
    detail = f"final answer {final:g}, expected {target:g}"
    if ok and unit:
        has_unit = re.sub(r"\s+", "", unit.lower()) in re.sub(r"\s+", "", answer.lower())
        detail += "" if has_unit else f" (unit '{unit}' not written)"
        return {"check": "final_answer", "ok": True, "detail": detail, "unit_present": has_unit}
    return {"check": "final_answer", "ok": ok, "detail": detail}


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_EQUATION = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?(?:\s*[-+*/x×÷]\s*\d+(?:\.\d+)?)+)\s*=\s*(-?\d+(?:\.\d+)?)")


def _safe_eval(expr: str) -> float | None:
    expr = expr.replace("x", "*").replace("×", "*").replace("÷", "/")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        raise ValueError

    try:
        return ev(tree)
    except (ValueError, ZeroDivisionError):
        return None


def arithmetic_slips(text: str) -> list[str]:
    """Written calculations like "120 / 2 = 50" that are wrong."""
    slips = []
    for expr, claimed in _EQUATION.findall(text):
        actual = _safe_eval(expr)
        if actual is not None and abs(actual - float(claimed)) > 1e-6 * max(1.0, abs(actual)):
            slips.append(f"{expr.strip()} = {claimed}, but it equals {actual:g}")
    return slips


def increments_ok(value: float, step: float) -> bool:
    return _is_step(value, step)
