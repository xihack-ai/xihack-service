import asyncio

import pytest

from hirespec_loop.cache import build_snapshot
from hirespec_loop.errors import ApprovalError, ClarificationRequired, ModelTaskError, PolicyError, StaleProposalError
from demo_fixtures import (
    OWNERSHIP_FEEDBACK, PROMOTION_FEEDBACK, UNCHANGED_FEEDBACK, VAGUE_FEEDBACK,
    load_candidates, seed_bodies,
)
from hirespec_loop.models import Category, CriterionBody, ProposedChange, SpecDraft
from hirespec_loop.service import HireSpecAI
from hirespec_loop.workflow import LoopSession

from .conftest import ControlledClient, draft_for


async def started(client):
    ai = HireSpecAI(client)
    draft = await draft_for(ai)
    session = LoopSession(ai, job_id="J1", candidates=load_candidates(), authorized_reviewers=frozenset({"manager"}))
    await session.approve_initial(draft, reviewer_id="manager")
    return ai, session


async def propose(session, reason=OWNERSHIP_FEEDBACK):
    return await session.submit_feedback(candidate_id="P01", decision="NO", reason=reason, reviewer_id="manager")


def test_complete_approval_recompute_ranking_and_historical_integrity(client):
    async def scenario():
        _, session = await started(client)
        v1_spec = session.current_spec
        v1 = session.visible_snapshot
        proposal = await propose(session)
        impact = await session.preview(proposal.proposal_id)
        assert proposal.status == "PENDING_APPROVAL" and impact.reevaluate_ids == ("C02",)
        assert session.current_spec == v1_spec and session.visible_snapshot == v1
        await session.accept(proposal.proposal_id, reviewer_id="manager")
        v2 = session.visible_snapshot
        assert session.current_spec.version == v2.spec_version == 2
        assert v1.model_evaluations == 24
        assert v2.model_evaluations == 6 and v2.cache_hits == 18
        assert [e.candidate_id for e in v2.entries][:2] == ["P04", "P02"]
        assert v1.entries[0].candidate_id == "P01"
        assert session.get_spec(1) == v1_spec and session.get_snapshot(1) == v1
        assert all(r.spec_version == 1 for r in v1.records)
        assert all(r.spec_version == 2 for r in v2.records)
        assert session.recompute_state.status == "READY"
    asyncio.run(scenario())


def test_category_only_promotion_has_zero_evaluation_calls(client):
    async def scenario():
        _, session = await started(client)
        proposal = await propose(session, PROMOTION_FEEDBACK)
        before = client.calls["evaluate_criterion"]
        await session.accept(proposal.proposal_id, reviewer_id="manager")
        assert client.calls["evaluate_criterion"] == before
        assert session.visible_snapshot.model_evaluations == 0 and session.visible_snapshot.cache_hits == 24
        assert session.current_spec.criteria[1].category == Category.MUST_HAVE
    asyncio.run(scenario())


def test_rejection_changes_neither_spec_nor_ranking(client):
    async def scenario():
        _, session = await started(client)
        old_spec, old_snapshot = session.current_spec, session.visible_snapshot
        proposal = await propose(session)
        receipt = await session.reject(proposal.proposal_id, reviewer_id="manager")
        assert receipt.action == "REJECTED"
        assert session.current_spec == old_spec and session.visible_snapshot == old_snapshot
        assert await session.reject(proposal.proposal_id, reviewer_id="manager") == receipt
        with pytest.raises(ApprovalError):
            await session.accept(proposal.proposal_id, reviewer_id="manager")
    asyncio.run(scenario())


def test_concurrent_duplicate_accept_publishes_once(client):
    async def scenario():
        _, session = await started(client)
        proposal = await propose(session)
        first, second = await asyncio.gather(
            session.accept(proposal.proposal_id, reviewer_id="manager"),
            session.accept(proposal.proposal_id, reviewer_id="manager"),
        )
        assert first == second and session.current_spec.version == 2
        assert client.calls["evaluate_criterion"] == 30
        count = len(client.observed)
        assert await session.accept(proposal.proposal_id, reviewer_id="manager") == first
        assert len(client.observed) == count
    asyncio.run(scenario())


def test_old_pending_proposal_cannot_overwrite_new_spec(client):
    async def scenario():
        _, session = await started(client)
        old = await propose(session, PROMOTION_FEEDBACK)
        latest = await propose(session)
        await session.accept(latest.proposal_id, reviewer_id="manager")
        snapshot = session.visible_snapshot
        with pytest.raises(StaleProposalError):
            await session.accept(old.proposal_id, reviewer_id="manager")
        assert session.current_spec.version == 2 and session.visible_snapshot == snapshot
    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["女性不适合这个岗位。", "年龄大了，而且没有独立上线经历。", "这个学校我不喜欢。"])
def test_blocked_feedback_cannot_be_accepted_or_edited(client, reason):
    async def scenario():
        _, session = await started(client)
        old = session.visible_snapshot
        proposal = await propose(session, reason)
        assert proposal.status == "BLOCKED" and not proposal.changes
        with pytest.raises(ApprovalError):
            await session.accept(proposal.proposal_id, reviewer_id="manager")
        with pytest.raises(ApprovalError):
            await session.edit(proposal.proposal_id, (), reviewer_id="manager")
        assert session.current_spec.version == 1 and session.visible_snapshot == old
    asyncio.run(scenario())


