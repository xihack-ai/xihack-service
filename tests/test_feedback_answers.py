"""Semantic review and conversation regressions; replies below are test doubles."""
import json

import pytest
from fastapi.testclient import TestClient

from app import llm
from app.main import app
from app.hireloop_rules import risk_assessment
from app.workflow import nodes


@pytest.mark.parametrize("text", ["必须有大厂经验", "只要985", "这个学校我不喜欢", "主动性强", "适应快节奏"])
def test_ambiguous_requirements_are_questions_not_blocks(monkeypatch, text):
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {
        "status": "NEEDS_CLARIFICATION", "reason": "需明确岗位要求",
        "questions": [{"question": "希望核验什么具体岗位经验？", "options": ["系统设计", "交付经验"]}]})
    result = risk_assessment([text])
    assert result["status"] == "NEEDS_CLARIFICATION"
    assert result["source_texts"] == [text]


def setup_job(monkeypatch):
    def extract(candidate, criteria):
        return [{"candidate_id": candidate["id"], "criterion_id": c["id"],
                 "quote": "后端服务接口开发经验", "stance": "support", "valid": False}
                for c in criteria if c["text"] == "后端服务接口开发经验"]
    monkeypatch.setattr(nodes, '_extract_for_candidate', extract)
    client = TestClient(app)
    draft = client.post('/jobs', json={"title": "测试", "requirement": "后端开发", "require_spec_confirmation": True,
        "candidates": [{"name": "模拟候选人", "resume_text": "后端服务接口开发经验，主动发现API超时并推动优化上线。"}]}).json()
    path = f'/calibrations/{draft["job_id"]}'
    before = client.post(path + '/spec/confirm', json={"criteria": [{"id": "c1", "type": "MUST_HAVE", "text": "后端服务接口开发经验", "weight": 2}]}).json()
    assert len(before['evaluations']) == 1
    original = llm.chat_json
    def reply(messages, **kwargs):
        system = messages[0]['content']
        if '判断招聘文本' in system:
            text = json.loads(messages[-1]['content'])['texts'][0]
            if text == '更主动':
                return {"status": "NEEDS_CLARIFICATION", "reason": "请说明主动性",
                    "questions": [{"question": "希望看到什么行为？", "options": ["发现问题并推动改进"]}]}
            return {"status": "SAFE", "reason": "岗位相关"}
        if '把反馈翻译' in system:
            return {"status": "PENDING_APPROVAL", "reason": "明确主动改进要求",
                "add": [{"text": "发现技术问题并推动改进落地", "type": "NICE_TO_HAVE", "weight": 2}], "update": [], "remove": []}
        return original(messages, **kwargs)
    monkeypatch.setattr(llm, 'chat_json', reply)
    return client, path, before


def test_answer_continues_feedback_without_publishing(monkeypatch):
    client, path, before = setup_job(monkeypatch)
    pending = client.post(path + '/feedback', json={"text": "更主动"}).json()
    assert pending['feedback_status'] == 'NEEDS_CLARIFICATION' and not pending['risk_flags']
    assert pending['criteria'] == before['criteria']
    body = {"feedback_id": pending['feedback_id'], "base_spec_version": 1, "answers": ["发现技术问题并推动改进落地"]}
    assert client.post(path + '/feedback/answer', json={**body, "base_spec_version": 2}).status_code == 409
    assert client.post(path + '/feedback/answer', json={**body, "feedback_id": "stale"}).status_code == 409
    assert client.post(path + '/feedback/answer', json={**body, "answers": [" "]}).status_code == 422
    result = client.post(path + '/feedback/answer', json=body).json()
    assert result['feedback_status'] == 'PENDING_APPROVAL'
    assert result['spec_version'] == 1 and result['criteria'] == before['criteria']
    assert result['evaluations'] == before['evaluations']
    assert result['feedback_thread']['original'] == '更主动'
    assert result['feedback_thread']['rounds'][0]['answers'] == body['answers']
    assert client.post(path + '/feedback/answer', json=body).status_code == 409
    approved = client.post(path + '/approval', json={"action": "accept", "base_spec_version": 1}).json()
    assert approved['spec_version'] == 2


def test_sensitive_answer_cannot_bypass_review(monkeypatch):
    client, path, before = setup_job(monkeypatch)
    pending = client.post(path + '/feedback', json={"text": "更主动"}).json()
    result = client.post(path + '/feedback/answer', json={"feedback_id": pending['feedback_id'],
        "base_spec_version": 1, "answers": ["只招男性"]}).json()
    assert result['feedback_status'] == 'BLOCKED'
    assert result['criteria'] == before['criteria'] and result['spec_version'] == 1
    assert client.post(path + '/approval', json={"action": "accept"}).status_code == 409


def test_review_failure_is_task_error(monkeypatch):
    client, path, _ = setup_job(monkeypatch)
    monkeypatch.setattr(llm, 'chat_json', lambda *a, **k: {})
    result = client.post(path + '/feedback', json={"text": "大厂经验"})
    assert result.status_code == 503
    assert result.json()['detail']['code'] == 'MODEL_TASK_ERROR'


def test_failed_answer_can_retry_same_round(monkeypatch):
    client, path, before = setup_job(monkeypatch)
    pending = client.post(path + '/feedback', json={"text": "更主动"}).json()
    body = {"feedback_id": pending['feedback_id'], "base_spec_version": 1, "answers": ["发现问题并推动改进"]}
    original = llm.chat_json
    monkeypatch.setattr(llm, 'chat_json', lambda *a, **k: {})
    assert client.post(path + '/feedback/answer', json=body).status_code == 503
    state = client.get(path).json()
    assert state['feedback_id'] == pending['feedback_id']
    assert state['feedback_status'] == 'NEEDS_CLARIFICATION' and state['workflow']['next_nodes'] == []
    assert state['criteria'] == before['criteria'] and state['feedback_thread']['rounds'] == []
    monkeypatch.setattr(llm, 'chat_json', original)
    assert client.post(path + '/feedback/answer', json=body).json()['feedback_status'] == 'PENDING_APPROVAL'
