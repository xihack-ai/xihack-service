"""Existing eight-table PostgreSQL contract. No migrations or shared-job mutations.

criteria stays an array. Extra workflow/cache metadata is namespaced in audit_events.
Every write locks the job, verifies its owner and uses parameterized SQL.
"""

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.database import DatabaseSettings
from hirespec_loop.cache import CacheKey, EvaluationCache
from hirespec_loop.errors import ApprovalError, InputError, StaleProposalError
from hirespec_loop.evaluation import text_hash
from hirespec_loop.models import (
    ApprovalReceipt, Candidate, ChangeImpact, ChangeProposal, EvaluationRecord,
    Feedback, ProposalStatus, RankingSnapshot, SpecDraft, SpecVersion, VerifiedEvaluation,
)

CONTRACT = "hirespec-pg-v1"
PREFIX = "HIRESPEC_"
PROPOSAL_STATUS = {
    ProposalStatus.PENDING_APPROVAL: "PENDING", ProposalStatus.BLOCKED: "BLOCKED",
    ProposalStatus.NO_CHANGE: "EMPTY", ProposalStatus.NEEDS_CLARIFICATION: "EMPTY",
}
TIER_VERDICT = {
    "QUALIFIED": "推荐", "NEEDS_VERIFICATION": "证据不足",
    "REQUIREMENT_CONFLICT": "待定", "VETO_TRIGGERED": "待定",
}


class StoreError(InputError):
    code = "STORE_CONFLICT"


class NotFound(InputError):
    code = "NOT_FOUND"


class JobBusy(StoreError):
    code = "JOB_BUSY"


def uuid_text(value) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise InputError("职位、候选人、反馈和提议 ID 必须是 UUID。") from None


