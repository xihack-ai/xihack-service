"""业务落库:标准版本、评估、反馈、提案、审计。

设计约定:
- 未配置 PG 时全部静默跳过(本地开发/单测走内存态);
- 只由工作流节点与 API 层调用,不反向依赖它们;
- 每次写入都是 upsert,重复执行安全。
"""

import hashlib
import os
from typing import Any

from psycopg.types.json import Jsonb

from app import db


def _j(v: Any) -> Jsonb:
    return Jsonb(v)


def _update_job_version(job_id: str | None, version: int) -> None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "update jobs set current_spec_version = greatest(current_spec_version, %s), "
            "updated_at = now() where id = %s", (version, job_id))


def audit(job_id: str | None, actor: str, event_type: str, payload: dict) -> None:
    if not db.is_configured() or not job_id:
        return
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into audit_events(job_id, actor, event_type, payload) values (%s, %s, %s, %s)",
            (job_id, actor, event_type, _j(payload)))


def save_job(job: dict) -> None:
    """写职位与候选人(resume_hash 用于同职位内去重)。"""
    if not db.is_configured():
        return
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into jobs(id, title, requirement, status) values (%s, %s, %s, 'RUNNING') "
            "on conflict (id) do update set title = excluded.title, "
            "requirement = excluded.requirement, updated_at = now()",
            (job["id"], job["title"], job["requirement"]))
        for c in job["candidates"]:
            cur.execute(
                "insert into candidates(id, job_id, name, resume_text, resume_hash, source_file) "
                "values (%s, %s, %s, %s, %s, %s) on conflict (id) do nothing",
                (c["id"], job["id"], c["name"], c["resume_text"],
                 hashlib.sha256(c["resume_text"].encode()).hexdigest(),
                 c.get("source_file") or os.getenv("SEED_SOURCE_FILE")))
    audit(job["id"], "system", "JOB_CREATED", {"candidates": len(job["candidates"])})


def load_job(job_id: str) -> dict | None:
    if not db.is_configured():
        return None
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("select * from jobs where id = %s", (job_id,))
        job = cur.fetchone()
        if not job:
            return None
        cur.execute("select id, name, resume_text from candidates where job_id = %s order by created_at",
                    (job_id,))
        return {**job, "candidates": list(cur.fetchall())}


def add_candidates(job_id: str, candidates: list[dict]) -> list[dict]:
    """Store an import in one transaction; return canonical IDs on text duplicates."""
    if not db.is_configured():
        return candidates
    result = []
    with db.connect() as conn, conn.transaction(), conn.cursor() as cur:
        for c in candidates:
            cur.execute(
                "insert into candidates(id,job_id,name,resume_text,resume_hash,source_file,"
                "original_filename,mime_type,file_size,parse_status,parser_version,parsed_at) "
                "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,'COMPLETED',%s,now()) "
                "on conflict (job_id,resume_hash) do nothing",
                (c["id"], job_id, c["name"], c["resume_text"], c["resume_hash"],
                 c["source_file"], c["original_filename"], c["mime_type"],
                 c["file_size"], c["parser_version"]))
            cur.execute("select id,name,resume_text,resume_hash,source_file,parser_version "
                        "from candidates where job_id=%s and resume_hash=%s",
                        (job_id, c["resume_hash"]))
            row = dict(cur.fetchone())
            row["id"] = str(row["id"])
            result.append(row)
        cur.execute("insert into audit_events(job_id,actor,event_type,payload) "
                    "values (%s,'hr','CANDIDATES_IMPORTED',%s)",
                    (job_id, _j({"candidate_ids": [c["id"] for c in result]})))
    return result


def save_spec(job_id: str | None, version: int, criteria: list[dict],
              source_proposal: bool = False) -> None:
    if not db.is_configured() or not job_id:
        return
    src = None
    if source_proposal:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "select id from change_proposals where job_id = %s and status = 'APPROVED' "
                "order by created_at desc limit 1", (job_id,))
            row = cur.fetchone()
            src = row["id"] if row else None
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into spec_versions(job_id, version, criteria, source_proposal_id) "
            "values (%s, %s, %s, %s) on conflict (job_id, version) "
            "do update set criteria = excluded.criteria", (job_id, version, _j(criteria), src))
    _update_job_version(job_id, version)


def save_feedback(job_id: str | None, version: int, text: str,
                  created_by: str | None = None) -> str | None:
    if not db.is_configured() or not job_id:
        return None
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into feedback(job_id, spec_version, text, created_by) "
            "values (%s, %s, %s, %s) returning id", (job_id, version, text, created_by))
        fid = str(cur.fetchone()["id"])
    audit(job_id, created_by or "hr", "FEEDBACK_SUBMITTED",
          {"text": text, "spec_version": version})
    return fid


