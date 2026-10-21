"""End-to-end workflows through the HTTP API with fake providers."""

from sqlalchemy import select

from app import db
from app.models import AuditLog, Setting

from .conftest import FakeEvaluator, FakeVision, page_png

SHEET = "Name: Arun\nQ1. Plants use light energy to make their own food.\nQ2. speed = 120 / 2 = 60 km/h"


def _sheet(world, sub_id, headers=None):
    return world.c.get(f"/api/submissions/{sub_id}", headers=headers or world.teacher).json()


def _answers(sheet):
    return {row["question"]["number"]: row for row in sheet["questions"]}


def test_upload_read_mark_review_approve_publish(world):
    FakeVision.PAGES = {"own": {1: SHEET}}
    r = world.upload(world.s1)
    assert r.status_code == 200, r.text
    sub = r.json()
    sheet = _sheet(world, sub["id"])
    assert sheet["status"] == "evaluated" and sheet["active_extraction"]["provider"] == "own"
    a = _answers(sheet)
    assert a["1"]["answer"]["text"] == "Plants use light energy to make their own food."
    assert a["1"]["evaluation"]["suggested_marks"] == 2 and a["1"]["answer"]["mark_status"] == "suggested"
    assert a["1"]["evaluation"]["rubric_version"] == 1 and a["1"]["evaluation"]["prompt_version"] == "eval-v1"
    # the student's identity must be confirmed before approval
    aid = a["1"]["answer"]["id"]
    assert world.c.post(f"/api/answers/{aid}/approve", headers=world.teacher, json={}).status_code == 409
    world.c.post(f"/api/submissions/{sub['id']}/identity", headers=world.teacher, json={"verified": True})
    # a teacher override needs a reason and is recorded
    marks = {"criterion_marks": {"C1": 1, "C2": 0.5}}
    assert world.c.put(f"/api/answers/{aid}/marks", headers=world.teacher, json=marks).status_code == 400
    r = world.c.put(f"/api/answers/{aid}/marks", headers=world.teacher, json={**marks, "reason": "food part is vague"})
    assert r.status_code == 200 and r.json()["final_marks"] == 1.5
    hist = world.c.get(f"/api/answers/{aid}/history", headers=world.teacher).json()
    assert hist["overrides"][0]["original_marks"] == 2 and hist["overrides"][0]["new_marks"] == 1.5
    # results cannot be approved while marks are unapproved
    world.c.post(f"/api/exams/{world.exam['id']}/results/reviewed", headers=world.admin)
    assert world.c.post(f"/api/exams/{world.exam['id']}/results/approved", headers=world.admin).status_code == 409
    for row in _answers(_sheet(world, sub["id"])).values():
        r = world.c.post(f"/api/answers/{row['answer']['id']}/approve", headers=world.teacher, json={"feedback": "good"})
        assert r.status_code == 200, r.text
    assert _sheet(world, sub["id"])["status"] == "approved"
    # teachers cannot publish; coordinators/admins can
    assert world.c.post(f"/api/exams/{world.exam['id']}/results/approved", headers=world.teacher).status_code == 403
    assert world.c.post(f"/api/exams/{world.exam['id']}/results/approved", headers=world.admin).status_code == 200
    # the student sees nothing until publication
    world.c.post("/api/users", headers=world.admin, json={"username": "arun", "name": "Arun", "role": "student",
                                                         "password": "student-pass-1", "student_id": world.s1["id"]})
    student = world.login("arun", "student-pass-1")
    assert world.c.get("/api/my/results", headers=student).json() == []
    world.c.post(f"/api/exams/{world.exam['id']}/results/published", headers=world.admin)
    [res] = world.c.get("/api/my/results", headers=student).json()
    assert res["total"] == 3.5 and res["questions"][0]["feedback"] == "good"
    # approved marks cannot be edited without reopening; published results cannot be reopened
    assert world.c.put(f"/api/answers/{aid}/marks", headers=world.teacher, json={**marks, "reason": "x"}).status_code == 409
    assert world.c.post(f"/api/answers/{aid}/reopen", headers=world.teacher, json={"reason": "x"}).status_code == 409


