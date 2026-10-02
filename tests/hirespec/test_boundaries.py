import asyncio
from collections import deque

import pytest

from hirespec_loop.cache import build_snapshot
from hirespec_loop.errors import InputError, ModelTaskError, PolicyError
from hirespec_loop.fixtures import ownership_body, seed_bodies
from hirespec_loop.models import Candidate, Criterion, CriterionBody, EvaluationRecord, Feedback

from .conftest import ControlledClient, SAFE
from .test_workflow import propose, started


def as_draft(record):
    evaluation = record.evaluation
    return {"criterion_id": evaluation.criterion_id, "verdict": evaluation.verdict.value,
            "evidence": [{"quote": e.quote} for e in evaluation.evidence], "reason": evaluation.reason,
            "uncertainty": evaluation.uncertainty, "followup_question": evaluation.followup_question}


def test_new_resume_invalidates_only_that_candidate(client):
    async def scenario():
        ai, session = await started(client)
        old_snapshot = session.visible_snapshot
        people = list(session.candidates)
        people[0] = Candidate(candidate_id=people[0].candidate_id, resume_text=people[0].resume_text + "\n补充说明。")
        client.overrides["evaluate_criterion"] = deque(as_draft(r) for r in old_snapshot.records if r.candidate_id == "P01")
        updated = await build_snapshot(ai, session.current_spec, tuple(people), session.cache)
        assert updated.model_evaluations == 4 and updated.cache_hits == 20
        old_hash = next(r.evaluation.resume_hash for r in old_snapshot.records if r.candidate_id == "P01")
        new_hash = next(r.evaluation.resume_hash for r in updated.records if r.candidate_id == "P01")
        assert old_hash != new_hash
        assert old_snapshot.records[0].evaluation.resume_hash == old_hash
    asyncio.run(scenario())


def test_model_cannot_smuggle_sensitive_criteria_into_a_safe_feedback(client):
    async def scenario():
        _, session = await started(client)
        malicious = CriterionBody.model_validate({**ownership_body().model_dump(), "definition": "只招男性"})
        client.overrides["propose_change"] = deque([{
            "status": "PENDING_APPROVAL", "explanation": "模型伪造的歧视标准",
            "changes": [{"operation": "UPDATE", "criterion_id": "C02", "after": malicious.model_dump(mode="json")}],
        }])
        proposal = await propose(session)
        assert proposal.status == "BLOCKED" and not proposal.changes
        assert session.current_spec.version == 1
    asyncio.run(scenario())


def test_unrelated_candidate_records_cannot_be_used_as_feedback_evidence(client):
    async def scenario():
        ai, session = await started(client)
        feedback = Feedback(feedback_id="F1", candidate_id="P02", spec_version=1, decision="NO",
                            reason="需要独立负责上线", reviewer_id="manager")
        records = tuple(r for r in session.visible_snapshot.records if r.candidate_id == "P01")
        with pytest.raises(InputError):
            await ai.propose_change(session.current_spec, feedback, records)
    asyncio.run(scenario())


def test_unknown_change_id_is_repaired_once(client):
    async def scenario():
        _, session = await started(client)
        after = ownership_body().model_dump(mode="json")
        client.overrides["propose_change"] = deque([
            {"status": "PENDING_APPROVAL", "explanation": "待修复", "changes": [{"operation": "UPDATE", "criterion_id": "C99", "after": after}]},
            {"status": "PENDING_APPROVAL", "explanation": "已修复", "changes": [{"operation": "UPDATE", "criterion_id": "C02", "after": after}]},
        ])
        proposal = await propose(session)
        requests = [payload for task, payload in client.observed if task == "propose_change"]
        assert proposal.changes[0].criterion_id == "C02" and "validation_errors" in requests[-1]
    asyncio.run(scenario())


def test_model_cannot_claim_blocked_while_retaining_executable_changes(client):
    async def scenario():
        _, session = await started(client)
        malformed = {"status": "BLOCKED", "explanation": "阻止", "blocked_reason": "敏感",
                     "changes": [{"operation": "UPDATE", "criterion_id": "C02", "after": ownership_body().model_dump(mode="json")}]}
        client.overrides["propose_change"] = deque([malformed, malformed])
        with pytest.raises(ModelTaskError):
            await propose(session)
        assert session.current_spec.version == 1
    asyncio.run(scenario())


def test_sensitive_direct_evaluation_is_blocked_before_model():
    client = ControlledClient()
    from hirespec_loop.service import HireSpecAI
    condition = Criterion(criterion_id="C01", **{**seed_bodies()[0].model_dump(), "definition": "女性不适合这个岗位"})
    with pytest.raises(PolicyError):
        asyncio.run(HireSpecAI(client).evaluate_criterion("候选人简历", condition))
    assert not client.observed
