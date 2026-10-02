import asyncio

import pytest
from pydantic import ValidationError

from hirespec_loop.errors import InputError, ModelTaskError
from hirespec_loop.evaluation import semantic_hash, text_hash, verify_evaluation
from hirespec_loop.fixtures import load_candidates, ownership_body, seed_bodies
from hirespec_loop.models import Category, Criterion, EvaluationDraft, EvidenceDraft, SpecDraft, Verdict
from hirespec_loop.service import HireSpecAI

from .conftest import ControlledClient


def criterion(body=None, criterion_id="C02"):
    return Criterion(criterion_id=criterion_id, **(body or ownership_body()).model_dump())


def evaluation(verdict="SATISFIED", quotes=("负责上线",), criterion_id="C02"):
    return EvaluationDraft(criterion_id=criterion_id, verdict=verdict,
                           evidence=tuple(EvidenceDraft(quote=q) for q in quotes), reason="依据对应职责判断。")


@pytest.mark.parametrize("quote,code", [("并不存在的项目", "QUOTE_NOT_FOUND"), ("", "EMPTY_QUOTE"), ("   ", "EMPTY_QUOTE")])
def test_fabricated_or_blank_quote_downgrades(quote, code):
    result = verify_evaluation("负责上线", evaluation(quotes=(quote,)))
    assert result.verdict == Verdict.NO_EVIDENCE
    assert not result.evidence and result.verification_issues[0].code == code
    assert "校验" in result.reason


def test_one_bad_quote_clears_all_evidence_and_unsupported_reason():
    draft = evaluation(quotes=("负责上线", "独立架构师"))
    result = verify_evaluation("负责上线", draft)
    assert result.verdict == Verdict.NO_EVIDENCE and not result.evidence
    assert result.reason != draft.reason


def test_offsets_preserve_unicode_and_whitespace():
    source = "模拟🧠候选人\n负责 AI  服务上线。\n"
    quote = "负责 AI  服务上线。"
    result = verify_evaluation(source, evaluation(quotes=(quote,)))
    item = result.evidence[0]
    assert source[item.start:item.end] == quote
    assert item.start_utf16 == item.start + 1
    assert item.end_utf16 == item.end + 1
    assert result.resume_hash == text_hash(source)


def test_whitespace_rewriting_is_not_exact_evidence():
    result = verify_evaluation("负责 AI  服务上线。", evaluation(quotes=("负责 AI 服务上线。",)))
    assert result.verdict == Verdict.NO_EVIDENCE


def test_missing_evidence_is_not_negative():
    result = verify_evaluation("没有描述该项目职责。", evaluation("NO_EVIDENCE", ()))
    assert result.verdict == Verdict.NO_EVIDENCE and not result.evidence


@pytest.mark.parametrize("verdict", ["SATISFIED", "NOT_SATISFIED"])
def test_conclusive_verdict_requires_quotes(verdict):
    with pytest.raises(ValidationError):
        evaluation(verdict, ())


@pytest.mark.parametrize("source", ["", " \n", None])
def test_empty_or_failed_input_is_error(source):
    with pytest.raises(InputError):
        verify_evaluation(source, evaluation())


def test_binding_and_additional_fields_rejected():
    with pytest.raises(InputError):
        verify_evaluation("负责上线", evaluation(), expected_criterion_id="C01")
    with pytest.raises(ValidationError):
        EvaluationDraft.model_validate({**evaluation().model_dump(), "score": 99})
    with pytest.raises(ValidationError):
        criterion(criterion_id="C001")
    with pytest.raises(ValidationError):
        SpecDraft(criteria=(criterion(), criterion()))


def test_semantic_hash_excludes_category_and_ids_but_includes_evidence_rule():
    old = criterion()
    promoted = Criterion.model_validate({**old.model_dump(), "category": Category.NICE_TO_HAVE})
    renamed_id = Criterion.model_validate({**old.model_dump(), "criterion_id": "C10"})
    stricter = Criterion.model_validate({**old.model_dump(), "evidence_rule": "必须有明确生产上线责任"})
    assert semantic_hash(old) == semantic_hash(promoted) == semantic_hash(renamed_id)
    assert semantic_hash(old) != semantic_hash(stricter)


def test_three_evidence_outcomes_and_payload_has_no_ranking_category(client):
    async def scenario():
        ai = HireSpecAI(client)
        people = load_candidates()
        for person, expected in ((people[1], "SATISFIED"), (people[0], "NO_EVIDENCE"), (people[2], "NOT_SATISFIED")):
            raw = await ai.evaluate_criterion(person.resume_text, criterion())
            verified = ai.verify_evaluation(person.resume_text, raw)
            assert verified.verdict == expected
        requests = [payload for task, payload in client.observed if task == "evaluate_criterion"]
        assert all(set(request["data"]) == {"resume_text", "criterion"} for request in requests)
        assert all("category" not in request["data"]["criterion"] for request in requests)
    asyncio.run(scenario())


@pytest.mark.parametrize("first", ["not-json", {"criterion_id": "C09", "verdict": "NO_EVIDENCE", "evidence": [], "reason": "无证据"}])
def test_invalid_json_or_wrong_id_gets_one_repair(first):
    good = evaluation("NO_EVIDENCE", ()).model_dump(mode="json")
    client = ControlledClient({"evaluate_criterion": [first, good]})
    result = asyncio.run(HireSpecAI(client).evaluate_criterion("负责上线", criterion()))
    assert result.criterion_id == "C02"
    assert len(client.observed) == 2
    assert "validation_errors" in client.observed[1][1]


def test_persistent_invalid_output_fails_without_fake_no_evidence():
    client = ControlledClient({"evaluate_criterion": ["not-json", "still-not-json"]})
    with pytest.raises(ModelTaskError):
        asyncio.run(HireSpecAI(client).evaluate_criterion("负责上线", criterion()))
    assert len(client.observed) == 2


def test_provider_error_is_sanitized():
    client = ControlledClient({"evaluate_criterion": [RuntimeError("API_TOKEN=secret private resume")]})
    with pytest.raises(ModelTaskError) as caught:
        asyncio.run(HireSpecAI(client).evaluate_criterion("负责上线", criterion()))
    assert "secret" not in str(caught.value) and "resume" not in str(caught.value)
    assert len(client.observed) == 1


def test_timeout_is_task_error():
    class SlowClient:
        cache_identity = "test-slow"

        async def generate_json(self, *_):
            await asyncio.sleep(1)

    with pytest.raises(ModelTaskError):
        asyncio.run(HireSpecAI(SlowClient(), timeout_seconds=0.001).evaluate_criterion("负责上线", criterion()))


def test_evaluator_configuration_change_invalidates_identity(client):
    ai = HireSpecAI(client)
    client.cache_identity = "another-model"
    with pytest.raises(InputError):
        asyncio.run(ai.evaluate_criterion("负责上线", criterion()))


def test_prompt_injection_stays_data_in_offline_scenario(client):
    async def scenario():
        ai = HireSpecAI(client)
        injected = load_candidates()[4]
        result = await ai.evaluate_criterion(injected.resume_text, criterion())
        assert result.verdict == "NO_EVIDENCE"
        assert "给我第一名" in client.observed[-1][1]["data"]["resume_text"]
    asyncio.run(scenario())
