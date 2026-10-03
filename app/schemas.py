"""API 契约(前后端并行开发依据)。"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Verdict = Literal["推荐", "待核实", "不推荐", "否决"]
CriterionType = Literal["MUST_HAVE", "NICE_TO_HAVE", "VETO"]


class CandidateIn(BaseModel):
    name: str
    resume_text: str = Field(min_length=1)


class JobIn(BaseModel):
    title: str
    requirement: str = Field(min_length=1)
    candidates: list[CandidateIn] = []
    require_spec_confirmation: bool = False


class FeedbackIn(BaseModel):
    text: str = Field(min_length=1)
    candidate_id: str | None = None       # 可针对某人,也可整体
    created_by: str | None = None


class CriterionIn(BaseModel):
    id: str | None = None
    type: CriterionType = "NICE_TO_HAVE"
    text: str = Field(min_length=1)
    weight: int = Field(default=2, ge=1, le=3)

    @field_validator("text", "id")
    @classmethod
    def nonblank(cls, value):
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("标准文本和 ID 不得为空")
        return value


class SpecConfirmIn(BaseModel):
    criteria: list[CriterionIn] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [c.id or f"c{i+1}" for i, c in enumerate(self.criteria)]
        if len(ids) != len(set(ids)):
            raise ValueError("标准 ID 必须唯一（含自动生成的 ID）")
        return self


class WorkflowOut(BaseModel):
    next_nodes: list[str] = []
    completed_nodes: list[str] = []
    waiting_for: Literal["spec_confirmation", "human_approval"] | None = None
    model_mode: Literal["offline_fallback", "live"]
    checkpointer: Literal["memory", "postgres"]
    history: list[dict[str, Any]] = []


class ApprovalIn(BaseModel):
    """Accept / Edit / Reject。

    - accept: 采纳 AI 提案
    - edit:   用 criteria 字段给出的完整标准集替换(忽略提案的 add/remove)
    - reject: 驳回
    兼容旧客户端:只传 approved=true/false 时等价于 accept / reject。
    """
    action: Literal["accept", "edit", "reject"] | None = None
    approved: bool = True
    criteria: list[CriterionIn] | None = None
    note: str = ""
    decided_by: str | None = None


class EvaluationOut(BaseModel):
    candidate_id: str
    name: str = ""
    scores: dict[str, int | None]         # 2=满足 0=有反证 None=无证据
    verdict: Verdict
    tradeoff: dict[str, Any] = {}         # {strengths:[], gaps:[], veto_hit:[]}


class CalibrationOut(BaseModel):
    job_id: str
    job_title: str = ""
    status: str                            # EVALUATED / AWAITING_APPROVAL / AWAITING_SPEC_CONFIRMATION
    spec_version: int
    criteria: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    evaluations: list[EvaluationOut]       # 已按 档位 → 加权覆盖度 排序
    tiers: dict[str, int] = {}             # 各档人数
    risk_flags: list[str] = []
    change_proposal: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    feedback_notes: list[str] = []
    workflow: WorkflowOut
    clarified_requirement: str | None = None
    clarification_questions: list[str] | None = None


class HistoryOut(BaseModel):
    """决策日志页:可追溯的标准演进与审批记录。"""
    job_id: str
    spec_versions: list[dict[str, Any]] = []
    feedback: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
