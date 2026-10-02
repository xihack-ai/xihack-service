"""Immutable contracts. IDs, versions and provenance are assigned by code."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator


def nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("必须包含非空白文字")
    return value  # Never normalize or strip evidence/source text.


def canonical_criterion_id(value: str) -> str:
    number = int(value[1:])
    if number < 1 or value != f"C{number:02d}":
        raise ValueError("标准 ID 必须规范化，例如 C01、C02、C100")
    return value


Text = Annotated[StrictStr, Field(min_length=1), AfterValidator(nonblank)]
CriterionID = Annotated[StrictStr, Field(pattern=r"^C[0-9]{2,}$"), AfterValidator(canonical_criterion_id)]
PositiveInt = Annotated[StrictInt, Field(ge=1)]


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, validate_default=True, revalidate_instances="always",
        protected_namespaces=("model_validate", "model_dump"),
    )


class Category(str, Enum):
    MUST_HAVE = "MUST_HAVE"
    NICE_TO_HAVE = "NICE_TO_HAVE"
    VETO = "VETO"


class Verdict(str, Enum):
    SATISFIED = "SATISFIED"
    NOT_SATISFIED = "NOT_SATISFIED"
    NO_EVIDENCE = "NO_EVIDENCE"


class SemanticCriterion(Contract):
    criterion_id: CriterionID
    definition: Text
    satisfied_when: Text
    not_satisfied_when: Text
    evidence_rule: Text


class CriterionBody(Contract):
    category: Category
    definition: Text
    satisfied_when: Text
    not_satisfied_when: Text
    evidence_rule: Text


class Criterion(CriterionBody):
    criterion_id: CriterionID

    def semantic(self) -> SemanticCriterion:
        return SemanticCriterion.model_validate(self.model_dump(exclude={"category"}))


class SpecDraftOutput(Contract):
    # LLMs do not allocate IDs.
    criteria: Annotated[tuple[CriterionBody, ...], Field(min_length=1, max_length=20)]
    pending_assumptions: tuple[Text, ...] = ()


class SpecDraft(Contract):
    criteria: Annotated[tuple[Criterion, ...], Field(min_length=1, max_length=20)]
    pending_assumptions: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def unique_ids(self) -> SpecDraft:
        if len({c.criterion_id for c in self.criteria}) != len(self.criteria):
            raise ValueError("标准 ID 不可重复")
        return self


class SpecVersion(Contract):
    job_id: Text
    version: PositiveInt
    criteria: Annotated[tuple[Criterion, ...], Field(min_length=1, max_length=20)]
    created_at: datetime
    approved_by: Text
    next_criterion_number: PositiveInt

    @model_validator(mode="after")
    def validate_metadata(self) -> SpecVersion:
        SpecDraft(criteria=self.criteria)
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at 必须带时区")
        if self.next_criterion_number <= max(int(c.criterion_id[1:]) for c in self.criteria):
            raise ValueError("next_criterion_number 必须大于历史已分配的 ID")
        return self


class Clarification(Contract):
    questions: Annotated[tuple[Text, ...], Field(max_length=3)]
    pending_assumptions: tuple[Text, ...] = ()


class EvidenceDraft(Contract):
    quote: StrictStr  # Empty quotes are downgraded by verification, never accepted.


class EvaluationDraft(Contract):
    criterion_id: CriterionID
    verdict: Verdict
    evidence: Annotated[tuple[EvidenceDraft, ...], Field(max_length=3)]
    reason: Text
    uncertainty: Text | None = None
    followup_question: Text | None = None

    @model_validator(mode="after")
    def require_evidence(self) -> EvaluationDraft:
        if self.verdict != Verdict.NO_EVIDENCE and not self.evidence:
            raise ValueError("满足或不满足的判断必须附带引用")
        return self


class VerifiedEvidence(Contract):
    quote: Text
    start: Annotated[StrictInt, Field(ge=0)]
    end: Annotated[StrictInt, Field(ge=1)]
    start_utf16: Annotated[StrictInt, Field(ge=0)]
    end_utf16: Annotated[StrictInt, Field(ge=1)]

    @model_validator(mode="after")
    def valid_offsets(self) -> VerifiedEvidence:
        if self.end <= self.start or self.end_utf16 <= self.start_utf16:
            raise ValueError("引用区间必须非空，end 为不包含的右端点")
        return self


class VerificationIssue(Contract):
    evidence_index: Annotated[StrictInt, Field(ge=0)]
    code: Literal["EMPTY_QUOTE", "QUOTE_NOT_FOUND"]


class VerifiedEvaluation(Contract):
    criterion_id: CriterionID
    resume_hash: Text
    verdict: Verdict
    evidence: Annotated[tuple[VerifiedEvidence, ...], Field(max_length=3)]
    reason: Text
    uncertainty: Text | None = None
    followup_question: Text | None = None
    verification_issues: tuple[VerificationIssue, ...] = ()

    @model_validator(mode="after")
    def require_verified_evidence(self) -> VerifiedEvaluation:
        if self.verdict != Verdict.NO_EVIDENCE and not self.evidence:
            raise ValueError("结论必须附带已验证证据")
        if self.verification_issues and (self.verdict != Verdict.NO_EVIDENCE or self.evidence):
            raise ValueError("引用失败必须降级并清空证据")
        return self


class Candidate(Contract):
    candidate_id: Text
    resume_text: Text


class EvaluationRecord(Contract):
    job_id: Text
    candidate_id: Text
    spec_version: PositiveInt
    criterion_semantic_hash: Text
    evaluator_version: Text
    evaluation: VerifiedEvaluation


class Feedback(Contract):
    feedback_id: Text
    candidate_id: Text
    spec_version: PositiveInt
    decision: Literal["YES", "NO"]
    reason: Text
    reviewer_id: Text


class ProposalStatus(str, Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    BLOCKED = "BLOCKED"
    NO_CHANGE = "NO_CHANGE"


class ChangeOperation(str, Enum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    REMOVE = "REMOVE"


class ProposedChange(Contract):
    operation: ChangeOperation
    criterion_id: CriterionID | None = None
    after: CriterionBody | None = None

    @model_validator(mode="after")
    def operation_shape(self) -> ProposedChange:
        if self.operation == ChangeOperation.ADD:
            if self.criterion_id is not None or self.after is None:
                raise ValueError("ADD 不指定 ID，且必须包含完整 after")
        elif self.operation == ChangeOperation.UPDATE:
            if self.criterion_id is None or self.after is None:
                raise ValueError("UPDATE 需要现有 ID 和完整 after")
        elif self.criterion_id is None or self.after is not None:
            raise ValueError("REMOVE 只指定现有 ID，不包含 after")
        return self


class ProposalOutput(Contract):
    status: ProposalStatus
    changes: tuple[ProposedChange, ...] = ()
    explanation: Text
    blocked_reason: Text | None = None
    clarification_question: Text | None = None

    @model_validator(mode="after")
    def status_shape(self) -> ProposalOutput:
        if self.status == ProposalStatus.PENDING_APPROVAL:
            if not self.changes or self.blocked_reason is not None or self.clarification_question is not None:
                raise ValueError("待审批提议必须有修改，不可同时标为阻止或待澄清")
        elif self.changes:
            raise ValueError("非待审批状态不得携带可执行修改")
        if self.status == ProposalStatus.BLOCKED and not self.blocked_reason:
            raise ValueError("BLOCKED 必须给出原因")
        if self.status == ProposalStatus.NEEDS_CLARIFICATION and not self.clarification_question:
            raise ValueError("待澄清必须附带问题")
        return self


class ChangeProposal(ProposalOutput):
    proposal_id: Text
    feedback_id: Text
    base_spec_version: PositiveInt


class SafetyFinding(Contract):
    code: Literal["SENSITIVE_ATTRIBUTE", "NON_JOB_RELATED", "NOT_ATOMIC", "NOT_EVALUABLE", "DUPLICATE"]
    explanation: Text


class SafetyAssessment(Contract):
    status: Literal["SAFE", "BLOCKED", "NEEDS_CLARIFICATION"]
    findings: tuple[SafetyFinding, ...] = ()
    clarification_question: Text | None = None

    @model_validator(mode="after")
    def check_status(self) -> SafetyAssessment:
        if self.status == "SAFE" and (self.findings or self.clarification_question):
            raise ValueError("SAFE 不得附带未解决的问题")
        if self.status != "SAFE" and not self.findings:
            raise ValueError("未通过检查必须说明原因")
        if self.status == "NEEDS_CLARIFICATION" and not self.clarification_question:
            raise ValueError("待澄清必须附带问题")
        return self


class CriterionDiff(Contract):
    criterion_id: CriterionID
    operation: ChangeOperation
    before: Criterion | None
    after: Criterion | None
    evaluation_changed: bool


class ChangeImpact(Contract):
    base_spec_version: PositiveInt
    criteria: tuple[Criterion, ...]
    next_criterion_number: PositiveInt
    diff: tuple[CriterionDiff, ...]
    reevaluate_ids: tuple[CriterionID, ...]
    reuse_ids: tuple[CriterionID, ...]
    removed_ids: tuple[CriterionID, ...]


class RankingEntry(Contract):
    candidate_id: Text
    tier: Literal["QUALIFIED", "NEEDS_VERIFICATION", "REQUIREMENT_CONFLICT", "VETO_TRIGGERED"]
    nice_to_have_met: Annotated[StrictInt, Field(ge=0)]
    explanations: tuple[Text, ...]


class RankingSnapshot(Contract):
    job_id: Text
    spec_version: PositiveInt
    entries: tuple[RankingEntry, ...]
    records: tuple[EvaluationRecord, ...]
    model_evaluations: Annotated[StrictInt, Field(ge=0)]
    cache_hits: Annotated[StrictInt, Field(ge=0)]
    elapsed_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]


class RecomputeState(Contract):
    status: Literal["IDLE", "RUNNING", "READY", "FAILED"] = "IDLE"
    target_version: PositiveInt | None = None
    visible_version: PositiveInt | None = None
    error_code: Text | None = None


class ApprovalReceipt(Contract):
    proposal_id: Text
    action: Literal["ACCEPTED", "REJECTED"]
    reviewer_id: Text
    result_version: PositiveInt
