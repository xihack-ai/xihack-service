"""FastAPI 入口:职位/候选人业务 CRUD + 招聘标准校准工作流接口。

职责边界:FastAPI 负责接口、权限、数据库 CRUD;
LangGraph 只在 /calibrations 下管理有状态、可暂停恢复的校准流程。
"""

import os
import hashlib
import threading
import uuid
from contextlib import contextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, HTTPException, UploadFile  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
import psycopg  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app import db, llm, store  # noqa: E402
from app import database  # noqa: E402
from app import resume_parser  # noqa: E402
from app.hireloop_rules import change_impact, judgment, proposal_criteria, risk_assessment  # noqa: E402
from app.recruiting_api import router as recruiting_router  # noqa: E402
from app.workflow import graph as wf  # noqa: E402
from app.workflow import nodes as wf_nodes  # noqa: E402
from app.schemas import (ApprovalIn, CalibrationOut, FeedbackIn, FeedbackAnswerIn, ClarificationFeedbackIn, HistoryOut,  # noqa: E402
                         EvaluateCandidatesIn, JobIn, SpecConfirmIn, ManualSpecProposalIn,
                         RequirementReanalyzeIn, RequirementClarifyIn, ProposalEditIn, WorkflowOut)
from hirespec_loop.errors import HireSpecError  # noqa: E402

