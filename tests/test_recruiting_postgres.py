"""Opt-in real PostgreSQL integration tests; only test-created jobs are deleted.

Enable HIRESPEC_RUN_DB_TESTS=1 with a running tunnel and a runtime PGPASSWORD.
No schema changes, no existing jobs queried or modified.
"""

import asyncio
import os
from uuid import uuid4

import pytest
import psycopg
from fastapi.testclient import TestClient

from app.main import app
from app.recruiting_service import PersistentLoop
from app.recruiting_store import CONTRACT, PREFIX, JobBusy, NotFound, RecruitingStore, StoreError
from hirespec_loop.errors import ApprovalError, ModelTaskError, PolicyError, StaleProposalError
from hirespec_loop.fixtures import (
    ANSWERS, OWNERSHIP_FEEDBACK, PROMOTION_FEEDBACK, REQUIREMENT, VAGUE_FEEDBACK,
    OfflineDemoClient, load_candidates, ownership_body, seed_bodies,
)
from hirespec_loop.models import ChangeOperation, ProposedChange
from hirespec_loop.prompts import EVALUATE, PROPOSE
from hirespec_loop.service import HireSpecAI

pytestmark = pytest.mark.skipif(os.getenv("HIRESPEC_RUN_DB_TESTS") != "1", reason="requires explicit real-database test opt-in")


@pytest.fixture
def scenario(request):
    actor = "test-" + str(uuid4())
    store = RecruitingStore()
    client = OfflineDemoClient()
    loop = PersistentLoop(store, HireSpecAI(client), model_mode="offline_demo")
    job = loop.create_job("[HireSpec 自动测试] 可清理独立职位", REQUIREMENT, actor)
    job_id = job["id"]
    def cleanup():
        # Exact generated UUID plus owner marker; never cleanup by name/prefix alone.
        with store.write_job(job_id, actor) as (conn, row):
            assert row["title"].startswith("[HireSpec 自动测试]")
            conn.execute("DELETE FROM jobs WHERE id=%s", (job_id,))
    request.addfinalizer(cleanup)
    candidates = {}
    for candidate in load_candidates():
        candidates[candidate.candidate_id] = store.add_candidate(job_id, actor, candidate.candidate_id, candidate.resume_text)
    draft = asyncio.run(loop.draft(job_id, actor, ANSWERS))
    first = asyncio.run(loop.approve_initial(job_id, actor, draft["draft_id"]))
    value = dict(actor=actor, store=store, client=client, loop=loop, job_id=job_id, candidates=candidates, first=first)
    return value


def feedback(s, reason=OWNERSHIP_FEEDBACK, version=1):
    return asyncio.run(s["loop"].feedback(s["job_id"], s["actor"], s["candidates"]["P01"]["id"], "NO", reason, version))


def test_durable_approval_restart_and_partial_cache(scenario):
    s = scenario
    p = feedback(s)
    assert s["store"].get_job(s["job_id"], s["actor"])["job"]["current_spec_version"] == 1
    accepted = asyncio.run(s["loop"].accept(s["job_id"], s["actor"], p["proposal"]["proposal_id"]))
    assert accepted["snapshot"]["model_evaluations"] == 6
    assert accepted["snapshot"]["cache_hits"] == 18
    restarted = PersistentLoop(RecruitingStore(), HireSpecAI(OfflineDemoClient()), model_mode="offline_demo")
    repeat = asyncio.run(restarted.accept(s["job_id"], s["actor"], p["proposal"]["proposal_id"]))
    assert repeat["already_decided"] and repeat["receipt"] == accepted["receipt"]
    view = restarted.store.get_job(s["job_id"], s["actor"])
    assert view["visible_spec_version"] == view["job"]["current_spec_version"] == 2
    assert len(view["visible_snapshot"]["records"]) == 24
    with s["store"].connect(read_only=True) as conn:
        assert conn.execute("SELECT count(*) AS n FROM spec_versions WHERE job_id=%s", (s["job_id"],)).fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM evaluations WHERE job_id=%s", (s["job_id"],)).fetchone()["n"] == 12
        assert isinstance(conn.execute("SELECT criteria FROM spec_versions WHERE job_id=%s AND version=2", (s["job_id"],)).fetchone()["criteria"], list)