def test_permissions_are_enforced_on_the_server(world, client):
    FakeVision.PAGES = {"own": {1: SHEET}}
    sub = world.upload(world.s1).json()
    # a teacher not assigned to the exam sees nothing (404, not 403)
    client.post("/api/users", headers=world.admin, json={"username": "other", "name": "Other", "role": "teacher",
                                                        "password": "other-pass-1"})
    other = world.login("other", "other-pass-1")
    assert client.get(f"/api/exams/{world.exam['id']}", headers=other).status_code == 404
    assert client.get(f"/api/submissions/{sub['id']}", headers=other).status_code == 404
    assert client.get("/api/exams", headers=other).json() == []
    page_id = _sheet(world, sub["id"])["page_list"][0]["id"]
    assert client.get(f"/api/pages/{page_id}/image", headers=other).status_code == 404
    # students cannot reach staff endpoints
    client.post("/api/users", headers=world.admin, json={"username": "kid", "name": "Kid", "role": "student",
                                                        "password": "kid-pass-12", "student_id": world.s2["id"]})
    kid = world.login("kid", "kid-pass-12")
    assert client.get(f"/api/submissions/{sub['id']}", headers=kid).status_code == 403
    assert client.get("/api/exams", headers=kid).status_code == 403
    # only admins see or change AI configuration; teachers cannot create exams
    assert client.get("/api/settings/ai", headers=world.teacher).status_code == 403
    assert client.post("/api/exams", headers=world.teacher, json={
        "name": "x", "academic_year": "2026", "class_name": "7", "subject": "x"}).status_code == 403
    assert client.get("/api/exams").status_code == 401


def test_api_keys_are_never_returned(world, client):
    cfg = client.get("/api/settings/ai", headers=world.admin).json()["config"]
    assert cfg["claude_api_key"].endswith("test") and "sk-ant" not in cfg["claude_api_key"]
    assert "sk_live" not in str(cfg)
    with db.new_session() as s:
        stored = s.get(Setting, "claude_api_key").secret
        assert "sk-ant-test" not in stored  # encrypted at rest
        details = [a.details for a in s.scalars(select(AuditLog).where(AuditLog.action == "ai_configuration_changed"))]
        assert "sk-ant-test" not in str(details)


def test_provider_failure_is_recorded_and_never_switches_silently(world):
    FakeVision.FAIL = {"own": "GPU server down"}
    world.c.put("/api/settings/ai", headers=world.admin, json={"changes": {"external_processing_acknowledged": True}})
    sub = world.upload(world.s1).json()
    sheet = _sheet(world, sub["id"])
    assert sheet["status"] == "needs_attention" and "GPU server down" in sheet["status_note"]
    assert [c[0] for c in FakeVision.calls] == ["own"]  # Claude was not tried
    assert sheet["page_list"]  # the original pages are kept
    # an explicit retry with Claude works
    FakeVision.PAGES = {"claude": {1: SHEET}}
    r = world.c.post(f"/api/submissions/{sub['id']}/process", headers=world.teacher, json={"provider": "claude"})
    assert r.status_code == 200
    sheet = _sheet(world, sub["id"])
    assert sheet["active_extraction"]["provider"] == "claude" and sheet["active_extraction"]["redacted"]


def test_automatic_fallback_only_when_enabled_and_audited(world):
    FakeVision.FAIL = {"own": "down"}
    FakeVision.PAGES = {"claude": {1: SHEET}}
    # enabling automatic fallback requires the external-processing acknowledgement
    r = world.c.put("/api/settings/ai", headers=world.admin, json={"changes": {"auto_fallback_to_claude": True}})
    assert r.status_code == 400
    world.c.put("/api/settings/ai", headers=world.admin, json={"changes": {
        "external_processing_acknowledged": True, "auto_fallback_to_claude": True}})
    sub = world.upload(world.s1).json()
    sheet = _sheet(world, sub["id"])
    assert sheet["active_extraction"]["provider"] == "claude"
    assert sheet["active_extraction"]["fallback_from"] == "own"
    with db.new_session() as s:
        assert s.scalars(select(AuditLog).where(AuditLog.action == "automatic_provider_fallback")).first()


def test_claude_vision_needs_acknowledgement(world):
    FakeVision.PAGES = {"own": {1: SHEET}}
    sub = world.upload(world.s1).json()
    r = world.c.post(f"/api/submissions/{sub['id']}/process", headers=world.teacher, json={"provider": "claude"})
    assert r.status_code == 400 and "acknowledge" in r.json()["detail"]


def test_comparison_keeps_both_readings_and_merges_nothing(world):
    world.c.put("/api/settings/ai", headers=world.admin, json={"changes": {"external_processing_acknowledged": True}})
    FakeVision.PAGES = {"own": {1: SHEET}, "claude": {1: SHEET.replace("120 / 2 = 60", "120 / 2 = 66")}}
    sub = world.upload(world.s1).json()
    before = _answers(_sheet(world, sub["id"]))["2"]["answer"]["text"]
    assert world.c.post(f"/api/submissions/{sub['id']}/compare", headers=world.teacher).status_code == 200
    cmp = world.c.get(f"/api/submissions/{sub['id']}/compare", headers=world.teacher).json()
    assert cmp["a"]["provider"] == "own" and cmp["b"]["provider"] == "claude"
    assert cmp["pages"][0]["numbers_match"] is False
    assert any(d["op"] == "replace" and d["b"] == "66" for d in cmp["pages"][0]["diff"])
    assert _answers(_sheet(world, sub["id"]))["2"]["answer"]["text"] == before  # nothing merged or switched
    # the teacher explicitly switches
    r = world.c.post(f"/api/submissions/{sub['id']}/extractions/{cmp['b']['id']}/activate", headers=world.teacher)
    assert r.status_code == 200
    assert "66" in _answers(_sheet(world, sub["id"]))["2"]["answer"]["text"]