app = FastAPI(title="xihack-service")
app.include_router(recruiting_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv(
            "CORS_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000,"
            "http://localhost:3001,http://127.0.0.1:3001,"
            "http://localhost:3002,http://127.0.0.1:3002,"
            "http://localhost:9023,http://127.0.0.1:9023",
        ).split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if db.is_configured():
    db.ensure_schema()

@app.exception_handler(database.DatabaseCheckError)
async def database_config_error(request, exc):
    return JSONResponse(status_code=503, content={"detail": {"code": exc.code, "message": exc.message}})


@app.exception_handler(HireSpecError)
async def hiring_error(request, exc):
    code = exc.code
    status = 404 if code == "NOT_FOUND" else 503 if code == "MODEL_TASK_ERROR" else 409 if code in {"STORE_CONFLICT", "STALE_PROPOSAL", "JOB_BUSY", "APPROVAL_ERROR"} else 422
    detail = {"code": code, "message": str(exc)}
    if code == "NEEDS_CLARIFICATION" and getattr(exc, "questions", None):
        detail["questions"] = exc.questions
    if getattr(exc, "feedback_id", None):
        detail["feedback_id"] = exc.feedback_id
    return JSONResponse(status_code=status, content={"detail": detail})


@app.exception_handler(psycopg.Error)
async def hiring_database_error(request, exc):
    # Never expose DSNs, passwords, resume text or raw PostgreSQL error details.
    return JSONResponse(status_code=503, content={"detail": {
        "code": "DATABASE_TASK_FAILED", "message": "数据库操作失败，未提交的事务已回滚；请读取职位状态确认已完成的步骤后重试。",
    }})


# 未配置 PG 时的进程内业务存储(仅本地开发用)
_mem_jobs: dict[str, dict] = {}

# 同一 job 的变更串行化:锁非阻塞,拿不到直接 409(不排队,避免叠提案/脏重评)。
_job_locks: dict[str, threading.Lock] = {}
_job_locks_guard = threading.Lock()


@contextmanager
def _job_mutex(job_id: str):
    with _job_locks_guard:
        lock = _job_locks.setdefault(job_id, threading.Lock())
    if not lock.acquire(blocking=False):
        raise HTTPException(409, detail={"code": "JOB_BUSY",
                                         "message": "该职位正在处理其他变更，请稍后重试"})
    try:
        yield
    finally:
        lock.release()

# 档位顺序:同档内再按已满足标准的权重之和降序
_TIER_ORDER = {"推荐": 0, "待核实": 1, "不推荐": 2, "否决": 3}


def _save_job(job: dict) -> None:
    if db.is_configured():
        store.save_job(job)
    else:
        _mem_jobs[job["id"]] = job


def _load_job(job_id: str) -> dict | None:
    if db.is_configured():
        return store.load_job(job_id)
    return _mem_jobs.get(job_id)


def _config(job_id: str) -> dict:
    return {"configurable": {"thread_id": job_id}}


def _rank(evaluations: list[dict], criteria: list[dict]) -> list[dict]:
    """分档排序:先按档位,再按已满足标准的权重之和(仅档内排序,不参与定档)。"""
    weight = {c["id"]: int(c.get("weight", 2)) for c in criteria}
    return sorted(
        evaluations,
        key=lambda e: (_TIER_ORDER.get(e["verdict"], 9),
                       -sum(weight.get(cid, 0) for cid, s in e["scores"].items() if s == 2), e["candidate_id"]))


def _workflow_info(snap) -> WorkflowOut:
    """workflow 元信息全部来自真实 checkpoint 状态(get_state.next),不虚构轨迹。"""
    nxt = list(snap.next or ())
    waiting = ("spec_confirmation" if "spec_confirmation" in nxt
               else "human_approval" if "human_approval" in nxt else None)
    history = [h for h in (snap.values or {}).get("workflow_history", []) if isinstance(h, dict)]
    completed: list[str] = []
    for h in history:
        node = h.get("node")
        if isinstance(node, str) and node not in completed:
            completed.append(node)
    return WorkflowOut(
        next_nodes=nxt,
        completed_nodes=completed,
        waiting_for=waiting,  # type: ignore[arg-type]
        model_mode=llm.model_mode(),
        checkpointer="postgres" if db.is_configured() else "memory",  # type: ignore[arg-type]
        history=history,
    )


def _snapshot(job_id: str) -> CalibrationOut:
    """把图当前状态快照成 API 响应。"""
    snap = wf.get_graph().get_state(_config(job_id))
    if not snap.values:
        raise HTTPException(404, "校准流程不存在")
    v = snap.values
    workflow = _workflow_info(snap)
    awaiting = workflow.waiting_for == "human_approval"
    payload = {}
    if awaiting and snap.tasks:
        intr = list(snap.tasks[0].interrupts or [])
        payload = intr[0].value if intr else {}
    names = {c["id"]: c["name"] for c in v.get("candidates", [])}
    criteria = v.get("criteria", [])
    evals = [{**e, "name": names.get(e["candidate_id"], ""),
              "judgments": e.get("judgments") or {c["id"]: judgment(c["type"], e["scores"].get(c["id"])) for c in criteria}}
             for e in v.get("evaluations", [])]
    ranked = _rank(evals, criteria)
    tiers = {t: sum(1 for e in ranked if e["verdict"] == t) for t in _TIER_ORDER}
    status = ("AWAITING_SPEC_CONFIRMATION" if workflow.waiting_for == "spec_confirmation"
              else "AWAITING_APPROVAL" if awaiting else "EVALUATED")
    if db.is_configured():
        store.update_job_status(job_id, status)
    return CalibrationOut(
        job_id=job_id,
        job_title=v.get("job_title", ""),
        requirement=v.get("requirement", ""),
        status=status,
        spec_version=v.get("spec_version", 0),
        criteria=criteria,
        evidence=[e for e in v.get("evidence", []) if e.get("valid")],
        evaluations=ranked,
        candidates=[{
            "candidate_id": c["id"], "name": c["name"],
            "source_file": c.get("source_file"),
            "character_count": len(c["resume_text"]),
            "parser_version": c.get("parser_version"),
            "evaluated": any(e["candidate_id"] == c["id"] for e in evals),
        } for c in v.get("candidates", [])],
        tiers=tiers,
        risk_flags=v.get("risk_flags", []),
        change_proposal=v.get("change_proposal") or (payload or {}).get("proposal"),
        approval=v.get("approval"),
        feedback_notes=v.get("feedback_notes", []),
        workflow=workflow,
        clarified_requirement=v.get("clarified_requirement"),
        clarification_questions=v.get("clarification_questions"),
        clarification_responses=v.get("clarification_responses") or {},
        allocated_criterion_ids=v.get("allocated_criterion_ids", []),
        recomputation=v.get("recomputation"),
        proposal_impact=change_impact(criteria, proposal_criteria(v), len(evals)) if awaiting else None,
        feedback_status=v.get("feedback_status"), feedback_question=v.get("feedback_question"),
        feedback_id=v.get("feedback_id"), feedback_review=v.get("feedback_review") or {},
        feedback_thread=v.get("feedback_thread") or {},
        approval_draft=v.get("approval_draft"),
        requirement_revision=v.get("requirement_revision") if awaiting else None,
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "storage": "postgres" if db.is_configured() else "memory"}


def _answered_requirement(requirement, answers):
    """Manager answers remain explicit source facts; never replace the original intent."""
    if not answers:
        return requirement.strip()
    return requirement.strip() + "\n\n用人经理澄清问答（以下回答补充或明确原需求）：\n" + "\n".join(
        f"问题：{item.question}\n经理回答：{item.answer}" for item in answers)


@app.post("/requirements/clarify")
def clarify_requirement(body: RequirementClarifyIn) -> dict:
    """Read-only preflight: ambiguity is a question, not a failed job creation."""
    requirement = _answered_requirement(body.requirement, body.answers)
    review = risk_assessment([requirement])
    if review["status"] != "SAFE":
        return {**review, "clarified_requirement": None}
    # On later rounds the semantic reviewer considers all answers, avoiding a
    # second independent question generator reopening already answered questions.
    clarification = wf_nodes.clarify({"requirement": requirement}) if not body.answers else {}
    questions = [{"question": q, "options": []} for q in clarification.get("clarification_questions", [])]
    return {"status": "NEEDS_CLARIFICATION" if questions else "SAFE",
            "reason": "请用人经理逐题明确岗位要求。" if questions else "需求已可生成待确认的标准草稿。",
            "questions": questions, "source_texts": [requirement],
            "clarified_requirement": clarification.get("clarified_requirement", requirement)}


@app.post("/jobs", response_model=CalibrationOut)
def create_job(body: JobIn) -> CalibrationOut:
    """建职位 + 候选人并启动校准。

    默认(旧行为兼容):澄清→标准→证据→评估一次跑完;
    require_spec_confirmation=true 时停在 spec_confirmation,
    首次标准确认前绝不抽证据/评估。
    """
    requirement = _answered_requirement(body.requirement, body.clarification_answers)
    # Never trust a frontend preflight status; edited answers are reviewed again.
    if body.clarification_answers:
        review = risk_assessment([requirement])
        if review["status"] != "SAFE":
            raise HTTPException(422, detail={"code": "POLICY_BLOCKED" if review["status"] == "BLOCKED" else "NEEDS_CLARIFICATION",
                "message": review["reason"], "questions": review["questions"]})
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id, "title": body.title, "requirement": requirement,
        "candidates": [{"id": str(uuid.uuid4()), "name": c.name, "resume_text": c.resume_text}
                       for i, c in enumerate(body.candidates)],
    }
    _save_job(job)
    wf.get_graph().invoke(
        {"job_id": job_id, "job_title": job["title"], "requirement": job["requirement"],
         "candidates": job["candidates"], "spec_version": 1,
         "require_spec_confirmation": body.require_spec_confirmation},
        _config(job_id))
    return _snapshot(job_id)


