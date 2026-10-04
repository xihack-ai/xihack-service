"""Teammate patch integration, preserving the new PDF-import workflow."""
import json

import pytest
from fastapi.testclient import TestClient

from app import llm
from app.main import app
from app.workflow import nodes
from app.hireloop_rules import quote_valid, risk_review
from app.evaluation_metrics import evaluate_rows
from hirespec_loop.errors import ModelTaskError


@pytest.fixture
def confirmed(monkeypatch):
    # Explicit test-only data, not semantic matching in production.
    def extract(cand, criteria):
        return [{"candidate_id": cand["id"], "criterion_id": c["id"],
                 "quote": "Python 后端开发", "stance": "support", "valid": False}
                for c in criteria if c["text"] == "Python 后端开发"]
    monkeypatch.setattr(nodes, "_extract_for_candidate", extract)
    client = TestClient(app)
    result = client.post("/jobs", json={"title": "集成测试", "requirement": "Python 后端开发",
        "require_spec_confirmation": True,
        "candidates": [{"name": "模拟甲", "resume_text": "Python 后端开发，参与项目"},
                       {"name": "模拟乙", "resume_text": "Python 后端开发，参与项目"}]}).json()
    result = client.post(f'/calibrations/{result["job_id"]}/spec/confirm', json={"criteria": [
        {"id": "c1", "type": "MUST_HAVE", "text": "Python 后端开发", "weight": 2}]}).json()
    assert len(result["evaluations"]) == 2
    return client, f'/calibrations/{result["job_id"]}', result


def test_update_preserves_id_zero_reextraction_and_stale_guard(confirmed, monkeypatch):
    client, path, before = confirmed
    original = llm.chat_json
    def reply(messages, **kwargs):
        if "把反馈翻译" in messages[0]["content"]:
            return {"reason": "改为加分项", "add": [], "remove": [], "update": [
                {**before["criteria"][0], "type": "NICE_TO_HAVE"}]}
        return original(messages, **kwargs)
    monkeypatch.setattr(llm, "chat_json", reply)
    pending = client.post(path + "/feedback", json={"text": "Python 改为加分项"}).json()
    assert pending["proposal_impact"]["recomputed_cells"] == 0
    assert pending["evaluations"] == before["evaluations"]
    assert client.post(path + "/approval", json={"action": "accept", "base_spec_version": 99}).status_code == 409
    monkeypatch.setattr(nodes, "_extract_for_candidate", lambda *a: pytest.fail("类型变更不能抽证"))
    after = client.post(path + "/approval", json={"action": "accept", "base_spec_version": 1}).json()
    assert after["criteria"] == [{**before["criteria"][0], "type": "NICE_TO_HAVE"}]
    assert after["recomputation"]["recomputed_cells"] == 0
    assert after["recomputation"]["reused_cells"] == 2
    assert after["evidence"] == before["evidence"]
    assert client.post(path + "/approval", json={"action": "accept"}).status_code == 409


def test_edit_saves_draft_without_publishing_then_accepts(confirmed):
    client, path, before = confirmed
    client.post(path + "/feedback", json={"text": "更看重 Docker"})
    edited = [{**before["criteria"][0], "type": "NICE_TO_HAVE"}]
    draft = client.post(path + "/proposal/edit", json={"criteria": edited, "base_spec_version": 1}).json()
    assert draft["spec_version"] == 1 and draft["criteria"] == before["criteria"]
    assert draft["evaluations"] == before["evaluations"] and draft["approval_draft"] == edited
    assert draft["workflow"]["waiting_for"] == "human_approval"
    after = client.post(path + "/approval", json={"action": "accept", "base_spec_version": 1}).json()
    assert after["spec_version"] == 2 and after["criteria"] == edited


def test_reject_dedup_and_ambiguous_feedback(confirmed):
    client, path, before = confirmed
    client.post(path + "/feedback", json={"text": "更看重 Docker"})
    client.post(path + "/approval", json={"action": "reject"})
    repeated = client.post(path + "/feedback", json={"text": "更看重 Docker"}).json()
    assert repeated["change_proposal"] is None and repeated["feedback_status"] == "NO_CHANGE"
    assert repeated["evaluations"] == before["evaluations"]
    ambiguous = client.post(path + "/feedback", json={"text": "感觉不合适"}).json()
    assert ambiguous["feedback_status"] == "NEEDS_CLARIFICATION" and ambiguous["feedback_question"]
    assert ambiguous["spec_version"] == 1


def test_retired_id_cannot_be_reused_and_delete_reranks_everyone(confirmed):
    client, path, before = confirmed
    client.post(path + "/feedback", json={"text": "更看重 Docker"})
    added = client.post(path + "/approval", json={"action": "accept"}).json()
    retired = added["criteria"][-1]["id"]
    client.post(path + "/feedback", json={"text": "删除 Docker"})
    deleted = client.post(path + "/approval", json={"action": "accept"}).json()
    assert len(deleted["evaluations"]) == 2
    assert deleted["recomputation"]["recomputed_cells"] == 0
    assert all(retired not in e["scores"] for e in deleted["evaluations"])
    client.post(path + "/feedback", json={"text": "更看重 Kubernetes"})
    assert client.post(path + "/proposal/edit", json={"criteria": added["criteria"]}).status_code == 422
    after = client.post(path + "/approval", json={"action": "accept"}).json()
    assert after["criteria"][-1]["id"] != retired


