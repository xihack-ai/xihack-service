"""Durable approval workflow. AI calls happen outside publication transactions."""

from app.recruiting_store import RecruitingStore, wire
from hirespec_loop.cache import build_snapshot
from hirespec_loop.errors import ClarificationRequired, HireSpecError, InputError
from hirespec_loop.models import (
    ApprovalReceipt, Candidate, ChangeProposal, ProposalStatus, SpecDraft,
)
from hirespec_loop.service import HireSpecAI


class PersistentLoop:
    def __init__(self, store: RecruitingStore, ai: HireSpecAI, *, model_mode: str):
        self.store, self.ai, self.model_mode = store, ai, model_mode

    def create_job(self, title, requirement, actor):
        return self.store.create_job(title, requirement, actor, self.model_mode)

    async def clarify(self, job_id, actor, answers):
        view = self.store.get_job(job_id, actor)
        result = await self.ai.clarify_requirement(view["job"]["requirement"], answers)
        with self.store.write_job(job_id, actor) as (conn, job):
            self.store.audit(conn, job_id, actor, "CLARIFICATION", {"answers": answers, "result": result})
        return result

    async def draft(self, job_id, actor, answers):
        view = self.store.get_job(job_id, actor)
        draft = await self.ai.draft_spec(view["job"]["requirement"], answers)
        return self.store.save_draft(job_id, actor, draft, answers)

    async def manual_draft(self, job_id, actor, draft):
        await self.ai.validate_spec(draft)
        return self.store.save_draft(job_id, actor, draft, {})

    async def approve_initial(self, job_id, actor, draft_id):
        with self.store.connect(read_only=True) as conn:
            self.store.owned_job(conn, job_id, actor)
            draft = self.store.load_draft(conn, job_id, draft_id)
        if draft.pending_assumptions:
            raise ClarificationRequired("初始标准仍有待确认假设，请先完成澄清或人工编辑。")
        await self.ai.validate_spec(draft)
        spec = self.store.initial(job_id, actor, draft_id, draft)
        with self.store.connect(read_only=True) as conn:
            self.store.owned_job(conn, job_id, actor)
            has_candidates = bool(self.store.candidates(conn, job_id))
        if not has_candidates:
            return {"spec": wire(spec), "snapshot": None, "requires_candidates": True,
                    "model_mode": self.model_mode}
        snapshot = await self.recompute(job_id, actor)
        return {"spec": wire(spec), "snapshot": wire(snapshot), "model_mode": self.model_mode}

    async def recompute(self, job_id, actor):
        # A session lock survives the short publication transactions, preventing
        # concurrent candidate uploads, approval or competing recompute workers.
        with self.store.lease(job_id, actor) as lease:
            spec = self.store.spec(lease, job_id)
            rows = self.store.candidates(lease, job_id)
            if not rows:
                raise InputError("请先导入至少一份文本简历。")
            candidates = tuple(Candidate(candidate_id=str(r["id"]), resume_text=r["resume_text"]) for r in rows)
            cache = self.store.load_cache(lease, job_id)
            with self.store.write_job(job_id, actor, lease=lease) as (conn, job):
                self.store.set_recompute(conn, job_id, actor, "RUNNING", spec.version)
            try:
                snapshot = await build_snapshot(self.ai, spec, candidates, cache, concurrency=4)
                with self.store.write_job(job_id, actor, lease=lease) as (conn, job):
                    if job["current_spec_version"] != spec.version:
                        raise InputError("评估期间标准版本改变，未发布混合结果。")
                    current_rows = self.store.candidates(conn, job_id)
                    if {(str(r["id"]), r["resume_hash"]) for r in rows} != {(str(r["id"]), r["resume_hash"]) for r in current_rows}:
                        raise InputError("评估期间候选人列表改变，未发布混合结果。")
                    if cache.new:
                        self.store.audit(conn, job_id, actor, "EVALUATION_CACHE_BATCH", {"values": cache.new})
                    self.store.publish_snapshot(conn, actor, snapshot)
            except BaseException as exc:
                with self.store.write_job(job_id, actor, lease=lease) as (conn, job):
                    if cache.new:
                        self.store.audit(conn, job_id, actor, "EVALUATION_CACHE_BATCH", {"values": cache.new})
                    self.store.set_recompute(conn, job_id, actor, "FAILED", spec.version,
                                             exc.code if isinstance(exc, HireSpecError) else "RECOMPUTE_FAILED")
                raise
            return snapshot

    async def feedback(self, job_id, actor, candidate_id, decision, reason, spec_version):
        feedback = self.store.save_feedback(job_id, actor, candidate_id, decision, reason, spec_version)
        try:
            proposal = await self.propose(job_id, actor, feedback.feedback_id)
        except HireSpecError as exc:
            # Durable feedback remains retryable. The API can return this ID without
            # echoing the resume/reason or provider error.
            exc.feedback_id = feedback.feedback_id
            raise
        return {"feedback": wire(feedback), **proposal}

    async def propose(self, job_id, actor, feedback_id):
        feedback, spec, records, existing = self.store.feedback_context(job_id, actor, feedback_id)
        if existing:
            return self.store.proposal_view(existing)
        proposal = await self.ai.propose_change(spec, feedback, records)
        impact = None
        if proposal.status == ProposalStatus.PENDING_APPROVAL:
            impact = await self.ai.validate_and_diff(spec, proposal.changes, base_spec_version=proposal.base_spec_version)
        return self.store.save_proposal(job_id, actor, proposal, impact)

    async def edit(self, job_id, actor, proposal_id, changes):
        row, current = self.store.proposal_context(job_id, actor, proposal_id)
        original = self.store.pending(row, current)
        impact = await self.ai.validate_and_diff(current, changes, base_spec_version=original.base_spec_version)
        edited = ChangeProposal.model_validate({
            **original.model_dump(), "changes": changes,
            "explanation": "人工编辑后的草稿；尚未批准，标准和排名未改变。",
        })
        return self.store.edit_proposal(job_id, actor, proposal_id, original, edited, impact)

    async def accept(self, job_id, actor, proposal_id):
        row, current = self.store.proposal_context(job_id, actor, proposal_id)
        if row["status"] == "APPROVED":
            return {"receipt": row["final_proposal"]["receipt"], "already_decided": True}
        original = self.store.pending(row, current)
        impact = await self.ai.validate_and_diff(current, original.changes, base_spec_version=original.base_spec_version)
        receipt, created = self.store.accept(job_id, actor, proposal_id, original, impact)
        if created:
            # Approval is already committed. Failed recompute retains old complete
            # visible ranking and can be retried via POST /recompute.
            snapshot = await self.recompute(job_id, actor)
            return {"receipt": wire(receipt), "already_decided": False, "snapshot": wire(snapshot)}
        return {"receipt": wire(receipt), "already_decided": True}