@app.get("/health/database")
def database_health() -> dict:
    try:
        report = database.inspect_database()
    except database.DatabaseCheckError as exc:
        raise HTTPException(
            status_code=503, detail={"code": exc.code, "message": exc.message}
        ) from None
    # HTTP 健康接口只返回检查结果，完整表名及账号只在本地检查终端显示。
    return {
        "status": report["status"], "database": report["database"],
        "read_only": report["read_only"],
        "expected_tables_present": report["expected_tables_present"],
    }


@app.get("/calibrations/{job_id}", response_model=CalibrationOut)
def get_calibration(job_id: str) -> CalibrationOut:
    return _snapshot(job_id)


@app.post("/calibrations/{job_id}/candidates/upload")
def upload_candidates(job_id: str, files: list[UploadFile] = File(...)) -> dict:
    """Parse locally and append candidates; no model calls and no automatic evaluation."""
    try:
        if not 1 <= len(files) <= resume_parser.MAX_FILES:
            raise HTTPException(422, "每批请上传 1～50 份简历。")
        g = wf.get_graph()
        with _job_mutex(job_id):
            snap = g.get_state(_config(job_id))
            if not snap.values:
                raise HTTPException(404, "校准流程不存在")
            if "human_approval" in snap.next:
                raise HTTPException(409, "请先处理待审批提案，再上传简历。")
            if snap.next and "spec_confirmation" not in snap.next:
                raise HTTPException(409, "有尚未完成的评估，请先重试评估再上传简历。")
            parsed, results, total_bytes = [], [], 0
            for file in files:
                filename = resume_parser.safe_filename(file.filename)
                data = file.file.read(resume_parser.MAX_FILE_BYTES + 1)
                total_bytes += len(data)
                if total_bytes > resume_parser.MAX_BATCH_BYTES:
                    raise HTTPException(413, "每批文件总大小不能超过 50 MB。")
                try:
                    candidate = resume_parser.parse_resume(data, filename, file.content_type)
                    parsed.append(candidate)
                    results.append({"filename": filename, "status": "parsed",
                                    "character_count": len(candidate["resume_text"])})
                except resume_parser.ResumeParseError as exc:
                    results.append({"filename": filename, "status": "failed", "error": str(exc)})
            existing = list(snap.values.get("candidates", []))
            by_hash = {hashlib.sha256(c["resume_text"].encode()).hexdigest(): c for c in existing}
            # Persist only new text, preserving IDs and evaluations for duplicate uploads.
            fresh = {}
            for candidate in parsed:
                if candidate["resume_hash"] not in by_hash:
                    fresh.setdefault(candidate["resume_hash"], candidate)
            stored = store.add_candidates(job_id, list(fresh.values())) if fresh else []
            for candidate in stored:
                existing.append(candidate)
                by_hash[candidate["resume_hash"]] = candidate
            seen = {hashlib.sha256(c["resume_text"].encode()).hexdigest()
                    for c in snap.values.get("candidates", [])}
            parsed_iter = iter(parsed)
            for result in results:
                if result["status"] == "failed":
                    continue
                candidate = next(parsed_iter)
                digest = candidate["resume_hash"]
                result.update(status="duplicate" if digest in seen else "imported",
                              candidate_id=by_hash[digest]["id"])
                seen.add(digest)
            if stored:
                # Preserve the initial approval pause; an import never approves a spec.
                as_node = "generate_spec" if "spec_confirmation" in snap.next else "evaluate"
                update = {"candidates": existing}
                if "spec_confirmation" in snap.next:
                    update["defer_initial_evaluation"] = True
                g.update_state(_config(job_id), update, as_node=as_node)
                if not db.is_configured() and job_id in _mem_jobs:
                    _mem_jobs[job_id]["candidates"] = existing
            return {"results": results, "calibration": _snapshot(job_id)}
    finally:
        for file in files:
            file.file.close()


