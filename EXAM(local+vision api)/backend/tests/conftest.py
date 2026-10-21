"""Test setup: a fresh database per test, jobs run inline, providers replaced by fakes."""

import io
import os
import tempfile

os.environ["EXAM_RUN_JOBS"] = "0"
os.environ.setdefault("EXAM_DATA_DIR", tempfile.mkdtemp(prefix="exam-tests-"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from app import db, providers  # noqa: E402
from app.config import settings  # noqa: E402
from app.providers.base import HealthResult, PageExtraction, ProviderError  # noqa: E402


class FakeVision:
    """Returns PAGES[name][page_no]; raises when FAIL[name] is set."""

    PAGES: dict = {}
    FAIL: dict = {}
    calls: list = []

    def __init__(self, cfg, name="own"):
        self.cfg = cfg
        self.name = name
        if name == "claude":
            ok, why = cfg.claude_vision_ready()
            if not ok:
                raise ProviderError(why, "not_configured")
        self.usage = {"input_tokens": 100, "output_tokens": 50} if name == "claude" else {}

    @property
    def model(self):
        return f"fake-{self.name}"

    def extract_page(self, image, page_no, options=None):
        FakeVision.calls.append((self.name, page_no))
        if FakeVision.FAIL.get(self.name):
            raise ProviderError(FakeVision.FAIL[self.name], "unavailable", retryable=False)
        text = FakeVision.PAGES.get(self.name, {}).get(page_no, "")
        return PageExtraction(page_no=page_no, text=text)

    def health_check(self):
        return HealthResult(True, self.name, self.model, "fake")


class FakeEvaluator:
    """``responder(message, schema) -> dict`` decides the marking; defaults to full marks with evidence."""

    responder = None
    calls: list = []

    def __init__(self, cfg, name="claude"):
        self.name = name
        self.model = f"fake-{name}"
        if name == "claude":
            ok, why = cfg.claude_ready()
            if not ok:
                raise ProviderError(why, "not_configured")

    def evaluate(self, message, schema):
        FakeEvaluator.calls.append(message)
        if FakeEvaluator.responder:
            return FakeEvaluator.responder(message, schema), {"input_tokens": 10, "output_tokens": 5}
        return full_marks(message, schema), {"input_tokens": 10, "output_tokens": 5}

    def health_check(self):
        return HealthResult(True, self.name, self.model, "fake")


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
def fresh(tmp_path, monkeypatch):
    settings.data_dir = tmp_path
    settings.run_jobs = False
    db.configure(settings)
    providers.VISION_FACTORIES.clear()
    providers.EVALUATOR_FACTORIES.clear()
    providers.VISION_FACTORIES.update({"own": lambda cfg: FakeVision(cfg, "own"),
                                       "claude": lambda cfg: FakeVision(cfg, "claude")})
    providers.EVALUATOR_FACTORIES.update({"claude": lambda cfg: FakeEvaluator(cfg, "claude"),
                                          "own_llm": lambda cfg: FakeEvaluator(cfg, "own_llm")})
    FakeVision.PAGES, FakeVision.FAIL, FakeVision.calls = {}, {}, []
    FakeEvaluator.responder, FakeEvaluator.calls = None, []
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
    """An admin, a teacher, two students and a 2-question exam with rubrics."""

    def __init__(self, client: TestClient):
        self.c = client
        r = client.post("/api/auth/setup", json={"username": "admin", "name": "Admin", "password": "admin-pass-1"})
        self.admin = {"Authorization": "Bearer " + r.json()["token"]}
        client.put("/api/settings/ai", headers=self.admin, json={"changes": {
            "own_vision_key": "sk_live_test", "claude_enabled": True, "claude_api_key": "sk-ant-test"}})
        self.s1 = client.post("/api/students", headers=self.admin, json={
            "admission_no": "A1", "name": "Arun", "class_name": "7", "section": "A", "roll_no": "1"}).json()
        self.s2 = client.post("/api/students", headers=self.admin, json={
            "admission_no": "A2", "name": "Divya", "class_name": "7", "section": "A", "roll_no": "2"}).json()
        t = client.post("/api/users", headers=self.admin, json={
            "username": "teacher", "name": "Teacher", "role": "teacher", "password": "teacher-pass-1"}).json()
        self.teacher_id = t["id"]
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
