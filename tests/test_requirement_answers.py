"""Initial ambiguity must be answerable before a job or candidate evaluation exists."""
from fastapi.testclient import TestClient
from app.main import app
from app import main
from app.workflow import nodes


def test_ambiguous_requirement_returns_questions_without_creating_job(monkeypatch):
    monkeypatch.setattr(main, "_save_job", lambda *_: (_ for _ in ()).throw(AssertionError("must not save")))
    monkeypatch.setattr(main, "risk_assessment", lambda _: {"status": "NEEDS_CLARIFICATION", "reason": "定义不明确", "questions": [{"question": "独立负责哪些职责？", "options": ["方案和上线", "非必要"]}]})
    response = TestClient(app).post("/requirements/clarify", json={"requirement": "高级后端"})
    assert response.status_code == 200
    assert response.json()["status"] == "NEEDS_CLARIFICATION"
    assert response.json()["questions"][0]["options"] == ["方案和上线", "非必要"]


def test_safe_requirement_still_asks_generated_questions(monkeypatch):
    monkeypatch.setattr(nodes, "clarify", lambda _: {"clarification_questions": ["需要管理团队吗？"], "clarified_requirement": "后端交付"})
    response = TestClient(app).post("/requirements/clarify", json={"requirement": "后端交付"})
    assert response.status_code == 200
    assert response.json()["status"] == "NEEDS_CLARIFICATION"
    assert response.json()["questions"][0]["question"] == "需要管理团队吗？"


def test_answers_reach_grounded_spec_and_pause_before_evaluation(monkeypatch):
    body = {"requirement": "高级后端", "answers": [{"question": "独立负责哪些职责？", "answer": "负责方案和上线，不要求人员管理"}]}
    seen = []
    def review(texts):
        seen.append(texts[0])
        return {"status": "SAFE", "reason": "已明确", "questions": []}
    monkeypatch.setattr(main, "risk_assessment", review)
    client = TestClient(app)
    assert client.post("/requirements/clarify", json=body).json()["status"] == "SAFE"
    monkeypatch.setattr(nodes, "_extract_for_candidate", lambda *_: (_ for _ in ()).throw(AssertionError("must not evaluate")))
    response = client.post("/jobs", json={"title": "后端", "requirement": body["requirement"], "clarification_answers": body["answers"], "require_spec_confirmation": True, "candidates": [{"name": "甲", "resume_text": "项目经历"}]})
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["workflow"]["waiting_for"] == "spec_confirmation"
    assert out["evaluations"] == []
    assert out["requirement"].startswith("高级后端")
    assert "负责方案和上线，不要求人员管理" in out["requirement"]
    assert len(seen) == 2


def test_sensitive_answer_is_blocked_in_preflight_and_create():
    body = {"requirement": "后端开发", "answers": [{"question": "招聘要求？", "answer": "只招男性"}]}
    client = TestClient(app)
    assert client.post("/requirements/clarify", json=body).json()["status"] == "BLOCKED"
    created = client.post("/jobs", json={"title": "后端", "requirement": body["requirement"], "clarification_answers": body["answers"]})
    assert created.status_code == 422
    assert created.json()["detail"]["code"] == "POLICY_BLOCKED"


def test_more_clarification_and_structured_gate_error(monkeypatch):
    question = {"question": "大型公司具体规模？", "options": []}
    monkeypatch.setattr(main, "risk_assessment", lambda _: {"status": "NEEDS_CLARIFICATION", "reason": "还需说明", "questions": [question]})
    body = {"requirement": "大厂", "answers": [{"question": "哪些公司？", "answer": "大型公司"}]}
    client = TestClient(app)
    assert client.post("/requirements/clarify", json=body).json()["questions"] == [question]
    result = client.post("/jobs", json={"title": "后端", "requirement": "大厂", "clarification_answers": body["answers"]})
    assert result.status_code == 422
    assert result.json()["detail"]["questions"] == [question]


def test_blank_answer_rejected_and_model_failure_not_a_question(monkeypatch):
    from hirespec_loop.errors import ModelTaskError
    client = TestClient(app)
    assert client.post("/requirements/clarify", json={"requirement": "后端", "answers": [{"question": "职责？", "answer": " "}]}).status_code == 422
    def broken(_):
        raise ModelTaskError("模型调用失败")
    monkeypatch.setattr(main, "risk_assessment", broken)
    failed = client.post("/requirements/clarify", json={"requirement": "后端"})
    assert failed.status_code == 503
    assert failed.json()["detail"]["code"] == "MODEL_TASK_ERROR"


def test_company_list_and_priority_survive_draft_and_confirmation(monkeypatch):
    import json
    from app import llm
    original = llm.chat_json
    generated = []
    def reply(messages, **kwargs):
        system = messages[0]['content']
        user = messages[-1]['content']
        if '判断招聘文本' in system:
            texts = json.loads(user)['texts']
            if texts[0].startswith('标准类型：'):
                assert 'NICE_TO_HAVE' in texts[0]
                if not all(company in texts[0] for company in ['腾讯','阿里','字节','美团']):
                    return {'status':'NEEDS_CLARIFICATION','reason':'标准遗漏公司名单','questions':[{'question':'哪些公司？'}]}
            return {'status':'SAFE','reason':'名单和优先级已明确','questions':[]}
        if 'source_quote' in system:
            generated.append(user)
            text = '一线大厂工作经验' if len(generated) == 1 else '曾在腾讯、阿里、字节或美团工作'
            return {'criteria':[{'id':'c1','type':'NICE_TO_HAVE','text':text,'weight':2,
                'source_quote':'最好有一线大厂工作经验' if len(generated) == 1 else '腾讯 阿里 字节 美团'}]}
        return original(messages, **kwargs)
    monkeypatch.setattr(llm,'chat_json',reply)
    client = TestClient(app)
    answers = [{'question':'一线大厂具体指哪些公司？','answer':'腾讯 阿里 字节 美团'},
               {'question':'是必须项还是优先项？','answer':'优先考虑项，非必须'}]
    result = client.post('/jobs',json={'title':'后端','requirement':'后端工程师，最好有一线大厂工作经验',
        'clarification_answers':answers,'require_spec_confirmation':True})
    assert result.status_code == 200, result.text
    draft = result.json()
    assert len(generated) == 2
    assert 'repair_reason' in generated[1]
    assert draft['criteria'][0]['type'] == 'NICE_TO_HAVE'
    assert all(name in draft['criteria'][0]['text'] for name in ['腾讯','阿里','字节','美团'])
    confirmed = client.post(f"/calibrations/{draft['job_id']}/spec/confirm",json={'criteria':draft['criteria']})
    assert confirmed.status_code == 200, confirmed.text


def test_repeated_draft_loss_is_task_failure_not_repeated_manager_question(monkeypatch):
    import pytest
    from hirespec_loop.errors import ClarificationRequired, ModelTaskError
    def ambiguous(_):
        raise ClarificationRequired('哪些公司？')
    monkeypatch.setattr(nodes,'check_criteria_risk',ambiguous)
    monkeypatch.setattr(nodes,'risk_review',lambda _: [])
    monkeypatch.setattr(nodes.llm,'chat_json',lambda *args,**kwargs: {'criteria': [
        {'id':'c1','type':'NICE_TO_HAVE','text':'后端开发','weight':2,'source_quote':'后端开发'}]})
    with pytest.raises(ModelTaskError,match='已有回答不需重复填写'):
        nodes.generate_spec({'requirement':'后端开发，优先腾讯 阿里 字节 美团经历','spec_version':1})