@app.get("/calibrations/{job_id}/candidates/{candidate_id}/resume")
def candidate_resume(job_id: str, candidate_id: str) -> dict:
    snap = wf.get_graph().get_state(_config(job_id))
    for candidate in snap.values.get("candidates", []):
        if candidate["id"] == candidate_id:
            return {"candidate_id": candidate_id, "name": candidate["name"],
                    "source_file": candidate.get("source_file"),
                    "resume_text": candidate["resume_text"]}
    raise HTTPException(404, "该职位下没有此候选人。")


@app.post("/calibrations/{job_id}/candidates/evaluate", response_model=CalibrationOut)
def evaluate_candidates(job_id: str, body: EvaluateCandidatesIn) -> CalibrationOut:
    """Evaluate only new candidates using the confirmed spec; retry is idempotent."""
    g = wf.get_graph()
    with _job_mutex(job_id):
        snap = g.get_state(_config(job_id))
        if not snap.values:
            raise HTTPException(404, "校准流程不存在")
        if "spec_confirmation" in snap.next or "human_approval" in snap.next:
            raise HTTPException(409, "请先确认招聘标准或处理待审批提案。")
        if not snap.values.get("criteria"):
            raise HTTPException(409, "尚未生成招聘标准。")
        if llm.model_mode() == "offline_fallback":
            raise HTTPException(503, detail={"code": "LIVE_MODEL_REQUIRED",
                                           "message": "请在后端配置真实模型并关闭 REPLAY_ONLY 后评估简历。"})
        known = {c["id"] for c in snap.values.get("candidates", [])}
        if not set(body.candidate_ids) <= known:
            raise HTTPException(422, "包含不属于该职位的候选人。")
        evaluated = {e["candidate_id"] for e in snap.values.get("evaluations", [])}
        pending = list(dict.fromkeys(cid for cid in body.candidate_ids if cid not in evaluated))
        if pending:
            # Reset an unfinished extraction from a failed request; completed results stay visible.
            if snap.next:
                if set(snap.next) - {"extract_evidence", "validate_evidence", "evaluate"}:
                    raise HTTPException(409, "其他流程尚未完成，请先处理当前流程。")
                g.update_state(_config(job_id), {"evaluation_requested": False}, as_node="evaluate")
            g.invoke({"evaluation_requested": True, "reeval_candidates": pending,
                      "reeval_criteria": None}, _config(job_id))
        return _snapshot(job_id)


