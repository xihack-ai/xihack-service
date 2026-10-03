"""Manager edits are staged, reviewed, and versioned rather than applied in place."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.usefixtures("labelled_calibration_evidence")


def create_job(client: TestClient) -> dict:
    response = client.post("/jobs", json={
        "title": "后端工程师",
        "requirement": "要求 Python 后端经验和项目交付能力",
        "candidates": [{"name": "样本甲", "resume_text": "负责 Python 后端服务开发。"}],
        "require_spec_confirmation": True,
    })
    assert response.status_code == 200
    job = response.json()
    response = client.post(f"/calibrations/{job['job_id']}/spec/confirm",
                           json={"criteria": job["criteria"]})
    assert response.status_code == 200
    return response.json()


def test_manual_revision_reject_then_approve():
    client = TestClient(app)
    job = create_job(client)
    job_id = job["job_id"]
    original = job["criteria"]
    edited = [{**original[0], "type": "NICE_TO_HAVE"},
              {"id": "c9", "type": "MUST_HAVE", "text": "独立负责上线", "weight": 2}]
    body = {"criteria": edited, "base_spec_version": 1,
            "requirement": "招聘能独立负责上线的 Python 后端工程师"}
    staged = client.post(f"/calibrations/{job_id}/spec/manual-proposal", json=body)
    assert staged.status_code == 200, staged.text
    assert staged.json()["workflow"]["waiting_for"] == "human_approval"
    assert staged.json()["criteria"] == original
    assert staged.json()["requirement"] == job["requirement"]
    rejected = client.post(f"/calibrations/{job_id}/approval", json={"action": "reject"})
    assert rejected.status_code == 200
    assert rejected.json()["spec_version"] == 1
    assert rejected.json()["requirement"] == job["requirement"]

    staged = client.post(f"/calibrations/{job_id}/spec/manual-proposal", json=body)
    assert staged.status_code == 200, staged.text
    approved = client.post(f"/calibrations/{job_id}/approval", json={"action": "accept"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["spec_version"] == 2
    assert approved.json()["criteria"] == edited
    assert approved.json()["requirement"] == body["requirement"]
    assert approved.json()["clarified_requirement"] == body["requirement"]
    assert approved.json()["clarification_questions"] == []
    assert client.post(f"/calibrations/{job_id}/spec/manual-proposal", json=body).status_code == 409


def test_manual_revision_blocks_sensitive_edit():
    client = TestClient(app)
    job = create_job(client)
    response = client.post(f"/calibrations/{job['job_id']}/spec/manual-proposal", json={
        "criteria": [{**job["criteria"][0], "text": "只招男性后端"}],
        "base_spec_version": 1,
    })
    assert response.status_code == 422
    assert client.get(f"/calibrations/{job['job_id']}").json()["spec_version"] == 1


def test_reanalyze_unapproved_intent_preserves_old_draft_and_candidates():
    client = TestClient(app)
    payload = {"title": "后端工程师", "requirement": "要求 Python 后端经验",
               "candidates": [{"name": "样本甲", "resume_text": "负责 Python 后端服务开发。"}],
               "require_spec_confirmation": True}
    old = client.post("/jobs", json=payload).json()
    response = client.post(f"/calibrations/{old['job_id']}/requirement/reanalyze", json={
        "requirement": "要求独立负责 Python 后端上线", "base_spec_version": 1})
    assert response.status_code == 200, response.text
    fresh = response.json()
    assert fresh["job_id"] != old["job_id"]
    assert fresh["requirement"] == "要求独立负责 Python 后端上线"
    assert len(fresh["candidates"]) == len(old["candidates"]) == 1
    assert fresh["workflow"]["waiting_for"] == "spec_confirmation"
    assert client.get(f"/calibrations/{old['job_id']}").json()["requirement"] == payload["requirement"]
