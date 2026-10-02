"""FastAPI 入口与路由。"""

from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # 尽早加载 .env,供 kb/llm 读取

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
import psycopg  # noqa: E402

from app import explore, kb as kb_mod  # noqa: E402
from app import llm  # noqa: E402
from app import database  # noqa: E402
from app.recruiting_api import router as recruiting_router  # noqa: E402
from hirespec_loop.errors import HireSpecError  # noqa: E402
from app.schemas import (  # noqa: E402
    ActionIn,
    CreateSessionIn,
    SceneSummary,
    SessionOut,
)

ROOT = Path(__file__).resolve().parent.parent

app = FastAPI(title="xihack", version="0.2.0")
app.include_router(recruiting_router)


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


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "llm_configured": llm.is_configured(),
        "replay_only": llm.replay_only(),
    }


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


@app.get("/scenes", response_model=list[SceneSummary])
def list_scenes() -> list[SceneSummary]:
    return [
        SceneSummary(id=s["id"], mode=s["mode"], title=s["title"], intro=s["intro"])
        for s in kb_mod.get_kb().list_scenes()
    ]


@app.get("/scenes/{scene_id}")
def get_scene(scene_id: str) -> dict:
    scene = kb_mod.get_scene(scene_id)
    if scene is None:
        raise HTTPException(status_code=404, detail="scene not found")
    return scene


@app.post("/sessions", response_model=SessionOut)
def create_session(req: CreateSessionIn) -> SessionOut:
    kb = kb_mod.get_kb()
    try:
        session_id = explore.create_session(req.scene_id, kb)
    except explore.SessionNotFound:
        raise HTTPException(status_code=404, detail="scene not found")
    except explore.UnsupportedMode as e:
        raise HTTPException(status_code=501, detail=f"模式尚未实现: {e}")
    return explore.to_out(session_id, explore.get_session(session_id), kb)


@app.get("/sessions/{session_id}", response_model=SessionOut)
def get_session(session_id: str) -> SessionOut:
    kb = kb_mod.get_kb()
    try:
        return explore.to_out(session_id, explore.get_session(session_id), kb)
    except explore.SessionNotFound:
        raise HTTPException(status_code=404, detail="session not found")


@app.post("/sessions/{session_id}/actions", response_model=SessionOut)
def do_action(session_id: str, req: ActionIn) -> SessionOut:
    try:
        return explore.do_action(session_id, req.action, req.model_dump(exclude_none=True))
    except explore.SessionNotFound:
        raise HTTPException(status_code=404, detail="session not found")
    except explore.ActionError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/kb/search")
def kb_search(q: str, scene_id: str | None = None, top_k: int = 5) -> list[dict]:
    kb = kb_mod.get_kb()
    claim_ids = None
    if scene_id:
        claim_ids = [c["id"] for c in kb.scene_claims(scene_id)]
    return kb.search_claims(q, claim_ids=claim_ids, top_k=top_k)


# 静态前端放在最后挂载,避免遮挡 API 路由
_web_dir = ROOT / "web"
if _web_dir.is_dir():
    app.mount("/", StaticFiles(directory=_web_dir, html=True), name="web")