@app.post("/calibrations/{job_id}/spec/confirm", response_model=CalibrationOut)
def confirm_spec(job_id: str, body: SpecConfirmIn) -> CalibrationOut:
    """首次标准确认:按(可能编辑过的) criteria save_spec 后继续抽证/评估。

    criteria 校验(text/type/唯一 ID/偏见风险)全部在 Command(resume=...) 之前
    完成;不合法直接 422,不会在 interrupt 恢复路径里报错污染 checkpoint。
    """
    g = wf.get_graph()
    with _job_mutex(job_id):
        snap = g.get_state(_config(job_id))
        if not snap.values:
            raise HTTPException(404, "校准流程不存在")
        if "spec_confirmation" not in (snap.next or ()):
            raise HTTPException(409, "当前没有待确认的初始标准")
        edited = wf_nodes.normalize_criteria([c.model_dump() for c in body.criteria])
        wf_nodes.check_criteria_risk(edited)
        g.invoke(Command(resume={"confirmed": True, "criteria": edited,
                                 "note": "spec_confirm", "decided_by": "hr"}),
                 _config(job_id))
        return _snapshot(job_id)


@app.post("/calibrations/{job_id}/feedback", response_model=CalibrationOut)
def submit_feedback(job_id: str, body: FeedbackIn) -> CalibrationOut:
    """提交 HR 反馈:图从入口路由到反馈分支,产出提案后停在人工审批。"""
    g = wf.get_graph()
    with _job_mutex(job_id):
        snap = g.get_state(_config(job_id))
        if not snap.values:
            raise HTTPException(404, "校准流程不存在")
        if "spec_confirmation" in (snap.next or ()):
            raise HTTPException(
                409, detail={"code": "SPEC_CONFIRMATION_REQUIRED",
                             "message": "请先确认初始标准(/spec/confirm),再提交反馈"})
        if "human_approval" in (snap.next or ()):
            raise HTTPException(409, "已有提案待审批,请先 Accept / Edit / Reject")
        if snap.next:
            raise HTTPException(409, "评估尚未完成，请先重试评估，再提交反馈。")
        g.invoke({"feedback": body.text, "feedback_created_by": body.created_by,
                  "feedback_thread": {"original": body.text, "rounds": [], "base_spec_version": snap.values.get("spec_version")},
                  "clarification_active_index": None}, _config(job_id))
        return _snapshot(job_id)


@app.post("/calibrations/{job_id}/feedback/answer", response_model=CalibrationOut)
def answer_feedback(job_id: str, body: FeedbackAnswerIn) -> CalibrationOut:
    """Continue the same feedback conversation; answers never publish a Spec."""
    g = wf.get_graph()
    with _job_mutex(job_id):
        snap = g.get_state(_config(job_id))
        state = snap.values
        if not state:
            raise HTTPException(404, "校准流程不存在")
        if snap.next or state.get("feedback_status") != "NEEDS_CLARIFICATION":
            raise HTTPException(409, "当前没有等待回答的反馈，请刷新任务")
        if body.base_spec_version != state.get("spec_version") or body.feedback_id != state.get("feedback_id"):
            raise HTTPException(409, "反馈或标准版本已变化，请刷新后回答")
        questions = (state.get("feedback_review") or {}).get("questions") or [
            {"question": state.get("feedback_question") or "请补充具体岗位要求"}]
        if len(body.answers) != len(questions):
            raise HTTPException(422, "请回答全部澄清问题")
        thread = dict(state.get("feedback_thread") or {"original": state.get("feedback_raw", ""), "rounds": []})
        rounds = list(thread.get("rounds", [])) + [{"feedback_id": body.feedback_id,
            "questions": questions, "answers": body.answers, "answered_by": body.created_by or "manager"}]
        thread["rounds"] = rounds
        text = "原反馈：" + thread["original"]
        for round_ in rounds:
            for question, answer in zip(round_["questions"], round_["answers"]):
                text += f"\n澄清问题：{question['question']}\n经理回答：{answer}"
        text += "\n请根据经理回答解释原反馈，只建议明确且岗位相关的最小修改，不新增未确认要求。"
        try:
            g.invoke({"feedback": text, "feedback_created_by": body.created_by,
                      "feedback_thread": thread}, _config(job_id))
        except HireSpecError:
            # A failed model task must leave the same unanswered round retryable.
            g.update_state(_config(job_id), dict(state), as_node="evaluate")
            raise
        store.audit(job_id, body.created_by or "manager", "FEEDBACK_CLARIFIED",
                    {"feedback_id": body.feedback_id, "round": len(rounds), "base_spec_version": body.base_spec_version})
        return _snapshot(job_id)


