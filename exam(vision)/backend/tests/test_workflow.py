"""End-to-end workflows through the HTTP API with a fake Claude."""

from sqlalchemy import select

from app import db
from app.models import AuditLog, Setting

from .conftest import FakeEvaluator, FakeReader, World, page_png

GOOD = {"name": "Arun Kumar", "answers": {"1": "Plants use light energy to make their own food.",
                                          "2": "speed = 120 / 2 = 60 km/h"}}


def _sheet(world, sub_id, headers=None):
    return world.c.get(f"/api/submissions/{sub_id}", headers=headers or world.teacher).json()


def _answers(sheet):
    return {row["question"]["number"]: row for row in sheet["questions"]}


def test_upload_read_mark_review_approve_publish(world):
    FakeReader.SHEET = GOOD
    r = world.upload(world.s1)
    assert r.status_code == 200, r.text
    sub = r.json()
    sheet = _sheet(world, sub["id"])
    assert sheet["status"] == "evaluated" and sheet["active_extraction"]["model"] == "fake-claude-reader"
    assert FakeReader.calls == [(1, ["1", "2"])]  # the whole sheet in one call, with the question list
    a = _answers(sheet)
    assert a["1"]["answer"]["text"] == "Plants use light energy to make their own food."
    assert a["1"]["evaluation"]["suggested_marks"] == 2 and a["1"]["answer"]["mark_status"] == "suggested"
    assert sheet["name_check"]["status"] == "match"
    assert sheet["cost"]["input_tokens"] == 3000 + 2 * 1000 and sheet["cost"]["cost_usd"] > 0
    aid = a["1"]["answer"]["id"]
    assert world.c.post(f"/api/answers/{aid}/approve", headers=world.teacher, json={}).status_code == 409  # identity
    world.c.post(f"/api/submissions/{sub['id']}/identity", headers=world.teacher, json={"verified": True})
    marks = {"criterion_marks": {"C1": 1, "C2": 0.5}}
    assert world.c.put(f"/api/answers/{aid}/marks", headers=world.teacher, json=marks).status_code == 400
    r = world.c.put(f"/api/answers/{aid}/marks", headers=world.teacher, json={**marks, "reason": "food part is vague"})
    assert r.status_code == 200 and r.json()["final_marks"] == 1.5
    world.c.post(f"/api/exams/{world.exam['id']}/results/reviewed", headers=world.admin)
    assert world.c.post(f"/api/exams/{world.exam['id']}/results/approved", headers=world.admin).status_code == 409
    for row in _answers(_sheet(world, sub["id"])).values():
        assert world.c.post(f"/api/answers/{row['answer']['id']}/approve", headers=world.teacher,
                            json={"feedback": "good"}).status_code == 200
    assert world.c.post(f"/api/exams/{world.exam['id']}/results/approved", headers=world.teacher).status_code == 403
    assert world.c.post(f"/api/exams/{world.exam['id']}/results/approved", headers=world.admin).status_code == 200
    world.c.post("/api/users", headers=world.admin, json={"username": "arun", "name": "Arun", "role": "student",
                                                         "password": "student-pass-1", "student_id": world.s1["id"]})
    student = world.login("arun", "student-pass-1")
    assert world.c.get("/api/my/results", headers=student).json() == []
    world.c.post(f"/api/exams/{world.exam['id']}/results/published", headers=world.admin)
    [res] = world.c.get("/api/my/results", headers=student).json()
    assert res["total"] == 3.5 and res["questions"][0]["feedback"] == "good"


def test_name_on_sheet_that_differs_is_flagged_but_never_changes_the_student(world):
    FakeReader.SHEET = {**GOOD, "name": "Rohini N"}
    sub = world.upload(world.s1).json()
    sheet = _sheet(world, sub["id"])
    assert sheet["name_check"]["status"] == "differs" and sheet["student"]["name"] == "Arun Kumar"


def test_unassigned_writing_is_kept_and_shown(world):
    FakeReader.SHEET = {**GOOD, "unassigned": "rough work: 12 x 4 = 48"}
    sub = world.upload(world.s1).json()
    assert _sheet(world, sub["id"])["active_extraction"]["unassigned_text"] == "rough work: 12 x 4 = 48"


