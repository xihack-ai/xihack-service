"""Local MVP recruitment routes. Real model via shared LLM_* config; server-bound actor.

The local bearer token is not production identity/organization authorization.
The deployment owner must replace this dependency before multi-user deployment.
"""

from functools import lru_cache
import hmac
import json
import os
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field, StrictBool

from app.recruiting_service import PersistentLoop
from app.recruiting_store import RecruitingStore, wire
from hirespec_loop.llm import CallableJSONClient
from hirespec_loop.models import Contract, PositiveInt, ProposedChange, SpecDraft, Text
from hirespec_loop.service import HireSpecAI

router = APIRouter(prefix="/api/hiring", tags=["HireSpec Loop（本地招聘闭环）"])
bearer = HTTPBearer(auto_error=False)


def actor(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    expected = os.getenv("HIRESPEC_API_TOKEN", "")
    if not expected:
        raise HTTPException(503, detail={"code": "HIRING_NOT_ENABLED", "message": "招聘接口未启用：请设置 HIRESPEC_API_TOKEN。"})
    if not credentials or credentials.scheme.lower() != "bearer" or not hmac.compare_digest(credentials.credentials, expected):
        raise HTTPException(401, detail={"code": "UNAUTHORIZED", "message": "请在接口文档 Authorize 中填写本地访问令牌。"})
    return os.getenv("HIRESPEC_LOCAL_ACTOR", "manager-local")


def llm_configured() -> bool:
    return bool(os.getenv("LLM_API_KEY"))


def ai_model_mode() -> str:
    return "live" if llm_configured() else "unconfigured"


@lru_cache
def get_ai():
    """真实模型适配器:与 LangGraph 主接口共用 LLM_BASE_URL/LLM_API_KEY/LLM_MODEL。"""
    if not llm_configured():
        raise HTTPException(503, detail={"code": "AI_NOT_CONFIGURED", "message": "未配置 LLM_API_KEY，招聘闭环 AI 功能不可用。"})
    base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")

    async def generate_json(system_prompt: str, request: dict, schema: dict):
        messages = [
            {"role": "system", "content": system_prompt
             + "\n\n只输出符合以下 JSON Schema 的 JSON 对象，不要包裹代码块：\n"
             + json.dumps(schema, ensure_ascii=False)},
            {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
        ]
        payload = {"model": model, "messages": messages, "temperature": 0}
        if urlparse(base).hostname == "api.deepseek.com":
            payload["thinking"] = {"type": "disabled"}
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['LLM_API_KEY']}"},
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    client = CallableJSONClient(
        generate_json, provider="openai-compatible",
        model=model, settings={"base_host": urlparse(base).hostname, "temperature": 0},
    )
    return HireSpecAI(client)


def get_loop(ai: Annotated[HireSpecAI, Depends(get_ai)]):
    return PersistentLoop(RecruitingStore(), ai, model_mode=ai_model_mode())


Actor = Annotated[str, Depends(actor)]
Loop = Annotated[PersistentLoop, Depends(get_loop)]


class JobIn(Contract):
    title: Annotated[Text, Field(max_length=200)]
    requirement: Annotated[Text, Field(max_length=5000)]


class AnswersIn(Contract):
    answers: dict[Text, Annotated[Text, Field(max_length=2000)]] = Field(default_factory=dict, max_length=10)


class CandidateIn(Contract):
    name: Annotated[Text, Field(max_length=200)]
    resume_text: Annotated[Text, Field(max_length=100000)]
    source_file: Annotated[Text, Field(max_length=500)] | None = None
    file_key: Annotated[Text, Field(max_length=1000)] | None = None
    original_filename: Annotated[Text, Field(max_length=500)] | None = None
    mime_type: Annotated[Text, Field(max_length=200)] | None = None
    file_size: Annotated[int, Field(ge=0, le=50_000_000)] | None = None
    parsed_data: dict = Field(default_factory=dict)
    parse_status: Literal["NOT_REQUESTED", "PENDING", "PROCESSING", "COMPLETED", "FAILED"] = "NOT_REQUESTED"
    parser_version: Annotated[Text, Field(max_length=100)] | None = None


class InitialApprovalIn(Contract):
    draft_id: UUID


class FeedbackIn(Contract):
    candidate_id: UUID
    spec_version: PositiveInt
    decision: Literal["YES", "NO"]
    reason: Annotated[Text, Field(max_length=2000)]


class EditIn(Contract):
    changes: Annotated[tuple[ProposedChange, ...], Field(min_length=1, max_length=20)]


class LabelIn(Contract):
    label: StrictBool
    split: Literal["calibration", "test"]


@router.get("/mode")
def mode(user: Actor):
    return {"model_mode": ai_model_mode(), "simulated": False, "actor": user,
            "notice": "模型复用主接口 LLM_* 配置；未配置 LLM_API_KEY 时 AI 端点返回 503。"}


@router.post("/jobs", status_code=201)
def create_job(body: JobIn, user: Actor):
    return RecruitingStore().create_job(body.title, body.requirement, user, ai_model_mode())


@router.get("/jobs/{job_id}")
def get_job(job_id: UUID, user: Actor):
    return RecruitingStore().get_job(str(job_id), user)


@router.post("/jobs/{job_id}/clarify")
async def clarify(job_id: UUID, body: AnswersIn, user: Actor, loop: Loop):
    return wire(await loop.clarify(str(job_id), user, body.answers))


@router.post("/jobs/{job_id}/drafts")
async def draft(job_id: UUID, body: AnswersIn, user: Actor, loop: Loop):
    return await loop.draft(str(job_id), user, body.answers)


@router.put("/jobs/{job_id}/draft")
async def manual_draft(job_id: UUID, body: SpecDraft, user: Actor, loop: Loop):
    return await loop.manual_draft(str(job_id), user, body)


@router.post("/jobs/{job_id}/candidates", status_code=201)
def candidate(job_id: UUID, body: CandidateIn, user: Actor):
    return RecruitingStore().add_candidate(
            str(job_id), user, body.name, body.resume_text, body.source_file,
            file_key=body.file_key, original_filename=body.original_filename,
            mime_type=body.mime_type, file_size=body.file_size,
            parsed_data=body.parsed_data, parse_status=body.parse_status,
            parser_version=body.parser_version,
        )


@router.post("/jobs/{job_id}/spec/approve")
async def initial(job_id: UUID, body: InitialApprovalIn, user: Actor, loop: Loop):
    return await loop.approve_initial(str(job_id), user, str(body.draft_id))


@router.post("/jobs/{job_id}/recompute")
async def recompute(job_id: UUID, user: Actor, loop: Loop):
    return wire(await loop.recompute(str(job_id), user))


@router.post("/jobs/{job_id}/feedback")
async def feedback(job_id: UUID, body: FeedbackIn, user: Actor, loop: Loop):
    return await loop.feedback(str(job_id), user, str(body.candidate_id), body.decision, body.reason, body.spec_version)


@router.post("/jobs/{job_id}/feedback/{feedback_id}/propose")
async def retry_proposal(job_id: UUID, feedback_id: UUID, user: Actor, loop: Loop):
    return await loop.propose(str(job_id), user, str(feedback_id))


@router.get("/jobs/{job_id}/proposals/{proposal_id}")
def proposal(job_id: UUID, proposal_id: UUID, user: Actor):
    store = RecruitingStore()
    row, _ = store.proposal_context(str(job_id), user, str(proposal_id))
    return store.proposal_view(row)


@router.put("/jobs/{job_id}/proposals/{proposal_id}")
async def edit(job_id: UUID, proposal_id: UUID, body: EditIn, user: Actor, loop: Loop):
    return await loop.edit(str(job_id), user, str(proposal_id), body.changes)


@router.post("/jobs/{job_id}/proposals/{proposal_id}/accept")
async def accept(job_id: UUID, proposal_id: UUID, user: Actor, loop: Loop):
    return await loop.accept(str(job_id), user, str(proposal_id))


@router.post("/jobs/{job_id}/proposals/{proposal_id}/reject")
def reject(job_id: UUID, proposal_id: UUID, user: Actor):
    return wire(RecruitingStore().reject(str(job_id), user, str(proposal_id)))


@router.put("/jobs/{job_id}/candidates/{candidate_id}/label")
def label(job_id: UUID, candidate_id: UUID, body: LabelIn, user: Actor):
    return RecruitingStore().label(str(job_id), user, str(candidate_id), body.label, body.split)