@app.post("/calibrations/{job_id}/spec/manual-proposal", response_model=CalibrationOut)
def propose_manual_spec(job_id: str, body: ManualSpecProposalIn) -> CalibrationOut:
    """Stage a manager-authored revision at human_approval without changing the active Spec."""
    g = wf.get_graph()
    with _job_mutex(job_id):
        snap = g.get_state(_config(job_id))
        if not snap.values:
            raise HTTPException(404, "校准流程不存在")
        if snap.next:
            raise HTTPException(409, "请先处理当前待确认标准或待审批提案")
        current = snap.values
        version = int(current.get("spec_version") or 1)
        if body.base_spec_version != version:
            raise HTTPException(409, "提议基础版本已过期，请刷新后重新编辑。")
        edited = wf_nodes.normalize_criteria([c.model_dump() for c in body.criteria])
        wf_nodes.check_criteria_risk(edited)
        old = {c["id"]: c for c in current.get("criteria", [])}
        new = {c["id"]: c for c in edited}
        retired = set(current.get("allocated_criterion_ids", [])) - set(old)
        if any(cid in retired for cid in new):
            raise HTTPException(422, "已删除的标准 ID 不得复用，请分配新 ID。")
        revised_requirement = body.requirement.strip() if body.requirement is not None else None
        if revised_requirement and revised_requirement != current.get("requirement"):
            risks = wf_nodes.risk_review([revised_requirement])
            if risks:
                raise HTTPException(422, "修订需求包含受限条件：" + "；".join(risks))
            clarification = wf_nodes.clarify({"requirement": revised_requirement})
        else:
            revised_requirement = None
            clarification = None
        if edited == current.get("criteria") and revised_requirement is None:
            raise HTTPException(422, "标准和招聘意图均未变化")
        proposal = {
            "reason": "招聘经理手动修订招聘标准" + ("和招聘意图" if revised_requirement else ""),
            "status": "PENDING_APPROVAL",
            "add": [dict(c) for c in edited if c["id"] not in old],
            "remove": sorted(old.keys() - new.keys()),
            "update": [dict(c) for c in edited if c["id"] in old and c != old[c["id"]]],
            "requirement_revision": revised_requirement,
        }
        fid = store.save_feedback(job_id, version, proposal["reason"], body.created_by)
        staged = {**current, "feedback_id": fid, "change_proposal": proposal}
        store.save_proposal(staged, status="PENDING_APPROVAL", risk_flags=[])
        g.update_state(_config(job_id), {
            "feedback_id": fid, "feedback_status": "PENDING_APPROVAL",
            "change_proposal": proposal, "approval_draft": edited,
            "requirement_revision": revised_requirement,
            "requirement_clarification_draft": clarification,
            "risk_flags": [], "feedback_question": None,
        }, as_node="propose_change")
        g.invoke(None, _config(job_id))
        return _snapshot(job_id)


