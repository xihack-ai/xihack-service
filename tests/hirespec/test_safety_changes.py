import asyncio
from datetime import datetime, timezone

import pytest

from hirespec_loop.changes import preview_changes
from hirespec_loop.errors import ClarificationRequired, InputError, ModelTaskError, PolicyError, StaleProposalError
from demo_fixtures import seed_bodies
from hirespec_loop.models import Category, Criterion, CriterionBody, ProposedChange, SpecDraft, SpecVersion
from hirespec_loop.safety import rule_review
from hirespec_loop.service import HireSpecAI

from .conftest import ControlledClient, SAFE


def spec():
    return SpecVersion(job_id="J1", version=1, created_at=datetime.now(timezone.utc), approved_by="manager",
                       criteria=tuple(Criterion(criterion_id=f"C{i:02d}", **c.model_dump()) for i, c in enumerate(seed_bodies(), 1)),
                       next_criterion_number=5)


def body(**updates):
    return CriterionBody.model_validate({**seed_bodies()[1].model_dump(), **updates})


@pytest.mark.parametrize("text", [
    "这个候选人年龄有点大。", "年龄大了。", "年龄小于35岁。", "只要30岁以下。",
    "女性不适合这个岗位。", "只招男性。", "只招未婚。",
    "他准备生育了，不合适。", "优先未婚。", "只招汉族。",
    "年龄大了，而且没有独立上线经历。", "Women are not suitable for this role.",
    "Only hire men", "age under 35",
])
def test_explicit_sensitive_conditions_blocked(text):
    assert rule_review((text,)).status == "BLOCKED"


@pytest.mark.parametrize("text", ["开发面向女性用户的产品", "给老年用户开发服务", "管理三人团队", "负责生产级 AI 服务上线"])
def test_product_audience_terms_not_blocked_by_keyword(text):
    assert rule_review((text,)).status == "SAFE"


def test_category_only_preview_reuses_all_evaluations():
    current = spec()
    change = ProposedChange(operation="UPDATE", criterion_id="C02", after=body(category="MUST_HAVE"))
    impact = preview_changes(current, (change,))
    assert impact.reevaluate_ids == () and len(impact.reuse_ids) == 4
    assert not impact.diff[0].evaluation_changed
    assert current.criteria[1].category == "NICE_TO_HAVE"
    assert impact.diff[0].before == current.criteria[1]


def test_add_remove_ids_are_program_allocated_and_never_reused():
    current = spec()
    added = body(definition="可靠性复盘经验")
    impact = preview_changes(current, (
        ProposedChange(operation="REMOVE", criterion_id="C04"),
        ProposedChange(operation="ADD", after=added),
    ))
    assert impact.removed_ids == ("C04",) and impact.reevaluate_ids == ("C05",)
    updated = SpecVersion.model_validate({**current.model_dump(), "version": 2,
                                         "criteria": impact.criteria, "next_criterion_number": impact.next_criterion_number})
    second = preview_changes(updated, (ProposedChange(operation="ADD", after=body(definition="事件处理经验")),))
    assert second.reevaluate_ids == ("C06",)


@pytest.mark.parametrize("changes", [
    (ProposedChange(operation="REMOVE", criterion_id="C99"),),
    (ProposedChange(operation="REMOVE", criterion_id="C02"), ProposedChange(operation="REMOVE", criterion_id="C02")),
    (ProposedChange(operation="UPDATE", criterion_id="C02", after=body()),),
    (ProposedChange(operation="ADD", after=body()),),
    tuple(ProposedChange(operation="REMOVE", criterion_id=f"C{i:02d}") for i in range(1, 5)),
])
def test_invalid_or_duplicate_change_shapes_rejected(changes):
    with pytest.raises(InputError):
        preview_changes(spec(), changes)


def test_human_sensitive_edit_rechecked_before_commit():
    ai = HireSpecAI(ControlledClient())
    change = ProposedChange(operation="UPDATE", criterion_id="C02", after=body(definition="只招男性"))
    with pytest.raises(PolicyError):
        asyncio.run(ai.validate_and_diff(spec(), (change,), base_spec_version=1))


def test_semantic_review_catches_condition_not_covered_by_rules():
    client = ControlledClient({"safety_review": [{
        "status": "BLOCKED", "findings": [{"code": "NON_JOB_RELATED", "explanation": "按个人出身排除候选人。"}],
    }]})
    ai = HireSpecAI(client)
    change = ProposedChange(operation="UPDATE", criterion_id="C02", after=body(definition="排除某种家庭背景的人"))
    with pytest.raises(PolicyError):
        asyncio.run(ai.validate_and_diff(spec(), (change,), base_spec_version=1))
    assert client.observed[0][0] == "safety_review"


def test_unknown_safety_or_model_failure_does_not_fail_open():
    change = ProposedChange(operation="UPDATE", criterion_id="C02", after=body(category="MUST_HAVE"))
    client = ControlledClient({"safety_review": [{
        "status": "NEEDS_CLARIFICATION", "findings": [{"code": "NOT_EVALUABLE", "explanation": "门槛不明确。"}],
        "clarification_question": "什么项目算生产级？",
    }]})
    with pytest.raises(ClarificationRequired):
        asyncio.run(HireSpecAI(client).validate_and_diff(spec(), (change,), base_spec_version=1))
    failing = ControlledClient({"safety_review": [TimeoutError("secret")]})
    with pytest.raises(ModelTaskError):
        asyncio.run(HireSpecAI(failing).validate_and_diff(spec(), (change,), base_spec_version=1))


def test_stale_proposal_rejected_before_model_call(client):
    change = ProposedChange(operation="UPDATE", criterion_id="C02", after=body(category="MUST_HAVE"))
    with pytest.raises(StaleProposalError):
        asyncio.run(HireSpecAI(client).validate_and_diff(spec(), (change,), base_spec_version=0))
    assert not client.observed


def test_initial_draft_sensitive_conditions_are_also_blocked():
    ai = HireSpecAI(ControlledClient())
    draft = SpecDraft(criteria=(Criterion(criterion_id="C01", **body(definition="年龄小于35岁").model_dump()),))
    with pytest.raises(PolicyError):
        asyncio.run(ai.validate_spec(draft))


def test_benign_demographic_context_requires_and_can_pass_semantic_review():
    client = ControlledClient({"safety_review": [SAFE]})
    change = ProposedChange(operation="UPDATE", criterion_id="C02", after=body(definition="开发面向女性用户的产品"))
    impact = asyncio.run(HireSpecAI(client).validate_and_diff(spec(), (change,), base_spec_version=1))
    assert impact.reevaluate_ids == ("C02",)
    assert client.observed[0][0] == "safety_review"