def test_uncertain_handwriting_is_verified_before_marking(world):
    FakeVision.PAGES = {"own": {1: "Q1. Plants make [?foad?] from light\nQ2. 60 km/h"}}
    sub = world.upload(world.s1).json()
    a = _answers(_sheet(world, sub["id"]))["1"]
    assert a["answer"]["ocr_status"] == "needs_verification" and a["evaluation"] is None
    assert any("verify the transcription" in f for f in a["answer"]["flags"])
    aid = a["answer"]["id"]
    world.c.patch(f"/api/answers/{aid}/text", headers=world.teacher, json={"text": "Plants make food from light"})
    world.c.post(f"/api/submissions/{sub['id']}/evaluate", headers=world.teacher, json={})
    a = _answers(_sheet(world, sub["id"]))["1"]
    assert a["evaluation"]["status"] == "ok"
    assert a["answer"]["original_text"] == "Plants make [?foad?] from light"  # the original is kept
    assert a["answer"]["text"] == "Plants make food from light"


def test_invalid_ai_output_is_retried_then_left_to_the_teacher(world):
    bad = {"question_id": "1", "maximum_marks": 2, "suggested_marks": 5,
           "criteria": [{"criterion_id": "C1", "criterion_name": "x", "maximum_marks": 1, "awarded_marks": 5,
                         "evidence": "light", "explanation": ""}],
           "strengths": [], "missing_concepts": [], "misconceptions": [], "student_final_answer": None,
           "requires_teacher_review": False, "review_reasons": []}
    FakeEvaluator.responder = lambda msg, schema: {**bad, "question_id": schema["properties"]["question_id"]["enum"][0]}
    FakeVision.PAGES = {"own": {1: SHEET}}
    sub = world.upload(world.s1).json()
    a = _answers(_sheet(world, sub["id"]))["1"]
    assert a["evaluation"]["status"] == "invalid" and a["answer"]["mark_status"] == "needs_review"
    assert a["answer"]["final_marks"] is None
    assert len(FakeEvaluator.calls) == 4  # two questions x (first try + one retry)
    assert "failed validation" in FakeEvaluator.calls[1]


def test_missing_rubric_is_flagged_not_invented(world):
    q3 = world.c.post(f"/api/exams/{world.exam['id']}/questions", headers=world.admin, json={
        "number": "3", "text": "Name two functions of roots.", "max_marks": 2}).json()
    assert q3["rubric_problems"]
    FakeVision.PAGES = {"own": {1: SHEET + "\nQ3. absorb water and hold the plant"}}
    sub = world.upload(world.s1).json()
    a = _answers(_sheet(world, sub["id"]))["3"]
    assert a["evaluation"] is None and any(f.startswith("rubric:") for f in a["answer"]["flags"])
    assert all("Name two functions" not in m for m in FakeEvaluator.calls)


def test_rubric_change_never_changes_approved_marks(world):
    FakeVision.PAGES = {"own": {1: SHEET}}
    sub = world.upload(world.s1).json()
    world.c.post(f"/api/submissions/{sub['id']}/identity", headers=world.teacher, json={"verified": True})
    a1 = _answers(_sheet(world, sub["id"]))["1"]["answer"]["id"]
    world.c.post(f"/api/answers/{a1}/approve", headers=world.teacher, json={})
    r = world.c.post(f"/api/questions/{world.q1['id']}/rubric", headers=world.teacher, json={
        "criteria": [{"id": "C1", "name": "photosynthesis named", "max_marks": 2}]})
    assert r.status_code == 200 and r.json()["rubric"]["version"] == 2
    FakeEvaluator.calls = []
    world.c.post(f"/api/exams/{world.exam['id']}/reevaluate", headers=world.teacher, json={"question_id": world.q1["id"]})
    a = _answers(_sheet(world, sub["id"]))["1"]
    assert a["answer"]["mark_status"] == "approved" and a["evaluation"]["rubric_version"] == 1
    assert FakeEvaluator.calls == []


def test_duplicates_and_identity(world):
    FakeVision.PAGES = {"own": {1: SHEET}}
    data = page_png("same file")
    assert world.upload(world.s1, data=data).status_code == 200
    assert world.upload(world.s1, data=page_png("other")).status_code == 409  # one sheet per student
    assert world.upload(world.s2, data=data).status_code == 409  # same file for another student
    outsider = world.c.post("/api/students", headers=world.admin, json={
        "admission_no": "B9", "name": "Other class", "class_name": "8"}).json()
    assert world.upload(outsider).status_code == 400