@app.post("/calibrations/{job_id}/requirement/reanalyze", response_model=CalibrationOut)
def reanalyze_draft_requirement(job_id: str, body: RequirementReanalyzeIn) -> CalibrationOut:
    """Create a fresh unapproved draft from revised intent; keep the old draft intact."""
    with _job_mutex(job_id):
        snap = wf.get_graph().get_state(_config(job_id))
        if not snap.values:
            raise HTTPException(404, "校准流程不存在")
        if "spec_confirmation" not in (snap.next or ()):
            raise HTTPException(409, "已确认的职位请通过标准修订提案修改招聘意图")
        old = snap.values
        if body.base_spec_version != old.get("spec_version"):
            raise HTTPException(409, "草稿版本已变化，请刷新后再分析")
        requirement = body.requirement.strip()
        if requirement == old.get("requirement"):
            raise HTTPException(422, "招聘意图未变化")
        new_id = str(uuid.uuid4())
        candidates = [{**c, "id": str(uuid.uuid4())} for c in old.get("candidates", [])]
        job = {"id": new_id, "title": old.get("job_title", "未命名职位"),
               "requirement": requirement, "candidates": candidates}
        # The new draft is isolated; the old job and approved versions are untouched.
        _save_job(job)
        wf.get_graph().invoke({"job_id": new_id, "job_title": job["title"],
            "requirement": requirement, "candidates": candidates,
            "spec_version": 1, "require_spec_confirmation": True}, _config(new_id))
        return _snapshot(new_id)


@app.post("/calibrations/{job_id}/clarification-feedback", response_model=CalibrationOut)
def submit_clarification_feedback(job_id: str, body: ClarificationFeedbackIn) -> CalibrationOut:
    """逐条处理生成 Spec 后未确认的问题；变更仍走模型提案和人工审批。"""
    g = wf.get_graph()
    with _job_mutex(job_id):
        snap = g.get_state(_config(job_id))
        if not snap.values:
            raise HTTPException(404, "校准流程不存在")
        if body.base_spec_version != snap.values.get("spec_version"):
            raise HTTPException(409, "基础版本已过期，请刷新后重新提交。")
        if snap.next:
            raise HTTPException(409, "请先确认初始标准或处理当前待审批提案。")
        questions = snap.values.get("clarification_questions") or []
        if body.question_index >= len(questions):
            raise HTTPException(422, "待核实问题不存在")
        key = str(body.question_index)
        responses = dict(snap.values.get("clarification_responses") or {})
        if responses.get(key, {}).get("status") == "PENDING_APPROVAL":
            raise HTTPException(409, "该问题已有待审批提案。")
        response = {"question": questions[body.question_index], "category": body.category,
                    "answer": body.answer, "base_spec_version": body.base_spec_version,
                    "status": "NOT_REQUIRED" if body.category == "NOT_REQUIRED" else "SUBMITTED"}
        responses[key] = response
        if body.category == "NOT_REQUIRED":
            # 从完成状态写入同一 checkpoint，不启动反馈图，也不改变标准/排名。
            g.update_state(_config(job_id), {"clarification_responses": responses,
                                             "clarification_active_index": None,
                                             "risk_flags": [], "feedback_status": "NO_CHANGE",
                                             "feedback_question": None, "change_proposal": None},
                           as_node="evaluate")
            store.audit(job_id, body.created_by or "hr", "CLARIFICATION_NOT_REQUIRED",
                        {"question_index": body.question_index, "spec_version": body.base_spec_version})
            return _snapshot(job_id)
        label = "必须项" if body.category == "MUST_HAVE" else "满意项（加分项）"
        feedback = (f"针对待核实问题『{response['question']}』，招聘经理澄清为{label}："
                    f"{body.answer}。请只建议对应的最小标准变更，不要自行增加年限或其他门槛。")
        g.invoke({"feedback": feedback, "feedback_created_by": body.created_by or "hr",
                  "feedback_thread": {"original": feedback, "rounds": [], "base_spec_version": body.base_spec_version},
                  "clarification_responses": responses, "clarification_active_index": key}, _config(job_id))
        return _snapshot(job_id)