@pytest.mark.parametrize("reason,status", [(VAGUE_FEEDBACK, "NEEDS_CLARIFICATION"), (UNCHANGED_FEEDBACK, "NO_CHANGE")])
def test_vague_or_repeated_feedback_does_not_create_executable_changes(client, reason, status):
    async def scenario():
        _, session = await started(client)
        proposal = await propose(session, reason)
        assert proposal.status == status and not proposal.changes
        with pytest.raises(ApprovalError):
            await session.accept(proposal.proposal_id, reviewer_id="manager")
        assert session.current_spec.version == 1
    asyncio.run(scenario())


def test_edit_is_draft_only_and_sensitive_edit_fails_without_overwrite(client):
    async def scenario():
        _, session = await started(client)
        proposal = await propose(session)
        original_snapshot = session.visible_snapshot
        bad_body = CriterionBody.model_validate({**seed_bodies()[1].model_dump(), "definition": "只招男性"})
        with pytest.raises(PolicyError):
            await session.edit(proposal.proposal_id, (ProposedChange(operation="UPDATE", criterion_id="C02", after=bad_body),), reviewer_id="manager")
        assert session.get_proposal(proposal.proposal_id) == proposal
        promoted = CriterionBody.model_validate({**seed_bodies()[1].model_dump(), "category": "MUST_HAVE"})
        await session.edit(proposal.proposal_id, (ProposedChange(operation="UPDATE", criterion_id="C02", after=promoted),), reviewer_id="manager")
        assert session.current_spec.version == 1 and session.visible_snapshot == original_snapshot
        await session.accept(proposal.proposal_id, reviewer_id="manager")
        assert session.visible_snapshot.model_evaluations == 0
        assert session.current_spec.criteria[1].definition == "参与过 AI 项目"
    asyncio.run(scenario())


def test_accept_safety_transport_failure_does_not_publish(client):
    async def scenario():
        _, session = await started(client)
        proposal = await propose(session)
        from collections import deque
        client.overrides["safety_review"] = deque([TimeoutError("secret")])
        snapshot = session.visible_snapshot
        with pytest.raises(ModelTaskError):
            await session.accept(proposal.proposal_id, reviewer_id="manager")
        assert session.current_spec.version == 1 and session.visible_snapshot == snapshot
        # A retry succeeds because there was no committed approval.
        await session.accept(proposal.proposal_id, reviewer_id="manager")
        assert session.current_spec.version == 2
    asyncio.run(scenario())


def test_recompute_failure_preserves_old_snapshot_and_retry_reuses_version(client):
    async def scenario():
        _, session = await started(client)
        proposal = await propose(session)
        snapshot = session.visible_snapshot
        client.fail_evaluations = True
        with pytest.raises(ModelTaskError):
            await session.accept(proposal.proposal_id, reviewer_id="manager")
        assert session.current_spec.version == 2  # The human approval was committed.
        assert session.visible_snapshot == snapshot and session.visible_snapshot.spec_version == 1
        assert session.recompute_state.status == "FAILED" and session.recompute_state.target_version == 2
        receipt = await session.accept(proposal.proposal_id, reviewer_id="manager")
        assert receipt.result_version == 2 and session.recompute_state.status == "FAILED"
        client.fail_evaluations = False
        await session.refresh_current()
        assert session.current_spec.version == 2 and session.visible_snapshot.spec_version == 2
        assert session.visible_snapshot.model_evaluations == 6 and session.visible_snapshot.cache_hits == 18
    asyncio.run(scenario())


def test_initial_approval_requires_confirmation_and_authorization(client):
    async def scenario():
        ai = HireSpecAI(client)
        draft = await draft_for(ai)
        session = LoopSession(ai, job_id="J1", candidates=load_candidates(), authorized_reviewers=frozenset({"manager"}))
        with pytest.raises(ApprovalError):
            await session.approve_initial(draft, reviewer_id="outsider")
        unresolved = SpecDraft(criteria=draft.criteria, pending_assumptions=("是否需要带团队？",))
        with pytest.raises(ClarificationRequired):
            await session.approve_initial(unresolved, reviewer_id="manager")
        assert session.visible_snapshot is None
        await session.approve_initial(draft, reviewer_id="manager")
        with pytest.raises(ApprovalError):
            await session.approve_initial(draft, reviewer_id="manager")
    asyncio.run(scenario())


def test_cache_is_partitioned_by_job_and_model(client):
    async def scenario():
        ai, session = await started(client)
        original = session.current_spec
        another = type(original).model_validate({**original.model_dump(), "job_id": "J2"})
        snapshot = await build_snapshot(ai, another, session.candidates, session.cache)
        assert snapshot.model_evaluations == 24 and snapshot.cache_hits == 0
        new_client = ControlledClient()
        new_client.cache_identity = "offline-fixtures-v2-different-model-settings"
        new_ai = HireSpecAI(new_client)
        snapshot = await build_snapshot(new_ai, original, session.candidates, session.cache)
        assert snapshot.model_evaluations == 24 and snapshot.cache_hits == 0
    asyncio.run(scenario())