def test_category_only_change_reuses_every_evaluation(scenario):
    s = scenario
    p = feedback(s, PROMOTION_FEEDBACK)
    result = asyncio.run(s["loop"].accept(s["job_id"], s["actor"], p["proposal"]["proposal_id"]))
    assert result["snapshot"]["model_evaluations"] == 0
    assert result["snapshot"]["cache_hits"] == 24


@pytest.mark.parametrize("reason,status", [
    ("女性不适合这个岗位。", "BLOCKED"),
    ("这个学校我不喜欢。", "BLOCKED"),
    ("他有上线经验，但年龄大了。", "BLOCKED"),
    (VAGUE_FEEDBACK, "NEEDS_CLARIFICATION"),
])
def test_sensitive_or_vague_feedback_cannot_be_accepted(scenario, reason, status):
    s = scenario
    p = feedback(s, reason)
    assert p["proposal"]["status"] == status and not p["can_approve"]
    with pytest.raises(ApprovalError):
        asyncio.run(s["loop"].accept(s["job_id"], s["actor"], p["proposal"]["proposal_id"]))
    assert s["store"].get_job(s["job_id"], s["actor"])["job"]["current_spec_version"] == 1


def test_edit_is_only_draft_and_sensitive_edit_is_blocked(scenario):
    s = scenario
    p = feedback(s)
    change = ProposedChange(operation=ChangeOperation.UPDATE, criterion_id="C02", after=ownership_body())
    result = asyncio.run(s["loop"].edit(s["job_id"], s["actor"], p["proposal"]["proposal_id"], (change,)))
    assert result["approval_status"] == "PENDING"
    assert s["store"].get_job(s["job_id"], s["actor"])["visible_spec_version"] == 1
    bad = ownership_body().model_copy(update={"definition": "女性不适合这个岗位"})
    with pytest.raises(PolicyError):
        asyncio.run(s["loop"].edit(s["job_id"], s["actor"], p["proposal"]["proposal_id"], (change.model_copy(update={"after": bad}),)))
    assert s["store"].get_job(s["job_id"], s["actor"])["job"]["current_spec_version"] == 1


def test_reject_is_durable_and_does_not_publish(scenario):
    s = scenario
    p = feedback(s)
    pid = p["proposal"]["proposal_id"]
    one = s["store"].reject(s["job_id"], s["actor"], pid)
    assert s["store"].reject(s["job_id"], s["actor"], pid) == one
    with pytest.raises(ApprovalError):
        asyncio.run(s["loop"].accept(s["job_id"], s["actor"], pid))
    assert s["store"].get_job(s["job_id"], s["actor"])["visible_spec_version"] == 1


def test_wrong_owner_stale_feedback_and_foreign_candidates_are_rejected(scenario):
    s = scenario
    with pytest.raises(NotFound):
        s["store"].get_job(s["job_id"], "other-manager")
    with pytest.raises(StaleProposalError):
        feedback(s, version=2)
    with pytest.raises(NotFound):
        s["store"].label(s["job_id"], s["actor"], str(uuid4()), True, "test")


def test_job_lease_blocks_competing_mutation(scenario):
    s = scenario
    with s["store"].lease(s["job_id"], s["actor"]):
        with pytest.raises(JobBusy):
            s["store"].add_candidate(s["job_id"], s["actor"], "new", "new resume")


class FailingEvaluationClient(OfflineDemoClient):
    async def generate_json(self, system_prompt, payload, output_schema):
        if system_prompt == EVALUATE:
            raise TimeoutError("private-provider-details")
        return await super().generate_json(system_prompt, payload, output_schema)


