"""Regressions for invented thresholds and partial/keyword evidence."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.workflow import nodes
from app.workflow.grounding import Assessment, decisive_quotes, grounded_standard
from hirespec_loop.errors import ModelTaskError


def test_seniority_does_not_authorize_five_years():
    source = '需要高级Python后端'
    assert not grounded_standard({'text': '5年以上Python经验', 'source_quote': source}, source)
    assert grounded_standard({'text': 'Python后端开发经验', 'source_quote': source}, source)


@pytest.mark.parametrize('text', ['高级Python后端', '不要求带团队', '无需管理团队'])
def test_unresolved_labels_and_waived_requirements_are_not_criteria(text):
    assert not grounded_standard({'text': text, 'source_quote': text}, text)


def test_waiver_does_not_authorize_veto_for_the_opposite():
    source = '不要求带团队'
    assert not grounded_standard({'text': '要求带团队', 'type': 'VETO', 'source_quote': source}, source)
    assert not grounded_standard({'text': '管理团队经验', 'type': 'NICE_TO_HAVE', 'source_quote': source}, source)


@pytest.mark.parametrize('source', ['至少5年Python经验', '至少五年Python经验', '至少60个月Python经验'])
def test_explicit_years_remain_allowed(source):
    assert grounded_standard({'text': '至少5年Python经验', 'source_quote': source}, source)


def test_clarification_hallucination_is_not_a_source(monkeypatch):
    monkeypatch.setattr(nodes.llm, 'is_configured', lambda: True)
    monkeypatch.setattr(nodes.llm, 'replay_only', lambda: False)
    monkeypatch.setattr(nodes.store, 'save_spec', lambda *a, **k: None)
    monkeypatch.setattr(nodes.llm, 'chat_json', lambda *a, **k: {'criteria': [
        {'id': 'bad', 'text': '5年Python经验', 'type': 'MUST_HAVE', 'weight': 3, 'source_quote': '高级Python后端'},
        {'id': 'good', 'text': 'Python后端开发', 'type': 'MUST_HAVE', 'weight': 3, 'source_quote': 'Python后端'}]})
    out = nodes.generate_spec({'requirement': '高级Python后端', 'clarified_requirement': '5年Python经验'})
    assert [c['id'] for c in out['criteria']] == ['good']


def assessment(verdict, quotes):
    return Assessment(criterion_id='c1', verdict=verdict, evidence=quotes, reason='测试')


@pytest.mark.parametrize('quote', ['使用Python开发服务', '3年Python开发经验'])
def test_program_rejects_unsupported_years_even_if_model_says_satisfied(quote):
    assert decisive_quotes(assessment('SATISFIED', [quote]), quote, '至少5年Python经验') == []


def test_explicit_long_enough_years_supported():
    quote = '累计6年Python后端开发经验'
    assert decisive_quotes(assessment('SATISFIED', [quote]), quote, '至少5年Python经验') == [quote]


@pytest.mark.parametrize('quote', ['2020年使用Python开发项目', '累计8年Java开发经验。使用Python开发接口'])
def test_calendar_years_and_unrelated_experience_do_not_prove_python_years(quote):
    assert decisive_quotes(assessment('SATISFIED', [quote]), quote, '至少5年Python经验') == []


def test_one_bad_quote_invalidates_entire_verdict():
    assert decisive_quotes(assessment('SATISFIED', ['使用Python', '不存在的引用']), '使用Python', 'Python经验') == []


def test_whitespace_rewriting_is_not_literal_evidence():
    assert decisive_quotes(assessment('SATISFIED', ['Python开发']), 'Python 开发', 'Python经验') == []


def test_no_evidence_does_not_become_keyword_support(monkeypatch):
    monkeypatch.setattr(nodes.llm, 'is_configured', lambda: True)
    monkeypatch.setattr(nodes.llm, 'replay_only', lambda: False)
    seen = []
    def reply(messages, **kwargs):
        seen.append(messages)
        return {'criterion_id': 'c1', 'verdict': 'NO_EVIDENCE', 'evidence': ['参与AI项目'], 'reason': '没有独立上线证据'}
    monkeypatch.setattr(nodes.llm, 'chat_json', reply)
    criteria = [{'id': 'c1', 'text': '独立负责AI系统上线', 'type': 'MUST_HAVE', 'weight': 3}]
    result = nodes._extract_for_candidate({'id': 'p1', 'resume_text': '参与AI项目'}, criteria)
    assert result == []
    assert 'MUST_HAVE' not in seen[0][1]['content']
    assert nodes._score('p1', criteria, result)['scores']['c1'] is None


def test_invalid_model_result_is_task_error_not_unknown(monkeypatch):
    monkeypatch.setattr(nodes.llm, 'is_configured', lambda: True)
    monkeypatch.setattr(nodes.llm, 'replay_only', lambda: False)
    monkeypatch.setattr(nodes.llm, 'chat_json', lambda *a, **k: {})
    with pytest.raises(ModelTaskError):
        nodes._extract_for_candidate({'id': 'p1', 'resume_text': 'Python'}, [{'id': 'c1', 'text': 'Python', 'type': 'MUST_HAVE', 'weight': 3}])


def test_live_semantic_verdict_uses_explicit_reverse_evidence(monkeypatch):
    monkeypatch.setattr(nodes.llm, 'is_configured', lambda: True)
    monkeypatch.setattr(nodes.llm, 'replay_only', lambda: False)
    quote = '仅负责联调，未参与技术方案和上线'
    monkeypatch.setattr(nodes.llm, 'chat_json', lambda *a, **k: {
        'criterion_id': 'c1', 'verdict': 'NOT_SATISFIED', 'evidence': [quote], 'reason': '明确反证'})
    criteria = [{'id': 'c1', 'text': '独立负责技术方案和上线', 'type': 'MUST_HAVE', 'weight': 3}]
    raw = nodes._extract_for_candidate({'id': 'p1', 'resume_text': quote}, criteria)
    verified = nodes._validate_evidence([{'id': 'p1', 'resume_text': quote}], criteria, raw)
    assert nodes._score('p1', criteria, verified)['scores']['c1'] == 0


@pytest.mark.offline_llm
def test_offline_refuses_to_invent_criteria():
    """离线不再编造兜底标准:直接拒绝并如实报错,绝不产出虚构结果。"""
    r = TestClient(app).post('/jobs', json={'title': '测试', 'requirement': 'Python后端',
        'candidates': [{'name': '模拟甲', 'resume_text': 'Python项目技术栈匹配团队要求'}]})
    assert r.status_code == 503
    assert r.json()['detail']['code'] == 'MODEL_TASK_ERROR'