def test_permissions_are_enforced_on_the_server(world, client):
    FakeReader.SHEET = GOOD
    sub = world.upload(world.s1).json()
    client.post("/api/users", headers=world.admin, json={"username": "other", "name": "Other", "role": "teacher",
                                                        "password": "other-pass-1"})
    other = world.login("other", "other-pass-1")
    assert client.get(f"/api/exams/{world.exam['id']}", headers=other).status_code == 404
    assert client.get(f"/api/submissions/{sub['id']}", headers=other).status_code == 404
    page_id = _sheet(world, sub["id"])["page_list"][0]["id"]
    assert client.get(f"/api/pages/{page_id}/image", headers=other).status_code == 404
    client.post("/api/users", headers=world.admin, json={"username": "kid", "name": "Kid", "role": "student",
                                                        "password": "kid-pass-12", "student_id": world.s2["id"]})
    kid = world.login("kid", "kid-pass-12")
    assert client.get(f"/api/submissions/{sub['id']}", headers=kid).status_code == 403
    assert client.get("/api/settings/ai", headers=world.teacher).status_code == 403
    assert client.get("/api/exams").status_code == 401


def test_the_claude_key_is_never_returned(world, client):
    cfg = client.get("/api/settings/ai", headers=world.admin).json()["config"]
    assert "sk-ant" not in str(cfg) and cfg["claude_api_key"].endswith("test")
    with db.new_session() as s:
        assert "sk-ant-test" not in s.get(Setting, "claude_api_key").secret
        details = [a.details for a in s.scalars(select(AuditLog).where(AuditLog.action == "ai_configuration_changed"))]
        assert "sk-ant-test" not in str(details)


def test_reading_failure_keeps_the_sheet_and_can_be_retried(world):
    FakeReader.FAIL = "Claude rate limit reached. Retry shortly."
    sub = world.upload(world.s1).json()
    sheet = _sheet(world, sub["id"])
    assert sheet["status"] == "needs_attention" and "rate limit" in sheet["status_note"] and sheet["page_list"]
    FakeReader.FAIL, FakeReader.SHEET = None, GOOD
    assert world.c.post(f"/api/submissions/{sub['id']}/process", headers=world.teacher).status_code == 200
    assert _sheet(world, sub["id"])["status"] == "evaluated"


def test_without_acknowledgement_sheets_are_stored_but_not_read(client):
    w = World(client, acknowledge=False)
    r = w.upload(w.s1)
    assert r.status_code == 200 and "acknowledge" in r.json()["note"] and r.json()["job"] is None
    assert FakeReader.calls == []
    assert client.post(f"/api/submissions/{r.json()['id']}/process", headers=w.teacher).status_code == 400


def test_uncertain_words_are_verified_before_marking(world):
    FakeReader.SHEET = {"answers": {"1": "Plants make [?foad?] from light", "2": "60 km/h"}}
    sub = world.upload(world.s1).json()
    a = _answers(_sheet(world, sub["id"]))["1"]
    assert a["answer"]["ocr_status"] == "needs_verification" and a["evaluation"] is None
    world.c.patch(f"/api/answers/{a['answer']['id']}/text", headers=world.teacher, json={"text": "Plants make food from light"})
    world.c.post(f"/api/submissions/{sub['id']}/evaluate", headers=world.teacher, json={})
    a = _answers(_sheet(world, sub["id"]))["1"]
    assert a["evaluation"]["status"] == "ok" and a["answer"]["original_text"] == "Plants make [?foad?] from light"


def test_invalid_ai_output_is_retried_then_left_to_the_teacher(world):
    bad = {"question_id": "1", "maximum_marks": 2, "suggested_marks": 5,
           "criteria": [{"criterion_id": "C1", "criterion_name": "x", "maximum_marks": 1, "awarded_marks": 5,
                         "evidence": "light", "explanation": ""}],
           "strengths": [], "missing_concepts": [], "misconceptions": [], "student_final_answer": None,
           "requires_teacher_review": False, "review_reasons": []}
    FakeEvaluator.responder = lambda msg, schema: {**bad, "question_id": schema["properties"]["question_id"]["enum"][0]}
    FakeReader.SHEET = GOOD
    sub = world.upload(world.s1).json()
    a = _answers(_sheet(world, sub["id"]))["1"]
    assert a["evaluation"]["status"] == "invalid" and a["answer"]["final_marks"] is None
    assert len(FakeEvaluator.calls) == 4 and "failed validation" in FakeEvaluator.calls[1]


def test_missing_rubric_is_flagged_not_invented(world):
    world.c.post(f"/api/exams/{world.exam['id']}/questions", headers=world.admin, json={
        "number": "3", "text": "Name two functions of roots.", "max_marks": 2})
    FakeReader.SHEET = {"answers": {**GOOD["answers"], "3": "absorb water and hold the plant"}}
    sub = world.upload(world.s1).json()
    a = _answers(_sheet(world, sub["id"]))["3"]
    assert a["evaluation"] is None and any(f.startswith("rubric:") for f in a["answer"]["flags"])


