"""Test setup: a fresh database per test, jobs run inline, Claude replaced by fakes."""

import io
import os
import tempfile

os.environ["EXAM_RUN_JOBS"] = "0"
os.environ.setdefault("EXAM_DATA_DIR", tempfile.mkdtemp(prefix="exam-vision-tests-"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from app import db, providers  # noqa: E402
from app.config import settings  # noqa: E402
from app.providers.base import HealthResult, PageText, ProviderError, SheetReading  # noqa: E402


class FakeReader:
    """Returns SHEET (answers by question number, name, unassigned text); raises when FAIL is set."""

    SHEET: dict = {}
    FAIL: str | None = None
    calls: list = []

    def __init__(self, cfg):
        ok, why = cfg.claude_vision_ready()
        if not ok:
            raise ProviderError(why, "not_configured")
        self.model = "fake-claude-reader"

    def read_sheet(self, images, questions):
        FakeReader.calls.append((len(images), [q["number"] for q in questions]))
        if FakeReader.FAIL:
            raise ProviderError(FakeReader.FAIL, "unavailable", retryable=True)
        answers = {n: {"text": t, "pages": [1], "uncertain": []}
                   for n, t in FakeReader.SHEET.get("answers", {}).items() if n in [q["number"] for q in questions]}
        text = "\n".join(f"{n}. {a['text']}" for n, a in answers.items())
        return SheetReading(model=self.model, pages=[PageText(page_no=i, text=text if i == 1 else "")
                                                     for i in range(1, len(images) + 1)],
                            answers=answers, unassigned_text=FakeReader.SHEET.get("unassigned", ""),
                            name_on_sheet=FakeReader.SHEET.get("name"),
                            usage={"input_tokens": 3000, "output_tokens": 500}, duration_ms=5)

    def health_check(self):
        return HealthResult(True, "claude", self.model, "fake")


class FakeEvaluator:
    """``responder(message, schema) -> dict`` decides the marking; defaults to full marks with evidence."""

    responder = None
    calls: list = []

    def __init__(self, cfg):
        ok, why = cfg.claude_ready()
        if not ok:
            raise ProviderError(why, "not_configured")
        self.model = "claude-opus-5-5"

    def evaluate(self, message, schema):
        FakeEvaluator.calls.append(message)
        if FakeEvaluator.responder:
            return FakeEvaluator.responder(message, schema), {"input_tokens": 1000, "output_tokens": 300}
        return full_marks(message, schema), {"input_tokens": 1000, "output_tokens": 300}

    def health_check(self):
        return HealthResult(True, "claude", self.model, "fake")


def full_marks(message, schema):
    """Full marks on every criterion, quoting the first answer line as evidence."""
    answer = message.split("<student_answer>")[1].split("</student_answer>")[0].strip()
    first = answer.splitlines()[0] if answer else ""
    qid = schema["properties"]["question_id"]["enum"][0]
    ids = schema["properties"]["criteria"]["items"]["properties"]["criterion_id"]["enum"]
    crit_max = {}
    for line in message.splitlines():
        for cid in ids:
            if line.startswith(f"- {cid} ("):
                crit_max[cid] = float(line.split("(")[1].split(" ")[0])
    criteria = [{"criterion_id": c, "criterion_name": c, "maximum_marks": crit_max[c], "awarded_marks": crit_max[c],
                 "evidence": first, "explanation": "meets the criterion"} for c in ids]
    return {"question_id": qid, "maximum_marks": sum(crit_max.values()), "suggested_marks": sum(crit_max.values()),
            "criteria": criteria, "strengths": ["clear"], "missing_concepts": [], "misconceptions": [],
            "student_final_answer": None, "requires_teacher_review": False, "review_reasons": []}


@pytest.fixture(autouse=True)
def fresh(tmp_path):
    settings.data_dir = tmp_path
    settings.run_jobs = False
    db.configure(settings)
    providers.FACTORIES.clear()
    providers.FACTORIES.update({"reader": FakeReader, "evaluator": FakeEvaluator})
    FakeReader.SHEET, FakeReader.FAIL, FakeReader.calls = {}, None, []
    FakeEvaluator.responder, FakeEvaluator.calls = None, []
    from app import main

    main._failures.clear()
    yield


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def page_png(text="answer sheet") -> bytes:
    img = Image.new("RGB", (1240, 1754), "white")
    d = ImageDraw.Draw(img)
    for i, line in enumerate(text.splitlines() or [text]):
        d.text((80, 120 + 40 * i), line, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class World:
    """An admin, a teacher, two students and a 2-question exam with rubrics; Claude configured."""

    def __init__(self, client: TestClient, acknowledge: bool = True):
        self.c = client
        r = client.post("/api/auth/setup", json={"username": "admin", "name": "Admin", "password": "admin-pass-1"})
        self.admin = {"Authorization": "Bearer " + r.json()["token"]}
        client.put("/api/settings/ai", headers=self.admin, json={"changes": {
            "claude_enabled": True, "claude_api_key": "sk-ant-test", "external_processing_acknowledged": acknowledge}})
        self.s1 = client.post("/api/students", headers=self.admin, json={
            "admission_no": "A1", "name": "Arun Kumar", "class_name": "7", "section": "A", "roll_no": "1"}).json()
        self.s2 = client.post("/api/students", headers=self.admin, json={
            "admission_no": "A2", "name": "Divya", "class_name": "7", "section": "A", "roll_no": "2"}).json()
        t = client.post("/api/users", headers=self.admin, json={
            "username": "teacher", "name": "Teacher", "role": "teacher", "password": "teacher-pass-1"}).json()
        self.teacher = self.login("teacher", "teacher-pass-1")
        self.exam = client.post("/api/exams", headers=self.admin, json={
            "name": "Unit Test", "academic_year": "2026-27", "class_name": "7", "section": "A", "subject": "Science"}).json()
        client.put(f"/api/exams/{self.exam['id']}/teachers", headers=self.admin, json={"user_ids": [t["id"]]})
        self.q1 = client.post(f"/api/exams/{self.exam['id']}/questions", headers=self.admin, json={
            "number": "1", "text": "Why do plants need sunlight?", "max_marks": 2}).json()
        self.q2 = client.post(f"/api/exams/{self.exam['id']}/questions", headers=self.admin, json={
            "number": "2", "text": "A car travels 120 km in 2 hours. Find its speed.", "qtype": "math",
            "max_marks": 2, "numeric_answer": "60", "unit": "km/h"}).json()
        for q, crit in ((self.q1, [("C1", "sunlight is the energy source for photosynthesis"),
                                   ("C2", "plants make their own food")]),
                        (self.q2, [("C1", "speed = distance / time"), ("C2", "60 km/h")])):
            r = client.post(f"/api/questions/{q['id']}/rubric", headers=self.teacher, json={
                "criteria": [{"id": i, "name": n, "max_marks": 1} for i, n in crit]})
            assert r.status_code == 200, r.text

    def login(self, username, password):
        r = self.c.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        return {"Authorization": "Bearer " + r.json()["token"]}

    def upload(self, student, headers=None, data=None, auto=True):
        return self.c.post(f"/api/exams/{self.exam['id']}/submissions", headers=headers or self.teacher,
                           data={"student_id": student["id"], "auto_process": str(auto).lower()},
                           files={"files": ("sheet.png", data or page_png(student["name"]), "image/png")})


@pytest.fixture
def world(client):
    return World(client)