@pytest.mark.parametrize("text", ["只招男性", "年龄不超过35岁", "女性不适合这个岗位", "只招未婚"])
def test_sensitive_feedback_and_edits_blocked(confirmed, text):
    client, path, before = confirmed
    blocked = client.post(path + "/feedback", json={"text": text}).json()
    assert blocked["feedback_status"] == "BLOCKED" and blocked["change_proposal"] is None
    assert blocked["evaluations"] == before["evaluations"]
    assert client.post(path + "/approval", json={"action": "accept"}).status_code == 409
    client.post(path + "/feedback", json={"text": "更看重 Docker"})
    assert client.post(path + "/proposal/edit", json={"criteria": [{**before["criteria"][0], "text": text}]}).status_code == 422


@pytest.mark.parametrize("text", ["至少5年后端经验", "有面向女性用户的产品开发经验", "有婚育服务系统开发经验"])
def test_contextual_safety_does_not_block_product_demographics(text):
    assert risk_review([text]) == []


def test_unknown_and_conflicting_veto_are_not_safe():
    criterion = [{"id": "v1", "type": "VETO", "text": "无法承担值班", "weight": 3}]
    result = nodes._score("A", criterion, [])
    assert result["verdict"] == "待核实" and result["judgments"]["v1"] == "UNKNOWN"
    ev = [{"candidate_id": "A", "criterion_id": "v1", "quote": "原文", "stance": s, "valid": True} for s in ("support", "against")]
    assert nodes._score("A", criterion, ev)["judgments"]["v1"] == "UNKNOWN"
    assert nodes._score("A", criterion, ev[:1])["judgments"]["v1"] == "HIT"
    assert nodes._score("A", criterion, ev[1:])["judgments"]["v1"] == "CLEAR"


def test_all_must_have_missing_is_not_recommended_without_fabricating_negative_evidence():
    criteria = [
        {"id": "c1", "type": "MUST_HAVE", "text": "会 Go", "weight": 3},
        {"id": "c2", "type": "MUST_HAVE", "text": "后端开发经验", "weight": 3},
    ]
    missing = nodes._score("A", criteria, [])
    assert missing["verdict"] == "不推荐"
    assert missing["scores"] == {"c1": None, "c2": None}
    assert missing["judgments"] == {"c1": "NO_EVIDENCE", "c2": "NO_EVIDENCE"}
    assert "不代表候选人没有相关能力" in missing["tier_reason"]

    partial = nodes._score("A", criteria, [
        {"candidate_id": "A", "criterion_id": "c1", "quote": "使用 Go 开发服务", "stance": "support", "valid": True}
    ])
    assert partial["verdict"] == "待核实"
    assert partial["scores"]["c2"] is None


def test_strict_quote_and_metrics_with_task_failure():
    assert not quote_valid("FastAPI‑based 服务\n使用 Python", "FastAPI-based 服务 使用 Python")
    rows = [{"candidate_id": "A", "criterion_id": "C1", "definition_version": "v1", "kind": "criterion",
             "expected": "NOT_SATISFIED", "predicted": "NOT_SATISFIED", "resume_text": "带教2人", "quotes": ["带教5人"]},
            {"candidate_id": "B", "criterion_id": "C1", "definition_version": "v1", "kind": "criterion",
             "expected": "NO_EVIDENCE", "predicted": None, "error": "timeout"}]
    report = evaluate_rows(rows)
    assert report["hallucination_count"] == 1 and report["task_failure_rate"] == .5
    assert report["criterion"]["accuracy"] == 1 and report["end_to_end_accuracy"] == .5


def test_replay_never_networks_and_missing_record_is_error(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("HIRELOOP_MODEL_MODE", "REPLAY")
    monkeypatch.setattr(llm, "CACHE_FILE", tmp_path / "cache.json")
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: pytest.fail("回放不得联网"))
    # Restore real JSON parser, bypassing the globally injected test model.
    messages = [{"role": "user", "content": "test"}]
    with pytest.raises(ModelTaskError):
        llm.chat(messages)
    key = llm._cache_key("gpt-4o-mini", messages)
    llm.CACHE_FILE.write_text(json.dumps({key: '{"ok":true}'}))
    assert llm.chat(messages) == '{"ok":true}'


def test_optional_semantic_audit_fails_closed_and_only_audits_touched_cells(monkeypatch):
    monkeypatch.setenv("HIRELOOP_SEMANTIC_REVIEW", "1")
    c = {"id": "c1", "type": "MUST_HAVE", "text": "管理5人", "weight": 3}
    ev = {"candidate_id": "A", "criterion_id": "c1", "quote": "带教2人", "stance": "support", "valid": False}
    state = {"candidates": [{"id": "A", "resume_text": "带教2人"}], "criteria": [c], "evidence": [ev]}
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {"supported": False})
    assert nodes.validate_evidence(state)["evidence"] == []
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {})
    with pytest.raises(ModelTaskError):
        nodes.validate_evidence(state)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: pytest.fail("类型变更不可审核旧证据"))
    assert nodes.validate_evidence({**state, "reeval_criteria": []})["evidence"] == [ev]
