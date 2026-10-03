"""招聘标准校准工作流的图状态定义。

原则:LangGraph 状态里只放流程状态(标准、评估结果、待审批变更),
用户/职位/简历等业务数据由 FastAPI + PG 负责,这里只存本次校准所需的快照。
"""

from typing import Any, Literal, TypedDict

CriterionType = Literal["MUST_HAVE", "NICE_TO_HAVE", "VETO"]
Verdict = Literal["推荐", "待核实", "不推荐", "否决"]


class Criterion(TypedDict):
    id: str
    type: CriterionType        # 硬性要求 / 加分项 / 一票否决
    text: str
    weight: int                # 仅用于档内排序与 Trade-off 展示,不参与分档


class Evidence(TypedDict):
    candidate_id: str
    criterion_id: str
    quote: str
    stance: Literal["support", "against"]   # 简历原文支持还是否定该条标准
    valid: bool


class Tradeoff(TypedDict):
    strengths: list[dict[str, Any]]   # [{criterion_id, text, quote, type}]
    gaps: list[dict[str, Any]]        # [{criterion_id, text, reason}]
    veto_hit: list[str]               # 命中的否决项 id


class Evaluation(TypedDict):
    candidate_id: str
    scores: dict[str, int | None]     # 2=满足 0=不满足 None=无证据(不臆断)
    judgments: dict[str, str]
    evidence_ids: list[str]
    verdict: Verdict
    tier_reason: str
    tradeoff: Tradeoff


class CalibrationState(TypedDict, total=False):
    job_id: str
    job_title: str
    requirement: str
    clarified_requirement: str

    criteria: list[Criterion]
    spec_version: int
    require_spec_confirmation: bool     # 外部注入:true 时首次标准需人工确认
    spec_confirmation: dict[str, Any] | None  # {"confirmed","note","decided_by","edited"}

    candidates: list[dict]          # {id, name, resume_text}
    evidence: list[Evidence]
    evaluations: list[Evaluation]
    evaluation_requested: bool    # Explicit evaluation of newly imported candidates.
    defer_initial_evaluation: bool # File imports are evaluated via the explicit button.

    feedback: str                   # 外部注入:HR 反馈原文;消费后置空
    feedback_id: str | None         # 落库后的 feedback.id,供提案引用
    feedback_created_by: str | None
    feedback_notes: list[str]
    risk_flags: list[str]

    change_proposal: dict[str, Any] | None   # {"reason","add":[Criterion],"remove":[criterion_id]}
    approval: dict[str, Any] | None          # {"action","approved","final_proposal","note"}
    reeval_candidates: list[str]             # 待重评候选人;空=无人
    reeval_criteria: list[str] | None        # None=全量标准;[]=只重排不抽证;[...]=只抽这几条

    clarified_requirement: str
    clarification_questions: list[str]
    clarification_responses: dict[str, dict[str, Any]]
    clarification_active_index: str | None
    workflow_history: list[dict[str, Any]]   # 当前图状态的操作记录(节点级轨迹)
    rejected_proposals: list[str]
    allocated_criterion_ids: list[str]
    recomputation: dict[str, Any]
    feedback_status: str | None
    feedback_question: str | None
    approval_draft: list[Criterion] | None
    requirement_revision: str | None  # Awaiting human approval; never overwrite the active intent early.
    requirement_clarification_draft: dict[str, Any] | None
