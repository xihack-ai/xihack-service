"""Single-process reference backend. Durable DB/auth/LangGraph belong to the captain.

The lock serializes approval/publication. Snapshots switch only when fully evaluated.
Approved spec versions survive recompute failure; refresh_current retries computation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from .cache import EvaluationCache, build_snapshot
from .errors import ApprovalError, ClarificationRequired, HireSpecError, InputError
from .models import (
    ApprovalReceipt, Candidate, ChangeImpact, ChangeProposal, Feedback, ProposalStatus,
    ProposedChange, RankingSnapshot, RecomputeState, SpecDraft, SpecVersion,
)
from .service import HireSpecAI


class LoopSession:
    def __init__(
        self, ai: HireSpecAI, *, job_id: str, candidates: tuple[Candidate, ...],
        authorized_reviewers: frozenset[str], cache: EvaluationCache | None = None,
        concurrency: int = 4,
    ) -> None:
        if not job_id.strip() or not candidates or len({c.candidate_id for c in candidates}) != len(candidates):
            raise InputError("职位 ID 和候选人列表必须有效且候选人 ID 不可重复。")
        if not authorized_reviewers or any(not r.strip() for r in authorized_reviewers):
            raise InputError("必须指定可审批的用户。")
        self.ai = ai
        self.job_id = job_id
        self.candidates = tuple(candidates)
        self.authorized_reviewers = frozenset(authorized_reviewers)
        self.cache = cache if cache is not None else EvaluationCache()
        self.concurrency = concurrency
        self._versions: dict[int, SpecVersion] = {}
        self._proposals: dict[str, ChangeProposal] = {}
        self._feedback: dict[str, Feedback] = {}
        self._decisions: dict[str, ApprovalReceipt] = {}
        self._snapshots: dict[int, RankingSnapshot] = {}
        self._snapshot: RankingSnapshot | None = None
        self._state = RecomputeState()
        self._lock = asyncio.Lock()

    @property
    def current_spec(self) -> SpecVersion:
        if not self._versions:
            raise InputError("请先确认并发布初始标准。")
        return self._versions[max(self._versions)]

    @property
    def visible_snapshot(self) -> RankingSnapshot | None:
        return self._snapshot

    @property
    def recompute_state(self) -> RecomputeState:
        return self._state

    def get_spec(self, version: int) -> SpecVersion:
        return self._versions[version]

    def get_snapshot(self, version: int) -> RankingSnapshot:
        return self._snapshots[version]

    def get_proposal(self, proposal_id: str) -> ChangeProposal:
        try:
            return self._proposals[proposal_id]
        except KeyError:
            raise InputError("未找到提议。") from None

    def _authorize(self, reviewer_id: str) -> None:
        if reviewer_id not in self.authorized_reviewers:
            raise ApprovalError("当前用户没有此职位的审批权限。")

    def _pending_proposal(self, proposal_id: str) -> ChangeProposal:
        proposal = self.get_proposal(proposal_id)
        if proposal.status != ProposalStatus.PENDING_APPROVAL or proposal_id in self._decisions:
            raise ApprovalError("仅待审批且未决的提议可以接受、编辑或拒绝。")
        return proposal

    async def approve_initial(self, draft: SpecDraft, *, reviewer_id: str) -> RankingSnapshot:
        self._authorize(reviewer_id)
        async with self._lock:
            if self._versions:
                raise ApprovalError("初始标准已发布，请通过变更提议修改。")
            if draft.pending_assumptions:
                raise ClarificationRequired("仍有待确认假设，请完成澄清后再批准初始标准。")
            await self.ai.validate_spec(draft)
            version = SpecVersion(
                job_id=self.job_id, version=1, criteria=draft.criteria,
                created_at=datetime.now(timezone.utc), approved_by=reviewer_id,
                next_criterion_number=max(int(c.criterion_id[1:]) for c in draft.criteria) + 1,
            )
            self._versions[1] = version
            return await self._refresh_locked()

    async def submit_feedback(
        self, *, candidate_id: str, decision: str, reason: str, reviewer_id: str,
    ) -> ChangeProposal:
        self._authorize(reviewer_id)
        async with self._lock:
            spec = self.current_spec
            if self._snapshot is None or self._snapshot.spec_version != spec.version:
                raise InputError("当前版本尚未完成评估，请完成重算后再提交反馈。")
            if candidate_id not in {c.candidate_id for c in self.candidates}:
                raise InputError("候选人不存在于当前职位。")
            feedback = Feedback(
                feedback_id=str(uuid4()), candidate_id=candidate_id, spec_version=spec.version,
                decision=decision, reason=reason, reviewer_id=reviewer_id,
            )
            # Preserve feedback even when the model task fails; caller can retry separately.
            self._feedback[feedback.feedback_id] = feedback
            records = tuple(r for r in self._snapshot.records if r.candidate_id == candidate_id)
            proposal = await self.ai.propose_change(spec, feedback, records)
            self._proposals[proposal.proposal_id] = proposal
            return proposal

    async def preview(self, proposal_id: str) -> ChangeImpact:
        async with self._lock:
            proposal = self._pending_proposal(proposal_id)
            return await self.ai.validate_and_diff(
                self.current_spec, proposal.changes, base_spec_version=proposal.base_spec_version,
            )

    async def edit(
        self, proposal_id: str, changes: tuple[ProposedChange, ...], *, reviewer_id: str,
    ) -> ChangeProposal:
        self._authorize(reviewer_id)
        async with self._lock:
            proposal = self._pending_proposal(proposal_id)
            await self.ai.validate_and_diff(
                self.current_spec, changes, base_spec_version=proposal.base_spec_version,
            )
            edited = ChangeProposal.model_validate({
                **proposal.model_dump(), "changes": changes,
                "explanation": "经理编辑了提议草稿；尚未批准，标准和排名未改变。",
            })
            self._proposals[proposal_id] = edited
            return edited

    async def reject(self, proposal_id: str, *, reviewer_id: str) -> ApprovalReceipt:
        self._authorize(reviewer_id)
        async with self._lock:
            existing = self._decisions.get(proposal_id)
            if existing:
                if existing.action != "REJECTED":
                    raise ApprovalError("已接受的提议不能再拒绝。")
                return existing
            self._pending_proposal(proposal_id)
            receipt = ApprovalReceipt(
                proposal_id=proposal_id, action="REJECTED", reviewer_id=reviewer_id,
                result_version=self.current_spec.version,
            )
            self._decisions[proposal_id] = receipt
            return receipt

    async def accept(self, proposal_id: str, *, reviewer_id: str) -> ApprovalReceipt:
        self._authorize(reviewer_id)
        async with self._lock:
            existing = self._decisions.get(proposal_id)
            if existing:
                if existing.action != "ACCEPTED":
                    raise ApprovalError("已拒绝的提议不能接受。")
                return existing  # Check idempotency before staleness, no model calls.
            proposal = self._pending_proposal(proposal_id)
            current = self.current_spec
            impact = await self.ai.validate_and_diff(
                current, proposal.changes, base_spec_version=proposal.base_spec_version,
            )
            version = SpecVersion(
                job_id=self.job_id, version=current.version + 1, criteria=impact.criteria,
                created_at=datetime.now(timezone.utc), approved_by=reviewer_id,
                next_criterion_number=impact.next_criterion_number,
            )
            receipt = ApprovalReceipt(
                proposal_id=proposal_id, action="ACCEPTED", reviewer_id=reviewer_id,
                result_version=version.version,
            )
            self._versions[version.version] = version
            self._decisions[proposal_id] = receipt
            await self._refresh_locked()
            return receipt

    async def refresh_current(self) -> RankingSnapshot:
        async with self._lock:
            return await self._refresh_locked()

    async def _refresh_locked(self) -> RankingSnapshot:
        spec = self.current_spec
        self._state = RecomputeState(
            status="RUNNING", target_version=spec.version,
            visible_version=self._snapshot.spec_version if self._snapshot else None,
        )
        try:
            snapshot = await build_snapshot(
                self.ai, spec, self.candidates, self.cache, concurrency=self.concurrency,
            )
        except BaseException as exc:
            self._state = RecomputeState(
                status="FAILED", target_version=spec.version,
                visible_version=self._snapshot.spec_version if self._snapshot else None,
                error_code=exc.code if isinstance(exc, HireSpecError) else "RECOMPUTE_FAILED",
            )
            raise
        self._snapshots[spec.version] = snapshot
        self._snapshot = snapshot
        self._state = RecomputeState(status="READY", target_version=spec.version, visible_version=spec.version)
        return snapshot
