"""FastAPI 入口:职位/候选人业务 CRUD + 招聘标准校准工作流接口。

职责边界:FastAPI 负责接口、权限、数据库 CRUD;
LangGraph 只在 /calibrations 下管理有状态、可暂停恢复的校准流程。
"""

import os
import threading
import uuid
from contextlib import contextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
import psycopg  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app import db, llm, store  # noqa: E402
from app import database  # noqa: E402
from app.recruiting_api import router as recruiting_router  # noqa: E402
from app.workflow import graph as wf  # noqa: E402
from app.workflow import nodes as wf_nodes  # noqa: E402
from app.schemas import (ApprovalIn, CalibrationOut, FeedbackIn, HistoryOut,  # noqa: E402
                         JobIn, SpecConfirmIn, WorkflowOut)
from hirespec_loop.errors import HireSpecError  # noqa: E402

app = FastAPI(title="xihack-service")
app.include_router(recruiting_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv(
            "CORS_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000,"
            "http://localhost:3001,http://127.0.0.1:3001,"
            "http://localhost:3002,http://127.0.0.1:3002",
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
                       -sum(weight.get(cid, 0) for cid, s in e["scores"].items() if s == 2)))


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
        model_mode="live" if (llm.is_configured() and not llm.replay_only())
                   else "offline_fallback",  # type: ignore[arg-type]
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
    evals = [{**e, "name": names.get(e["candidate_id"], "")} for e in v.get("evaluations", [])]
    ranked = _rank(evals, criteria)
    tiers = {t: sum(1 for e in ranked if e["verdict"] == t) for t in _TIER_ORDER}
    status = ("AWAITING_SPEC_CONFIRMATION" if workflow.waiting_for == "spec_confirmation"
              else "AWAITING_APPROVAL" if awaiting else "EVALUATED")
    if db.is_configured():
        store.update_job_status(job_id, status)
    return CalibrationOut(
        job_id=job_id,
        job_title=v.get("job_title", ""),
        status=status,
        spec_version=v.get("spec_version", 0),
        criteria=criteria,
        evidence=[e for e in v.get("evidence", []) if e.get("valid")],
        evaluations=ranked,
        tiers=tiers,
        risk_flags=v.get("risk_flags", []),
        change_proposal=v.get("change_proposal") or (payload or {}).get("proposal"),
        approval=v.get("approval"),
        feedback_notes=v.get("feedback_notes", []),
        workflow=workflow,
        clarified_requirement=v.get("clarified_requirement"),
        clarification_questions=v.get("clarification_questions"),
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "storage": "postgres" if db.is_configured() else "memory"}


@app.post("/jobs", response_model=CalibrationOut)
def create_job(body: JobIn) -> CalibrationOut:
    """建职位 + 候选人并启动校准。

    默认(旧行为兼容):澄清→标准→证据→评估一次跑完;
    require_spec_confirmation=true 时停在 spec_confirmation,
    首次标准确认前绝不抽证据/评估。
    """
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id, "title": body.title, "requirement": body.requirement,
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
        g.invoke({"feedback": body.text, "feedback_created_by": body.created_by}, _config(job_id))
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
        if body.action is None and body.criteria:
            action = "edit"
        else:
            action = body.action or ("accept" if body.approved else "reject")
        if action == "edit" and not body.criteria:
            raise HTTPException(422, "action=edit 必须提供 criteria")

        edited = None
        if action == "edit" and body.criteria:
            # 格式/风险校验必须在 Command(resume=...) 之前:报错不入 checkpoint。
            edited = wf_nodes.normalize_criteria([c.model_dump() for c in body.criteria])
            wf_nodes.check_criteria_risk(edited)

        decision = {
            "action": action,
            "approved": action in ("accept", "edit"),
            "criteria": edited,
            "note": body.note,
            "decided_by": body.decided_by or "hr",
        }
        g.invoke(Command(resume=decision), _config(job_id))
        return _snapshot(job_id)


@app.get("/calibrations/{job_id}/history", response_model=HistoryOut)
def get_history(job_id: str) -> HistoryOut:
    """决策日志:标准版本演进、反馈、提案审批结论、审计流水。"""
    if not db.is_configured():
        raise HTTPException(503, "历史查询需要配置数据库")
    if not _load_job(job_id):
        raise HTTPException(404, "职位不存在")
    return HistoryOut(job_id=job_id, **store.load_history(job_id))