def update_job_status(job_id: str | None, status: str) -> None:
    if not db.is_configured() or not job_id:
        return
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("update jobs set status = %s, updated_at = now() where id = %s",
                    (status, job_id))


def save_proposal(state: dict, status: str, risk_flags: list[str]) -> None:
    """把本轮提案落库;status ∈ BLOCKED / EMPTY / PENDING_APPROVAL。"""
    job_id, fid = state.get("job_id"), state.get("feedback_id")
    if not db.is_configured() or not job_id or not fid:
        return
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into change_proposals(job_id, feedback_id, from_version, proposal, "
            "risk_flags, status) values (%s, %s, %s, %s, %s, %s)",
            (job_id, fid, int(state.get("spec_version", 1)),
             _j(state.get("change_proposal") or {}), risk_flags, status))
    audit(job_id, "system", "PROPOSAL_" + status, {"risk_flags": risk_flags})


def decide_proposal(job_id: str | None, decision: dict, status: str) -> None:
    """写审批结果(Accept/Edit → APPROVED,Reject → REJECTED)与 Edit 后的最终标准。"""
    if not db.is_configured() or not job_id:
        return
    final = decision.get("criteria") if status == "APPROVED" else None
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "update change_proposals set status = %s, decided_by = %s, decision_note = %s, "
            "final_proposal = %s, decided_at = now() "
            "where id = (select id from change_proposals where job_id = %s "
            "and status = 'PENDING_APPROVAL' order by created_at desc limit 1)",
            (status, decision.get("decided_by") or decision.get("actor"),
             decision.get("note"), _j(final) if final else None, job_id))
    audit(job_id, str(decision.get("decided_by") or "hr"), "PROPOSAL_DECIDED",
          {"status": status, "note": decision.get("note", "")})


def save_evaluations(job_id: str | None, version: int, evaluations: list[dict]) -> None:
    if not db.is_configured() or not job_id or not version:
        return
    quotes: dict[str, list[dict]] = {}
    for ev in evaluations:
        quotes[ev["candidate_id"]] = [
            {"criterion_id": s["criterion_id"], "quote": s["quote"]}
            for s in ev.get("tradeoff", {}).get("strengths", []) if s.get("quote")]
    with db.connect() as conn, conn.cursor() as cur:
        for ev in evaluations:
            cur.execute(
                "insert into evaluations(job_id, spec_version, candidate_id, scores, evidence, "
                "tradeoff, verdict) values (%s, %s, %s, %s, %s, %s, %s) "
                "on conflict (job_id, spec_version, candidate_id) do update "
                "set scores = excluded.scores, evidence = excluded.evidence, "
                "tradeoff = excluded.tradeoff, verdict = excluded.verdict, "
                "created_at = now()",
                (job_id, version, ev["candidate_id"], _j(ev["scores"]),
                 _j(quotes.get(ev["candidate_id"], [])), _j(ev.get("tradeoff", {})),
                 ev["verdict"]))
    audit(job_id, "system", "EVALUATED",
          {"spec_version": version, "count": len(evaluations),
           "tiers": {v: sum(1 for e in evaluations if e["verdict"] == v)
                     for v in ("推荐", "待核实", "不推荐", "否决")}})


def load_history(job_id: str) -> dict:
    """决策日志页数据源:标准版本 + 反馈 + 提案 + 审计。"""
    out: dict[str, Any] = {"spec_versions": [], "feedback": [], "proposals": [], "audit": []}
    if not db.is_configured():
        return out
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("select version, criteria, source_proposal_id, created_at from spec_versions "
                    "where job_id = %s order by version", (job_id,))
        out["spec_versions"] = list(cur.fetchall())
        cur.execute("select id, spec_version, text, created_by, created_at from feedback "
                    "where job_id = %s order by created_at", (job_id,))
        out["feedback"] = list(cur.fetchall())
        cur.execute("select id, feedback_id, from_version, proposal, final_proposal, risk_flags, "
                    "status, decided_by, decision_note, decided_at, created_at from change_proposals "
                    "where job_id = %s order by created_at", (job_id,))
        out["proposals"] = list(cur.fetchall())
        cur.execute("select id, actor, event_type, payload, created_at from audit_events "
                    "where job_id = %s order by id", (job_id,))
        out["audit"] = list(cur.fetchall())
    return out