def wire(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: wire(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire(v) for v in value]
    if isinstance(value, (datetime, UUID)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    return value


class DurableCache(EvaluationCache):
    def __init__(self):
        super().__init__()
        self.new = []

    def put(self, key, value):
        super().put(key, value)
        self.new.append({"key": asdict(key), "evaluation": wire(value)})


class RecruitingStore:
    def __init__(self, settings: DatabaseSettings | None = None):
        self.settings = settings

    def connect(self, *, read_only=False, autocommit=False):
        config = self.settings or DatabaseSettings.from_env()
        return psycopg.connect(
            host=config.host, port=config.port, dbname=config.database,
            user=config.user, password=config.password, connect_timeout=config.connect_timeout,
            application_name="hirespec-persistence", row_factory=dict_row,
            autocommit=autocommit,
            options=f"-c default_transaction_read_only={'on' if read_only else 'off'} -c statement_timeout=8000 -c lock_timeout=3000",
        )

    @staticmethod
    def audit(conn, job_id, actor, event, payload):
        conn.execute(
            "INSERT INTO audit_events (job_id,actor,event_type,payload) VALUES (%s,%s,%s,%s)",
            (job_id, actor, PREFIX + event, Jsonb({"contract": CONTRACT, **wire(payload)})),
        )

    @staticmethod
    def event(conn, job_id, name):
        return conn.execute(
            "SELECT payload FROM audit_events WHERE job_id=%s AND event_type=%s "
            "AND payload->>'contract'=%s ORDER BY id DESC LIMIT 1",
            (job_id, PREFIX + name, CONTRACT),
        ).fetchone()

    def owned_job(self, conn, job_id, actor, *, lock=False):
        row = conn.execute(
            "SELECT * FROM jobs WHERE id=%s" + (" FOR UPDATE" if lock else ""), (job_id,)
        ).fetchone()
        marker = self.event(conn, job_id, "JOB_CREATED") if row else None
        if not row or not marker or marker["payload"].get("owner") != actor:
            # Deliberately does not disclose other jobs or accept ownership from a request body.
            raise NotFound("未找到当前账号创建的 HireSpec 职位。")
        return row

    @staticmethod
    def try_lock(conn, job_id, *, session=False):
        function = "pg_try_advisory_lock" if session else "pg_try_advisory_xact_lock"
        result = conn.execute(
            f"SELECT {function}(hashtextextended(%s,0)) AS acquired",
            ("hirespec-v1:" + str(job_id),),
        ).fetchone()
        if not result["acquired"]:
            raise JobBusy("该职位正在处理另一项操作，请稍后重试。")

    @contextmanager
    def write_job(self, job_id, actor, *, lease=None):
        job_id = uuid_text(job_id)
        if lease is not None:
            # The caller owns a session advisory lock on this exact job.
            with lease.transaction():
                job = self.owned_job(lease, job_id, actor, lock=True)
                yield lease, job
        else:
            with self.connect() as conn:
                self.try_lock(conn, job_id)
                job = self.owned_job(conn, job_id, actor, lock=True)
                yield conn, job

    @contextmanager
    def lease(self, job_id, actor):
        job_id = uuid_text(job_id)
        with self.connect(autocommit=True) as conn:
            self.owned_job(conn, job_id, actor)
            self.try_lock(conn, job_id, session=True)
            try:
                yield conn
            finally:
                conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", ("hirespec-v1:" + job_id,))

    def create_job(self, title, requirement, actor, mode):
        job_id = str(uuid4())
        with self.connect() as conn:
            row = conn.execute(
                "INSERT INTO jobs(id,title,requirement,status) VALUES (%s,%s,%s,'RUNNING') RETURNING *",
                (job_id, title, requirement),
            ).fetchone()
            self.audit(conn, job_id, actor, "JOB_CREATED", {"owner": actor, "model_mode": mode})
        return wire(row)

    def save_draft(self, job_id, actor, draft, answers):
        draft_id = str(uuid4())
        with self.write_job(job_id, actor) as (conn, job):
            if job["current_spec_version"]:
                raise ApprovalError("初始标准已发布，请使用反馈提议修改。")
            self.audit(conn, job_id, actor, "DRAFT_SAVED", {
                "draft_id": draft_id, "draft": draft, "answers": answers,
            })
        return {"draft_id": draft_id, "draft": wire(draft), "published": False}

    def load_draft(self, conn, job_id, draft_id):
        row = conn.execute(
            "SELECT payload FROM audit_events WHERE job_id=%s AND event_type=%s "
            "AND payload->>'draft_id'=%s AND payload->>'contract'=%s ORDER BY id DESC LIMIT 1",
            (job_id, PREFIX + "DRAFT_SAVED", uuid_text(draft_id), CONTRACT),
        ).fetchone()
        if not row:
            raise NotFound("未找到标准草稿。")
        return SpecDraft.model_validate(row["payload"]["draft"])

    def spec(self, conn, job_id, version=None):
        if version is None:
            version = conn.execute("SELECT current_spec_version FROM jobs WHERE id=%s", (job_id,)).fetchone()["current_spec_version"]
        row = conn.execute(
            "SELECT payload FROM audit_events WHERE job_id=%s AND event_type=%s "
            "AND (payload->>'version')::int=%s AND payload->>'contract'=%s ORDER BY id DESC LIMIT 1",
            (job_id, PREFIX + "SPEC_PUBLISHED", version, CONTRACT),
        ).fetchone()
        if not row:
            raise InputError("当前职位尚未发布 Hiring Spec。")
        value = SpecVersion.model_validate(row["payload"]["spec"])
        saved = conn.execute("SELECT criteria FROM spec_versions WHERE job_id=%s AND version=%s", (job_id, version)).fetchone()
        if not saved or saved["criteria"] != wire(value.criteria):
            raise StoreError("标准记录与发布日志不一致。")
        return value

    def initial(self, job_id, actor, draft_id, draft):
        with self.write_job(job_id, actor) as (conn, job):
            if job["current_spec_version"]:
                event = self.event(conn, job_id, "SPEC_PUBLISHED")
                if job["current_spec_version"] == 1 and event["payload"].get("draft_id") == draft_id:
                    return self.spec(conn, job_id)
                raise ApprovalError("初始标准已经发布。")
            if self.load_draft(conn, job_id, draft_id) != draft:
                raise StoreError("草稿已变化，请重新检查后批准。")
            version = SpecVersion(
                job_id=job_id, version=1, criteria=draft.criteria,
                created_at=datetime.now(timezone.utc), approved_by=actor,
                next_criterion_number=max(int(c.criterion_id[1:]) for c in draft.criteria)+1,
            )
            self.insert_spec(conn, actor, version, {"draft_id": draft_id})
            return version

    def insert_spec(self, conn, actor, spec, extra, proposal_id=None):
        conn.execute(
            "INSERT INTO spec_versions(job_id,version,criteria,source_proposal_id,created_at) VALUES (%s,%s,%s,%s,%s)",
            (spec.job_id, spec.version, Jsonb(wire(spec.criteria)), proposal_id, spec.created_at),
        )
        self.audit(conn, spec.job_id, actor, "SPEC_PUBLISHED", {"version": spec.version, "spec": spec, **extra})
        conn.execute(
            "UPDATE jobs SET current_spec_version=%s,status='RUNNING',updated_at=now() WHERE id=%s",
            (spec.version, spec.job_id),
        )

    def candidates(self, conn, job_id):
        rows = conn.execute("SELECT * FROM candidates WHERE job_id=%s ORDER BY id", (job_id,)).fetchall()
        for row in rows:
            if text_hash(row["resume_text"]) != row["resume_hash"]:
                raise StoreError("简历内容哈希不一致，不能复用评估。")
        return rows

    def add_candidate(self, job_id, actor, name, resume_text, source_file=None):
        digest = text_hash(resume_text)
        with self.write_job(job_id, actor) as (conn, job):
            existing = conn.execute("SELECT * FROM candidates WHERE job_id=%s AND resume_hash=%s", (job_id, digest)).fetchone()
            if existing:
                return {**wire(existing), "reused": True}
            row = conn.execute(
                "INSERT INTO candidates(job_id,name,resume_text,resume_hash,source_file) VALUES (%s,%s,%s,%s,%s) RETURNING *",
                (job_id, name, resume_text, digest, source_file),
            ).fetchone()
            self.audit(conn, job_id, actor, "CANDIDATE_ADDED", {"candidate_id": str(row["id"]), "resume_hash": digest})
            conn.execute("UPDATE jobs SET status='RUNNING',updated_at=now() WHERE id=%s", (job_id,))
            return {**wire(row), "reused": False}

    def snapshot(self, conn, job_id, version=None):
        if version is None:
            event = self.event(conn, job_id, "RANKING_PUBLISHED")
        else:
            event = conn.execute(
                "SELECT payload FROM audit_events WHERE job_id=%s AND event_type=%s "
                "AND (payload->>'spec_version')::int=%s AND payload->>'contract'=%s ORDER BY id DESC LIMIT 1",
                (job_id, PREFIX + "RANKING_PUBLISHED", version, CONTRACT),
            ).fetchone()
        if not event:
            return None
        value = event["payload"]["snapshot"]
        rows = conn.execute(
            "SELECT scores FROM evaluations WHERE job_id=%s AND spec_version=%s ORDER BY candidate_id",
            (job_id, value["spec_version"]),
        ).fetchall()
        records = []
        for row in rows:
            if row["scores"].get("contract") != CONTRACT:
                raise StoreError("评估记录格式不兼容。")
            records.extend(row["scores"]["records"])
        result = RankingSnapshot.model_validate({**value, "records": records})
        if {r.candidate_id for r in result.records} != {e.candidate_id for e in result.entries}:
            raise StoreError("排名与评估候选人集合不一致。")
        spec = self.spec(conn, job_id, result.spec_version)
        if len(result.records) != len(result.entries)*len(spec.criteria):
            raise StoreError("排名的评估矩阵不完整。")
        return result

    def get_job(self, job_id, actor):
        job_id = uuid_text(job_id)
        with self.connect(read_only=True) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            job = self.owned_job(conn, job_id, actor)
            spec = self.spec(conn, job_id) if job["current_spec_version"] else None
            snapshot = self.snapshot(conn, job_id)
            draft = self.event(conn, job_id, "DRAFT_SAVED")
            runtime = self.event(conn, job_id, "RECOMPUTE_STATE")
            candidates = self.candidates(conn, job_id)
            proposals = conn.execute("SELECT id,status,proposal,final_proposal FROM change_proposals WHERE job_id=%s ORDER BY created_at,id", (job_id,)).fetchall()
            evaluated_ids = {entry.candidate_id for entry in snapshot.entries} if snapshot else set()
            pending_candidates = [str(c["id"]) for c in candidates if str(c["id"]) not in evaluated_ids]
        return wire({
            "job": job, "spec": spec, "visible_snapshot": snapshot,
            "visible_spec_version": snapshot.spec_version if snapshot else None,
            "draft": draft["payload"] if draft else None,
            "recompute": runtime["payload"] if runtime else {"status": "IDLE"},
            "candidates": candidates, "proposals": [self.proposal_view(row) for row in proposals],
            "pending_candidate_ids": pending_candidates,
            "snapshot_stale": not snapshot or snapshot.spec_version != job["current_spec_version"] or bool(pending_candidates),
        })

    def load_cache(self, conn, job_id):
        cache = DurableCache()
        rows = conn.execute(
            "SELECT payload FROM audit_events WHERE job_id=%s AND event_type=%s "
            "AND payload->>'contract'=%s ORDER BY id",
            (job_id, PREFIX+"EVALUATION_CACHE_BATCH", CONTRACT),
        ).fetchall()
        for row in rows:
            for item in row["payload"]["values"]:
                key = CacheKey(**item["key"])
                if key.job_id != job_id:
                    raise StoreError("评估缓存跨职位。")
                EvaluationCache.put(cache, key, VerifiedEvaluation.model_validate(item["evaluation"]))
        return cache

    def set_recompute(self, conn, job_id, actor, status, target, error_code=None):
        visible = self.snapshot(conn, job_id)
        self.audit(conn, job_id, actor, "RECOMPUTE_STATE", {
            "status": status, "target_version": target,
            "visible_version": visible.spec_version if visible else None, "error_code": error_code,
        })
        db_status = "FAILED" if status == "FAILED" else "RUNNING"
        if status == "READY":
            pending = conn.execute("SELECT 1 FROM change_proposals WHERE job_id=%s AND status='PENDING'", (job_id,)).fetchone()
            db_status = "AWAITING_APPROVAL" if pending else "EVALUATED"
        conn.execute("UPDATE jobs SET status=%s,updated_at=now() WHERE id=%s", (db_status, job_id))

    def publish_snapshot(self, conn, actor, snapshot):
        for entry in snapshot.entries:
            records = tuple(r for r in snapshot.records if r.candidate_id == entry.candidate_id)
            conn.execute(
                "INSERT INTO evaluations(job_id,spec_version,candidate_id,scores,evidence,verdict) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(job_id,spec_version,candidate_id) "
                "DO UPDATE SET scores=excluded.scores,evidence=excluded.evidence,verdict=excluded.verdict",
                (snapshot.job_id, snapshot.spec_version, entry.candidate_id,
                 Jsonb({"contract": CONTRACT, "records": wire(records), "tier": entry.tier,
                        "nice_to_have_met": entry.nice_to_have_met}),
                 Jsonb([{"criterion_id": r.evaluation.criterion_id, "evidence": wire(r.evaluation.evidence)} for r in records]),
                 TIER_VERDICT[entry.tier]),
            )
        self.audit(conn, snapshot.job_id, actor, "RANKING_PUBLISHED", {
            "spec_version": snapshot.spec_version,
            "snapshot": snapshot.model_dump(mode="json", exclude={"records"}),
        })
        self.set_recompute(conn, snapshot.job_id, actor, "READY", snapshot.spec_version)

    def save_feedback(self, job_id, actor, candidate_id, decision, reason, expected_version):
        candidate_id = uuid_text(candidate_id)
        with self.write_job(job_id, actor) as (conn, job):
            spec = self.spec(conn, job_id)
            if spec.version != expected_version:
                raise StaleProposalError("反馈来自旧版页面，请刷新当前标准后重新反馈。")
            snapshot = self.snapshot(conn, job_id)
            candidates = self.candidates(conn, job_id)
            if not snapshot or snapshot.spec_version != spec.version or {r.candidate_id for r in snapshot.records} != {str(c["id"]) for c in candidates}:
                raise StoreError("现行版本尚未完整评估，请先完成重算再反馈。")
            if candidate_id not in {str(c["id"]) for c in candidates}:
                raise NotFound("该候选人不属于此职位。")
            feedback = Feedback(feedback_id=str(uuid4()), candidate_id=candidate_id,
                                spec_version=spec.version, decision=decision, reason=reason, reviewer_id=actor)
            conn.execute(
                "INSERT INTO feedback(id,job_id,candidate_id,spec_version,text,created_by) VALUES (%s,%s,%s,%s,%s,%s)",
                (feedback.feedback_id, job_id, candidate_id, spec.version, reason, actor),
            )
            self.audit(conn, job_id, actor, "FEEDBACK_SAVED", {"feedback": feedback})
        return feedback

    def feedback_context(self, job_id, actor, feedback_id):
        with self.connect(read_only=True) as conn:
            self.owned_job(conn, job_id, actor)
            row = conn.execute(
                "SELECT payload FROM audit_events WHERE job_id=%s AND event_type=%s "
                "AND payload->'feedback'->>'feedback_id'=%s AND payload->>'contract'=%s ORDER BY id DESC LIMIT 1",
                (job_id, PREFIX+"FEEDBACK_SAVED", uuid_text(feedback_id), CONTRACT),
            ).fetchone()
            if not row:
                raise NotFound("未找到反馈记录。")
            feedback = Feedback.model_validate(row["payload"]["feedback"])
            spec = self.spec(conn, job_id)
            snapshot = self.snapshot(conn, job_id)
            if feedback.spec_version != spec.version:
                raise StaleProposalError("反馈基础版本已过期，请基于现行版本重新反馈。")
            if not snapshot or snapshot.spec_version != spec.version:
                raise StoreError("当前版本未完成评估。")
            existing = conn.execute("SELECT * FROM change_proposals WHERE job_id=%s AND feedback_id=%s ORDER BY created_at LIMIT 1", (job_id, feedback_id)).fetchone()
            records = tuple(r for r in snapshot.records if r.candidate_id == feedback.candidate_id)
        return feedback, spec, records, existing

    def save_proposal(self, job_id, actor, proposal, impact=None):
        with self.write_job(job_id, actor) as (conn, job):
            if job["current_spec_version"] != proposal.base_spec_version:
                raise StaleProposalError("生成提议期间标准已改变，请重新反馈。")
            existing = conn.execute("SELECT * FROM change_proposals WHERE job_id=%s AND feedback_id=%s", (job_id, proposal.feedback_id)).fetchone()
            if existing:
                return self.proposal_view(existing)
            if proposal.status == ProposalStatus.PENDING_APPROVAL and conn.execute("SELECT 1 FROM change_proposals WHERE job_id=%s AND status='PENDING'", (job_id,)).fetchone():
                raise StoreError("已有一条待审批提议，请先接受或拒绝；本次反馈已保存，可稍后重试生成。")
            row = conn.execute(
                "INSERT INTO change_proposals(id,job_id,feedback_id,from_version,proposal,risk_flags,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (proposal.proposal_id, job_id, proposal.feedback_id, proposal.base_spec_version,
                 Jsonb({"contract": CONTRACT, "ai": wire(proposal), "impact": wire(impact)}),
                 [proposal.status.value] if proposal.status != ProposalStatus.PENDING_APPROVAL else [],
                 PROPOSAL_STATUS[proposal.status]),
            ).fetchone()
            self.audit(conn, job_id, actor, "PROPOSAL_SAVED", {"proposal_id": proposal.proposal_id, "ai_status": proposal.status.value})
            if proposal.status == ProposalStatus.PENDING_APPROVAL:
                conn.execute("UPDATE jobs SET status='AWAITING_APPROVAL',updated_at=now() WHERE id=%s", (job_id,))
        return self.proposal_view(row)

    @staticmethod
    def effective(row):
        content = row["final_proposal"] or row["proposal"]
        if content.get("contract") != CONTRACT:
            raise StoreError("提议记录格式不兼容。")
        return ChangeProposal.model_validate(content["ai"])

    @classmethod
    def proposal_view(cls, row):
        content = row["final_proposal"] or row["proposal"]
        proposal = cls.effective(row)
        return wire({
            "proposal": proposal, "approval_status": row["status"],
            "impact": content.get("impact"), "receipt": content.get("receipt"),
            "can_approve": row["status"] == "PENDING" and proposal.status == ProposalStatus.PENDING_APPROVAL,
        })

    def proposal_context(self, job_id, actor, proposal_id):
        with self.connect(read_only=True) as conn:
            self.owned_job(conn, job_id, actor)
            row = conn.execute("SELECT * FROM change_proposals WHERE id=%s AND job_id=%s", (uuid_text(proposal_id), job_id)).fetchone()
            if not row:
                raise NotFound("未找到此职位的提议。")
            return row, self.spec(conn, job_id)

    @staticmethod
    def pending(row, current):
        proposal = RecruitingStore.effective(row)
        if row["status"] != "PENDING" or proposal.status != ProposalStatus.PENDING_APPROVAL:
            raise ApprovalError("仅待审批且通过安全检查的提议可以接受、编辑或拒绝。")
        if row["from_version"] != current.version:
            raise StaleProposalError("提议基础版本已过期，请重新生成。")
        return proposal

    def edit_proposal(self, job_id, actor, proposal_id, original, edited, impact):
        with self.write_job(job_id, actor) as (conn, job):
            row = conn.execute("SELECT * FROM change_proposals WHERE id=%s AND job_id=%s FOR UPDATE", (proposal_id, job_id)).fetchone()
            if not row:
                raise NotFound("未找到提议。")
            current = self.spec(conn, job_id)
            if self.pending(row, current) != original:
                raise StoreError("提议已经变化，请重新加载。")
            row = conn.execute(
                "UPDATE change_proposals SET final_proposal=%s WHERE id=%s RETURNING *",
                (Jsonb({"contract": CONTRACT, "ai": wire(edited), "impact": wire(impact)}), proposal_id),
            ).fetchone()
            self.audit(conn, job_id, actor, "PROPOSAL_EDITED", {"proposal_id": proposal_id})
        return self.proposal_view(row)

    def accept(self, job_id, actor, proposal_id, original, impact):
        with self.write_job(job_id, actor) as (conn, job):
            row = conn.execute("SELECT * FROM change_proposals WHERE id=%s AND job_id=%s FOR UPDATE", (proposal_id, job_id)).fetchone()
            if not row:
                raise NotFound("未找到提议。")
            if row["status"] == "APPROVED":
                return ApprovalReceipt.model_validate(row["final_proposal"]["receipt"]), False
            current = self.spec(conn, job_id)
            if self.pending(row, current) != original or impact.base_spec_version != current.version:
                raise StoreError("提议已经变化，请重新审核。")
            spec = SpecVersion(job_id=job_id, version=current.version+1, criteria=impact.criteria,
                               created_at=datetime.now(timezone.utc), approved_by=actor,
                               next_criterion_number=impact.next_criterion_number)
            receipt = ApprovalReceipt(proposal_id=proposal_id, action="ACCEPTED", reviewer_id=actor, result_version=spec.version)
            conn.execute(
                "UPDATE change_proposals SET status='APPROVED',decided_by=%s,decided_at=now(),final_proposal=%s WHERE id=%s",
                (actor, Jsonb({"contract": CONTRACT, "ai": wire(original), "impact": wire(impact), "receipt": wire(receipt)}), proposal_id),
            )
            self.insert_spec(conn, actor, spec, {"proposal_id": proposal_id, "impact": impact}, proposal_id)
            self.audit(conn, job_id, actor, "PROPOSAL_ACCEPTED", {"receipt": receipt})
        return receipt, True

    def reject(self, job_id, actor, proposal_id):
        with self.write_job(job_id, actor) as (conn, job):
            row = conn.execute("SELECT * FROM change_proposals WHERE id=%s AND job_id=%s FOR UPDATE", (uuid_text(proposal_id), job_id)).fetchone()
            if not row:
                raise NotFound("未找到提议。")
            if row["status"] == "REJECTED":
                return ApprovalReceipt.model_validate(row["final_proposal"]["receipt"])
            # Rejecting a stale pending proposal is allowed, so the pending slot can be cleared.
            proposal = self.effective(row)
            if row["status"] != "PENDING" or proposal.status != ProposalStatus.PENDING_APPROVAL:
                raise ApprovalError("该提议不可拒绝；已接受、阻止或待澄清的提议不能重复决策。")
            receipt = ApprovalReceipt(proposal_id=proposal_id, action="REJECTED", reviewer_id=actor, result_version=job["current_spec_version"])
            content = row["final_proposal"] or row["proposal"]
            conn.execute("UPDATE change_proposals SET status='REJECTED',decided_by=%s,decided_at=now(),final_proposal=%s WHERE id=%s", (actor, Jsonb({**content, "receipt": wire(receipt)}), proposal_id))
            self.audit(conn, job_id, actor, "PROPOSAL_REJECTED", {"receipt": receipt})
            snapshot = self.snapshot(conn, job_id)
            status = "EVALUATED" if snapshot and snapshot.spec_version == job["current_spec_version"] else "RUNNING"
            conn.execute("UPDATE jobs SET status=%s,updated_at=now() WHERE id=%s", (status, job_id))
            return receipt

    def label(self, job_id, actor, candidate_id, label, split):
        with self.write_job(job_id, actor) as (conn, job):
            exists = conn.execute("SELECT 1 FROM candidates WHERE id=%s AND job_id=%s", (uuid_text(candidate_id), job_id)).fetchone()
            if not exists:
                raise NotFound("候选人不属于此职位。")
            conn.execute(
                "INSERT INTO candidate_labels(candidate_id,job_id,label,split,labeled_by) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT(candidate_id) DO UPDATE SET label=excluded.label,split=excluded.split,labeled_by=excluded.labeled_by",
                (candidate_id, job_id, label, split, actor),
            )
            self.audit(conn, job_id, actor, "LABEL_SAVED", {"candidate_id": candidate_id, "label": label, "split": split})
        return {"candidate_id": candidate_id, "label": label, "split": split}
