"""Local MVP recruitment routes. Explicit simulated AI and server-bound actor.

The local bearer token is not production identity/organization authorization.
The deployment owner must replace this dependency before multi-user deployment.
"""

from functools import lru_cache
import hmac
import os
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field, StrictBool

from app.recruiting_service import PersistentLoop
from app.recruiting_store import RecruitingStore, wire
from hirespec_loop.fixtures import OfflineDemoClient
from hirespec_loop.models import Contract, PositiveInt, ProposedChange, SpecDraft, Text
from hirespec_loop.service import HireSpecAI

router = APIRouter(prefix="/api/hiring", tags=["HireSpec Loop（本地招聘闭环）"])
bearer = HTTPBearer(auto_error=False)


def actor(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    expected = os.getenv("HIRESPEC_API_TOKEN", "")
    if not expected:
        raise HTTPException(503, detail={"code": "HIRING_NOT_ENABLED", "message": "请用 local_backend.py --recruiting-demo 启用本地招聘接口。"})
    if not credentials or credentials.scheme.lower() != "bearer" or not hmac.compare_digest(credentials.credentials, expected):
        raise HTTPException(401, detail={"code": "UNAUTHORIZED", "message": "请在接口文档 Authorize 中填写本地访问令牌。"})
    return os.getenv("HIRESPEC_LOCAL_ACTOR", "manager-local")


@lru_cache
def get_ai():
    mode = os.getenv("HIRESPEC_AI_MODE", "unconfigured")
    if mode != "offline_demo":
        raise HTTPException(503, detail={"code": "AI_NOT_CONFIGURED", "message": "尚未接入真实模型；模拟演示需明确启用 offline_demo。"})
    return HireSpecAI(OfflineDemoClient())


def get_loop(ai: Annotated[HireSpecAI, Depends(get_ai)]):
    return PersistentLoop(RecruitingStore(), ai, model_mode=os.getenv("HIRESPEC_AI_MODE", "unconfigured"))


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
    mode = os.getenv("HIRESPEC_AI_MODE", "unconfigured")
    return {"model_mode": mode, "simulated": mode == "offline_demo", "actor": user,
            "notice": "真实 PostgreSQL 持久化；模型回复为预置模拟数据，未验证真实模型能力。"}


@router.post("/jobs", status_code=201)
def create_job(body: JobIn, user: Actor):
    return RecruitingStore().create_job(body.title, body.requirement, user, os.getenv("HIRESPEC_AI_MODE", "unconfigured"))


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