def test_rubric_change_never_changes_approved_marks(world):
    FakeReader.SHEET = GOOD
    sub = world.upload(world.s1).json()
    world.c.post(f"/api/submissions/{sub['id']}/identity", headers=world.teacher, json={"verified": True})
    a1 = _answers(_sheet(world, sub["id"]))["1"]["answer"]["id"]
    world.c.post(f"/api/answers/{a1}/approve", headers=world.teacher, json={})
    world.c.post(f"/api/questions/{world.q1['id']}/rubric", headers=world.teacher, json={
        "criteria": [{"id": "C1", "name": "photosynthesis named", "max_marks": 2}]})
    FakeEvaluator.calls = []
    world.c.post(f"/api/exams/{world.exam['id']}/reevaluate", headers=world.teacher, json={"question_id": world.q1["id"]})
    a = _answers(_sheet(world, sub["id"]))["1"]
    assert a["answer"]["mark_status"] == "approved" and a["evaluation"]["rubric_version"] == 1 and FakeEvaluator.calls == []


def test_duplicates_and_bulk_upload(world):
    FakeReader.SHEET = GOOD
    data = page_png("same file")
    assert world.upload(world.s1, data=data).status_code == 200
    assert world.upload(world.s1, data=page_png("other")).status_code == 409
    assert world.upload(world.s2, data=data).status_code == 409
    r = world.c.post(f"/api/exams/{world.exam['id']}/submissions/bulk", headers=world.teacher,
                     data={"auto_process": "false"},
                     files=[("files", ("A2.png", page_png("a2"), "image/png")),
                            ("files", ("unknown.png", page_png("x"), "image/png"))])
    res = {x["file"]: x for x in r.json()["results"]}
    assert res["A2.png"]["ok"] and not res["unknown.png"]["ok"]


def test_numeric_check_and_blank_answer(world):
    FakeReader.SHEET = {"answers": {"1": "", "2": "speed = 120 / 2 = 50 km/h"}}
    sub = world.upload(world.s1).json()
    a = _answers(_sheet(world, sub["id"]))
    assert a["1"]["evaluation"]["provider"] == "rules" and a["1"]["answer"]["final_marks"] == 0
    ev = a["2"]["evaluation"]
    assert any("final answer check failed" in r for r in ev["review_reasons"])
    assert len(FakeEvaluator.calls) == 1


def test_first_marks_are_not_overrides(client):
    w = World(client)
    client.put("/api/settings/ai", headers=w.admin, json={"changes": {"claude_api_key": ""}})
    FakeReader.SHEET = GOOD
    sub = w.upload(w.s1).json()
    assert _sheet(w, sub["id"])["status"] == "uploaded"  # no key: stored, not read
    client.put("/api/settings/ai", headers=w.admin, json={"changes": {"claude_api_key": "sk-ant-test"}})
    FakeEvaluator.responder = lambda m, s: (_ for _ in ()).throw(__import__("app").providers.base.ProviderError("down"))
    client.post(f"/api/submissions/{sub['id']}/process", headers=w.teacher)
    aid = _answers(_sheet(w, sub["id"]))["1"]["answer"]["id"]
    r = client.put(f"/api/answers/{aid}/marks", headers=w.teacher, json={"criterion_marks": {"C1": 1, "C2": 0}})
    assert r.status_code == 200
    assert client.get(f"/api/answers/{aid}/history", headers=w.teacher).json()["overrides"] == []


def test_settings_validation_and_tests(world, client):
    assert client.put("/api/settings/ai", headers=world.admin, json={"changes": {"claude_eval_model": "gpt-x"}}).status_code == 400
    assert client.put("/api/settings/ai", headers=world.admin, json={"changes": {"claude_vision_model": "claude-sonnet-5-5"}}).status_code == 200
    r = client.post("/api/settings/ai/test/reading?sample=true", headers=world.admin).json()
    assert r["ok"] and "sample_text" in r
    assert client.post("/api/settings/ai/test/marking", headers=world.admin).json()["ok"]


def test_draft_all_saves_ai_rubrics_only_where_missing(world, client, monkeypatch):
    from app.services import rubric_draft

    client.post(f"/api/exams/{world.exam['id']}/questions", headers=world.admin, json={
        "number": "3", "text": "What is sales?", "max_marks": 2})
    monkeypatch.setattr(rubric_draft, "draft", lambda cfg, exam, q, client=None: {
        "criteria": [{"id": "C1", "name": "selling products or services", "max_marks": 1, "description": ""},
                     {"id": "C2", "name": "to earn money", "max_marks": 1, "description": ""}]})
    r = client.post(f"/api/exams/{world.exam['id']}/rubrics/draft-all", headers=world.teacher, json={})
    assert r.json() == {"saved": ["3"], "failed": []}
