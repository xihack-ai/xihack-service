"""A clarification is persisted per question and only changes Spec after approval."""

from fastapi.testclient import TestClient

from app.main import app
from app.workflow import graph as wf


def _job():
    client = TestClient(app)
    created = client.post("/jobs", json={"title": "后端工程师", "requirement": "会 Go 的后端",
                                        "require_spec_confirmation": True, "candidates": []}).json()
    path = f'/calibrations/{created["job_id"]}'
    confirmed = client.post(path + "/spec/confirm", json={"criteria": [
        {"id": "c1", "type": "MUST_HAVE", "text": "Go 后端经验", "weight": 2}]}).json()
    wf.get_graph().update_state({"configurable": {"thread_id": created["job_id"]}},
                                {"clarification_questions": ["是否需要上线经验？", "是否需要管理团队？"]},
                                as_node="evaluate")
    return client, path, confirmed


def test_not_required_is_recorded_without_spec_change():
    client, path, before = _job()
    result = client.post(path + "/clarification-feedback", json={
        "question_index": 1, "category": "NOT_REQUIRED", "answer": "",
        "base_spec_version": before["spec_version"]})
    assert result.status_code == 200, result.text
    value = result.json()
    assert value["clarification_responses"]["1"]["status"] == "NOT_REQUIRED"
    assert value["spec_version"] == before["spec_version"]
    assert value["criteria"] == before["criteria"]
    assert client.get(path).json()["clarification_responses"]["1"]["status"] == "NOT_REQUIRED"


def test_required_question_waits_for_approval_and_reject_can_retry():
    client, path, before = _job()
    body = {"question_index": 0, "category": "MUST_HAVE", "answer": "独立负责过生产环境上线",
            "base_spec_version": before["spec_version"]}
    pending_response = client.post(path + "/clarification-feedback", json=body)
    assert pending_response.status_code == 200, pending_response.text
    pending = pending_response.json()
    assert pending["workflow"]["waiting_for"] == "human_approval"
    assert pending["clarification_responses"]["0"]["status"] == "PENDING_APPROVAL"
    assert pending["spec_version"] == before["spec_version"]
    assert pending["criteria"] == before["criteria"]
    assert all(item["type"] == "MUST_HAVE" for item in pending["change_proposal"]["add"])
    assert client.post(path + "/clarification-feedback", json=body).status_code == 409
    rejected = client.post(path + "/approval", json={"action": "reject"}).json()
    assert rejected["clarification_responses"]["0"]["status"] == "REJECTED"
    assert rejected["spec_version"] == before["spec_version"]


def test_accepted_clarification_upgrades_spec_and_keeps_response():
    client, path, before = _job()
    pending = client.post(path + "/clarification-feedback", json={
        "question_index": 0, "category": "NICE_TO_HAVE", "answer": "独立负责过生产环境上线",
        "base_spec_version": before["spec_version"]}).json()
    assert pending["change_proposal"]["add"][0]["type"] == "NICE_TO_HAVE"
    after = client.post(path + "/approval", json={
        "action": "accept", "base_spec_version": before["spec_version"]}).json()
    assert after["spec_version"] == before["spec_version"] + 1
    assert after["clarification_responses"]["0"]["status"] == "APPROVED"
    assert after["criteria"][-1]["type"] == "NICE_TO_HAVE"
    assert client.get(path).json()["clarification_responses"]["0"]["status"] == "APPROVED"
    optional = client.post(path + "/clarification-feedback", json={
        "question_index": 1, "category": "NOT_REQUIRED", "answer": "",
        "base_spec_version": after["spec_version"]}).json()
    assert optional["clarification_responses"]["0"]["status"] == "APPROVED"
    assert optional["clarification_responses"]["1"]["status"] == "NOT_REQUIRED"
    assert optional["criteria"] == after["criteria"]


def test_clarification_validation_and_sensitive_block():
    client, path, before = _job()
    body = {"question_index": 0, "category": "NICE_TO_HAVE", "answer": "",
            "base_spec_version": before["spec_version"]}
    assert client.post(path + "/clarification-feedback", json=body).status_code == 422
    assert client.post(path + "/clarification-feedback", json={**body, "answer": "上线经验",
        "base_spec_version": 99}).status_code == 409
    blocked = client.post(path + "/clarification-feedback", json={**body,
        "answer": "只招男性"}).json()
    assert blocked["feedback_status"] == "BLOCKED"
    assert blocked["clarification_responses"]["0"]["status"] == "BLOCKED"
    assert blocked["spec_version"] == before["spec_version"]