def test_failed_recompute_keeps_approved_spec_and_old_complete_ranking(scenario):
    s = scenario
    p = feedback(s)
    broken = PersistentLoop(s["store"], HireSpecAI(FailingEvaluationClient()), model_mode="offline_demo")
    with pytest.raises(ModelTaskError):
        asyncio.run(broken.accept(s["job_id"], s["actor"], p["proposal"]["proposal_id"]))
    view = s["store"].get_job(s["job_id"], s["actor"])
    assert view["job"]["current_spec_version"] == 2
    assert view["job"]["status"] == "FAILED" and view["visible_spec_version"] == 1
    assert view["recompute"]["error_code"] == "MODEL_TASK_ERROR"
    duplicate = asyncio.run(broken.accept(s["job_id"], s["actor"], p["proposal"]["proposal_id"]))
    assert duplicate["already_decided"]
    recovered = asyncio.run(s["loop"].recompute(s["job_id"], s["actor"]))
    assert recovered.spec_version == 2 and recovered.model_evaluations == 6


class FailingProposalClient(OfflineDemoClient):
    async def generate_json(self, system_prompt, payload, output_schema):
        if system_prompt == PROPOSE:
            raise TimeoutError("private-provider-details")
        return await super().generate_json(system_prompt, payload, output_schema)


def test_feedback_survives_failed_proposal_and_can_be_retried(scenario):
    s = scenario
    broken = PersistentLoop(s["store"], HireSpecAI(FailingProposalClient()), model_mode="offline_demo")
    with pytest.raises(ModelTaskError) as caught:
        asyncio.run(broken.feedback(s["job_id"], s["actor"], s["candidates"]["P01"]["id"], "NO", OWNERSHIP_FEEDBACK, 1))
    fid = caught.value.feedback_id
    result = asyncio.run(s["loop"].propose(s["job_id"], s["actor"], fid))
    assert result["proposal"]["feedback_id"] == fid


def test_real_http_job_read_and_label_write(scenario, monkeypatch):
    s = scenario
    monkeypatch.setenv("HIRESPEC_API_TOKEN", "integration-local-token")
    monkeypatch.setenv("HIRESPEC_LOCAL_ACTOR", s["actor"])
    headers = {"Authorization": "Bearer integration-local-token"}
    client = TestClient(app)
    response = client.get(f"/api/hiring/jobs/{s['job_id']}", headers=headers)
    assert response.status_code == 200 and response.json()["visible_spec_version"] == 1
    response = client.put(f"/api/hiring/jobs/{s['job_id']}/candidates/{s['candidates']['P04']['id']}/label", headers=headers, json={"label": True, "split": "calibration"})
    assert response.status_code == 200
    with s["store"].connect(read_only=True) as conn:
        assert conn.execute("SELECT label FROM candidate_labels WHERE candidate_id=%s", (s["candidates"]["P04"]["id"],)).fetchone()["label"] is True


def test_only_one_pending_proposal_and_saved_feedback_retry(scenario):
    s = scenario
    first = feedback(s)
    with pytest.raises(StoreError) as caught:
        feedback(s)
    assert caught.value.feedback_id
    s["store"].reject(s["job_id"], s["actor"], first["proposal"]["proposal_id"])
    second = asyncio.run(s["loop"].propose(s["job_id"], s["actor"], caught.value.feedback_id))
    assert second["can_approve"]