@app.post("/calibrations/{job_id}/proposal/edit", response_model=CalibrationOut)
def edit_proposal(job_id: str, body: ProposalEditIn) -> CalibrationOut:
    """Save a reviewed draft; keep the approval interrupt and current ranking."""
    g = wf.get_graph()
    with _job_mutex(job_id):
        snap = g.get_state(_config(job_id))
        if "human_approval" not in snap.next:
            raise HTTPException(409, "当前没有待审批提议")
        if body.base_spec_version is not None and body.base_spec_version != snap.values.get("spec_version"):
            raise HTTPException(409, "基础版本已过期，请刷新后编辑")
        edited = wf_nodes.normalize_criteria([c.model_dump() for c in body.criteria])
        wf_nodes.check_criteria_risk(edited)
        current_ids = {c["id"] for c in snap.values.get("criteria", [])}
        retired = set(snap.values.get("allocated_criterion_ids", [])) - current_ids
        if any(c["id"] in retired for c in edited):
            raise HTTPException(422, "已删除的标准 ID 不得复用")
        patch = {"approval_draft": edited}
        if body.requirement is not None:
            revised = body.requirement.strip()
            risks = wf_nodes.risk_review([revised])
            if risks:
                raise HTTPException(422, "修订需求包含受限条件：" + "；".join(risks))
            patch["requirement_revision"] = revised if revised != snap.values.get("requirement") else None
            patch["requirement_clarification_draft"] = (
                wf_nodes.clarify({"requirement": revised}) if patch["requirement_revision"] else None)
        if edited == snap.values.get("criteria") and patch.get("requirement_revision", snap.values.get("requirement_revision")) is None:
            raise HTTPException(422, "草稿没有变更；若不采纳提案，请点击拒绝")
        g.update_state(_config(job_id), patch, as_node="propose_change")
        store.audit(job_id, "manager", "PROPOSAL_DRAFT_SAVED", {"base_version": snap.values.get("spec_version")})
        return _snapshot(job_id)


@app.post("/calibrations/{job_id}/approval", response_model=CalibrationOut)
def submit_approval(job_id: str, body: ApprovalIn) -> CalibrationOut:
    """恢复被 interrupt 的审批节点:Accept / Edit / Reject。

    Edit 时用 body.criteria 覆盖整套标准(体现"人可以改写 AI 提案,而非二选一")。
    """
    g = wf.get_graph()
    with _job_mutex(job_id):
        snap = g.get_state(_config(job_id))
        if not snap.values:
            raise HTTPException(404, "校准流程不存在")
        if "human_approval" not in (snap.next or ()):
            raise HTTPException(409, "当前没有待审批的标准变更")
        if body.base_spec_version is not None and body.base_spec_version != snap.values.get("spec_version"):
            raise HTTPException(409, "提议基础版本已过期，请刷新当前版本后重新审核。")
        if body.action is None and body.criteria:
            action = "edit"
        else:
            action = body.action or ("accept" if body.approved else "reject")
        edit_input = body.criteria
        if action == "accept" and snap.values.get("approval_draft"):
            action = "edit"
            edit_input = snap.values["approval_draft"]
        if action == "edit" and not edit_input:
            raise HTTPException(422, "action=edit 必须提供 criteria")

        edited = None
        if action == "edit" and edit_input:
            # 格式/风险校验必须在 Command(resume=...) 之前:报错不入 checkpoint。
            edited = wf_nodes.normalize_criteria([c if isinstance(c, dict) else c.model_dump() for c in edit_input])
            wf_nodes.check_criteria_risk(edited)
            current_ids = {c["id"] for c in snap.values.get("criteria", [])}
            retired = set(snap.values.get("allocated_criterion_ids", [])) - current_ids
            if any(c["id"] in retired for c in edited):
                raise HTTPException(422, "已删除的标准 ID 不得复用，请分配新 ID。")
        elif action == "accept":
            # Recheck original or edited content immediately before publication.
            wf_nodes.check_criteria_risk(wf_nodes.normalize_criteria(proposal_criteria(snap.values)))

        decision = {
            "action": action,
            "approved": action in ("accept", "edit"),
            "criteria": edited,
            "note": body.note,
            "decided_by": body.decided_by or "hr",
        }
        g.invoke(Command(resume=decision), _config(job_id))
        if action != "reject" and job_id in _mem_jobs:
            _mem_jobs[job_id]["requirement"] = g.get_state(_config(job_id)).values.get("requirement", "")
        return _snapshot(job_id)


@app.get("/calibrations/{job_id}/history", response_model=HistoryOut)
def get_history(job_id: str) -> HistoryOut:
    """决策日志:标准版本演进、反馈、提案审批结论、审计流水。"""
    if not db.is_configured():
        raise HTTPException(503, "历史查询需要配置数据库")
    if not _load_job(job_id):
        raise HTTPException(404, "职位不存在")
    return HistoryOut(job_id=job_id, **store.load_history(job_id))