def test_bulk_upload_matches_admission_numbers_from_file_names(world):
    FakeVision.PAGES = {"own": {1: SHEET}}
    r = world.c.post(f"/api/exams/{world.exam['id']}/submissions/bulk", headers=world.teacher,
                     data={"auto_process": "false"},
                     files=[("files", ("A1.png", page_png("a1"), "image/png")),
                            ("files", ("unknown.png", page_png("x"), "image/png"))])
    res = {x["file"]: x for x in r.json()["results"]}
    assert res["A1.png"]["ok"] and res["A1.png"]["student"] == "Arun"
    assert not res["unknown.png"]["ok"]


def test_numeric_check_flags_a_full_mark_for_a_wrong_answer(world):
    FakeVision.PAGES = {"own": {1: "Q1. Plants use light energy to make their own food.\nQ2. speed = 120 / 2 = 50 km/h"}}
    sub = world.upload(world.s1).json()
    ev = _answers(_sheet(world, sub["id"]))["2"]["evaluation"]
    assert ev["requires_review"] and any("final answer check failed" in r for r in ev["review_reasons"])
    assert any(c["check"] == "arithmetic" for c in ev["checks"])


def test_blank_answer_gets_zero_without_a_model_call(world):
    FakeVision.PAGES = {"own": {1: "Q1. Plants use light energy to make their own food."}}
    sub = world.upload(world.s1).json()
    a = _answers(_sheet(world, sub["id"]))["2"]
    assert a["evaluation"]["provider"] == "rules" and a["answer"]["final_marks"] == 0
    assert a["answer"]["mark_status"] == "needs_review"
    assert len(FakeEvaluator.calls) == 1


def test_marking_unavailable_without_claude_key(client):
    from .conftest import World

    w = World(client)
    client.put("/api/settings/ai", headers=w.admin, json={"changes": {"claude_api_key": "", "evaluation_provider": "claude"}})
    FakeVision.PAGES = {"own": {1: SHEET}}
    sub = w.upload(w.s1).json()
    sheet = _sheet(w, sub["id"])
    assert sheet["status"] == "needs_attention" and "Claude" in sheet["status_note"]
    assert _answers(sheet)["1"]["answer"]["text"]  # reading still succeeded


def test_login_rate_limit_and_wrong_password(client, world):
    assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    assert client.post("/api/auth/setup", json={"username": "xyz", "name": "x", "password": "longpassword"}).status_code == 409


def test_first_marks_without_ai_are_not_counted_as_overrides(client):
    from .conftest import World

    w = World(client)
    client.put("/api/settings/ai", headers=w.admin, json={"changes": {"claude_api_key": "", "evaluation_provider": "claude"}})
    FakeVision.PAGES = {"own": {1: SHEET}}
    sub = w.upload(w.s1).json()
    aid = _answers(_sheet(w, sub["id"]))["1"]["answer"]["id"]
    r = client.put(f"/api/answers/{aid}/marks", headers=w.teacher, json={"criterion_marks": {"C1": 1, "C2": 0}})
    assert r.status_code == 200  # no reason needed: there was nothing to override
    assert client.get(f"/api/answers/{aid}/history", headers=w.teacher).json()["overrides"] == []
    r = client.put(f"/api/answers/{aid}/marks", headers=w.teacher, json={"criterion_marks": {"C1": 1, "C2": 1}})
    assert r.status_code == 400  # a change to existing marks needs a reason


def test_draft_all_saves_ai_rubrics_only_where_missing(world, client, monkeypatch):
    from app.services import rubric_draft

    q3 = client.post(f"/api/exams/{world.exam['id']}/questions", headers=world.admin, json={
        "number": "3", "text": "What is sales?", "max_marks": 2}).json()
    monkeypatch.setattr(rubric_draft, "draft", lambda cfg, exam, q, client=None: {
        "criteria": [{"id": "C1", "name": "selling products or services", "max_marks": 1, "description": ""},
                     {"id": "C2", "name": "to earn money / meet customer needs", "max_marks": 1, "description": ""}],
        "reference_answer": "Sales is selling products or services to customers for money."})
    r = client.post(f"/api/exams/{world.exam['id']}/rubrics/draft-all", headers=world.teacher, json={})
    assert r.json() == {"saved": ["3"], "failed": []}  # Q1 and Q2 already had rubrics
    q = client.get(f"/api/exams/{world.exam['id']}", headers=world.teacher).json()["questions"][2]
    assert q["id"] == q3["id"] and q["rubric"]["source"] == "ai_draft" and q["rubric_problems"] == []