def test_full_feedback_edit_accept_and_duplicate_through_http(scenario, monkeypatch):
    from app.recruiting_api import get_ai
    s = scenario
    monkeypatch.setenv("HIRESPEC_API_TOKEN", "integration-local-token")
    monkeypatch.setenv("HIRESPEC_LOCAL_ACTOR", s["actor"])
    monkeypatch.setenv("HIRESPEC_AI_MODE", "offline_demo")
    app.dependency_overrides[get_ai] = lambda: HireSpecAI(OfflineDemoClient())
    headers = {"Authorization": "Bearer integration-local-token"}
    client = TestClient(app)
    base = f"/api/hiring/jobs/{s['job_id']}"
    try:
        result = client.post(base+"/feedback", headers=headers, json={
            "candidate_id": s["candidates"]["P01"]["id"], "spec_version": 1,
            "decision": "NO", "reason": OWNERSHIP_FEEDBACK,
        })
        assert result.status_code == 200
        pid = result.json()["proposal"]["proposal_id"]
        path = base+f"/proposals/{pid}"
        assert client.get(path, headers=headers).json()["can_approve"]
        change = ProposedChange(operation=ChangeOperation.UPDATE, criterion_id="C02", after=ownership_body())
        edit = client.put(path, headers=headers, json={"changes": [change.model_dump(mode="json")]})
        assert edit.status_code == 200
        assert client.get(base, headers=headers).json()["visible_spec_version"] == 1
        accepted = client.post(path+"/accept", headers=headers)
        assert accepted.status_code == 200 and accepted.json()["snapshot"]["model_evaluations"] == 6
        repeated = client.post(path+"/accept", headers=headers)
        assert repeated.status_code == 200 and repeated.json()["already_decided"]
        assert client.get(base, headers=headers).json()["visible_spec_version"] == 2
    finally:
        app.dependency_overrides.pop(get_ai, None)


def test_snapshot_write_failure_rolls_back_whole_version_and_retains_cache(scenario, monkeypatch):
    s = scenario
    p = feedback(s)
    original_publish = s["store"].publish_snapshot
    def broken_publish(conn, actor, snapshot):
        # Write the complete candidate matrix, then simulate a failing transaction.
        # All evaluation rows and RANKING_PUBLISHED must roll back together.
        original_publish(conn, actor, snapshot)
        raise psycopg.OperationalError("simulated-write-failure")
    monkeypatch.setattr(s["store"], "publish_snapshot", broken_publish)
    with pytest.raises(psycopg.OperationalError):
        asyncio.run(s["loop"].accept(s["job_id"], s["actor"], p["proposal"]["proposal_id"]))
    view = s["store"].get_job(s["job_id"], s["actor"])
    assert view["job"]["current_spec_version"] == 2 and view["visible_spec_version"] == 1
    assert view["job"]["status"] == "FAILED" and view["snapshot_stale"]
    with s["store"].connect(read_only=True) as conn:
        assert conn.execute("SELECT count(*) AS n FROM evaluations WHERE job_id=%s AND spec_version=2", (s["job_id"],)).fetchone()["n"] == 0
    monkeypatch.setattr(s["store"], "publish_snapshot", original_publish)
    recovered = asyncio.run(s["loop"].recompute(s["job_id"], s["actor"]))
    assert recovered.spec_version == 2 and recovered.model_evaluations == 0 and recovered.cache_hits == 24


def test_initial_spec_can_be_approved_before_resume_import():
    actor = "test-" + str(uuid4())
    store = RecruitingStore()
    loop = PersistentLoop(store, HireSpecAI(OfflineDemoClient()), model_mode="offline_demo")
    job = loop.create_job("[HireSpec 自动测试] 可清理独立职位", REQUIREMENT, actor)
    try:
        draft = asyncio.run(loop.draft(job["id"], actor, ANSWERS))
        result = asyncio.run(loop.approve_initial(job["id"], actor, draft["draft_id"]))
        assert result["spec"]["version"] == 1 and result["snapshot"] is None and result["requires_candidates"]
        view = store.get_job(job["id"], actor)
        assert view["job"]["current_spec_version"] == 1 and view["visible_snapshot"] is None
    finally:
        with store.write_job(job["id"], actor) as (conn, row):
            assert row["title"].startswith("[HireSpec 自动测试]")
            conn.execute("DELETE FROM jobs WHERE id=%s", (job["id"],))
