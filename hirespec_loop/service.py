"""The AI owner's six public operations; all publication stays outside this class."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

from .changes import ensure_unique_definitions, preview_changes
from .errors import InputError, StaleProposalError
from .evaluation import evaluator_hash, semantic_hash, verify_evaluation
from .llm import JSONClient, validated_call
from .models import (
    ChangeImpact, ChangeProposal, Clarification, Criterion, EvaluationDraft, EvaluationRecord,
    Feedback, ProposalOutput, ProposalStatus, ProposedChange, SpecDraft, SpecDraftOutput, SpecVersion,
)
from .prompts import CLARIFY, DRAFT, EVALUATE, PROPOSE
from .safety import require_safe, review_safety, rule_review


def criterion_texts(criteria: tuple[Criterion, ...]) -> tuple[str, ...]:
    return tuple("\n".join((c.definition, c.satisfied_when, c.not_satisfied_when, c.evidence_rule)) for c in criteria)


class HireSpecAI:
    def __init__(self, client: JSONClient, *, timeout_seconds: float = 30.0) -> None:
        if not client.cache_identity.strip() or timeout_seconds <= 0:
            raise InputError("模型身份不能为空，超时时间必须大于零。")
        self.client = client
        self.timeout_seconds = timeout_seconds
        self._identity = client.cache_identity
        self.evaluator_version = evaluator_hash(self._identity)

    def _check_identity(self) -> None:
        if self.client.cache_identity != self._identity:
            raise InputError("模型配置已改变，请创建新的 HireSpecAI 以隔离评估缓存。")

    @staticmethod
    def _require_text(text: str) -> None:
        if not isinstance(text, str) or not text.strip():
            raise InputError("输入文本为空或解析失败。")

    async def _safety(self, scope: str, texts: tuple[str, ...], context: dict | None = None):
        self._check_identity()
        return await review_safety(
            self.client, scope=scope, texts=texts, context=context,
            timeout_seconds=self.timeout_seconds,
        )

    async def clarify_requirement(self, requirement: str, answers: Mapping[str, str] | None = None) -> Clarification:
        self._require_text(requirement)
        answers = dict(answers or {})
        for key, value in answers.items():
            self._require_text(key)
            self._require_text(value)
        require_safe(await self._safety("requirement", (requirement, *answers.values())))
        return await validated_call(
            self.client, CLARIFY, {"requirement": requirement, "answers": answers},
            Clarification, timeout_seconds=self.timeout_seconds,
        )

    async def draft_spec(self, requirement: str, answers: Mapping[str, str]) -> SpecDraft:
        self._require_text(requirement)
        answers = dict(answers)
        for key, value in answers.items():
            self._require_text(key)
            self._require_text(value)
        require_safe(await self._safety("requirement", (requirement, *answers.values())))

        def check_draft(result: SpecDraftOutput) -> None:
            try:
                ensure_unique_definitions(tuple(
                    Criterion(criterion_id=f"C{i:02d}", **c.model_dump())
                    for i, c in enumerate(result.criteria, 1)
                ))
            except InputError:
                raise ValueError("重复标准") from None

        output = await validated_call(
            self.client, DRAFT, {"requirement": requirement, "answers": answers},
            SpecDraftOutput, timeout_seconds=self.timeout_seconds, validate_context=check_draft,
        )
        draft = SpecDraft(
            criteria=tuple(Criterion(criterion_id=f"C{i:02d}", **c.model_dump()) for i, c in enumerate(output.criteria, 1)),
            pending_assumptions=output.pending_assumptions,
        )
        await self.validate_spec(draft)
        return draft

    async def validate_spec(self, draft: SpecDraft) -> None:
        ensure_unique_definitions(draft.criteria)
        require_safe(await self._safety(
            "spec", criterion_texts(draft.criteria),
            {"criteria": [c.model_dump(mode="json") for c in draft.criteria]},
        ))

    async def evaluate_criterion(self, resume_text: str, criterion: Criterion) -> EvaluationDraft:
        self._require_text(resume_text)
        self._check_identity()
        require_safe(rule_review(criterion_texts((criterion,))))

        def match_id(result: EvaluationDraft) -> None:
            if result.criterion_id != criterion.criterion_id:
                raise ValueError("评估标准 ID 不一致")

        return await validated_call(
            self.client, EVALUATE,
            {"resume_text": resume_text, "criterion": criterion.semantic().model_dump(mode="json")},
            EvaluationDraft, timeout_seconds=self.timeout_seconds, validate_context=match_id,
        )

    verify_evaluation = staticmethod(verify_evaluation)

    async def propose_change(
        self, current: SpecVersion, feedback: Feedback, records: tuple[EvaluationRecord, ...],
        *, proposal_id: str | None = None,
    ) -> ChangeProposal:
        self._check_identity()
        if feedback.spec_version != current.version:
            raise StaleProposalError("反馈对应旧版本，请基于当前标准重新提交。")
        expected = {c.criterion_id: c for c in current.criteria}
        if len(records) != len(expected) or {r.evaluation.criterion_id for r in records} != set(expected):
            raise InputError("反馈必须绑定该候选人在当前版本的完整已验证评估。")
        for record in records:
            if (record.job_id != current.job_id or record.candidate_id != feedback.candidate_id
                or record.spec_version != current.version
                or record.criterion_semantic_hash != semantic_hash(expected[record.evaluation.criterion_id])
                or record.evaluator_version != self.evaluator_version):
                raise InputError("反馈评估的候选人、职位、版本或评估器不匹配。")
        if len({r.evaluation.resume_hash for r in records}) != 1:
            raise InputError("反馈评估混用了不同简历文本。")
        metadata = {
            "proposal_id": proposal_id or str(uuid4()), "feedback_id": feedback.feedback_id,
            "base_spec_version": current.version,
        }
        safety = await self._safety("feedback", (feedback.reason,))
        if safety.status != "SAFE":
            return self._safety_proposal(safety, metadata)

        def valid_targets(result: ProposalOutput) -> None:
            if result.status == ProposalStatus.PENDING_APPROVAL:
                try:
                    preview_changes(current, result.changes)
                except InputError:
                    raise ValueError("非法或无效修改目标") from None

        output = await validated_call(
            self.client, PROPOSE,
            {"spec": current.model_dump(mode="json"), "feedback": feedback.model_dump(mode="json"),
             "evaluations": [r.evaluation.model_dump(mode="json", exclude={"verification_issues"}) for r in records]},
            ProposalOutput, timeout_seconds=self.timeout_seconds, validate_context=valid_targets,
        )
        if output.status == ProposalStatus.PENDING_APPROVAL:
            impact = preview_changes(current, output.changes)
            safety = await self._safety("spec", criterion_texts(impact.criteria),
                                        {"criteria": [c.model_dump(mode="json") for c in impact.criteria]})
            if safety.status != "SAFE":
                return self._safety_proposal(safety, metadata)
        return ChangeProposal(**metadata, **output.model_dump())

    @staticmethod
    def _safety_proposal(safety, metadata: dict) -> ChangeProposal:
        blocked = safety.status == "BLOCKED"
        reason = "；".join(f.explanation for f in safety.findings)
        return ChangeProposal(
            **metadata,
            status=ProposalStatus.BLOCKED if blocked else ProposalStatus.NEEDS_CLARIFICATION,
            explanation="该反馈或提议尚不能进入正式 Hiring Spec。",
            blocked_reason=reason if blocked else None,
            clarification_question=None if blocked else safety.clarification_question,
        )

    async def validate_and_diff(
        self, current: SpecVersion, proposed_changes: tuple[ProposedChange, ...],
        *, base_spec_version: int,
    ) -> ChangeImpact:
        self._check_identity()
        if base_spec_version != current.version:
            raise StaleProposalError("提议基础版本已过期，请基于当前版本重新提议。")
        impact = preview_changes(current, proposed_changes)
        # Always inspect the whole prospective spec, including human edits.
        await self.validate_spec(SpecDraft(criteria=impact.criteria))
        return impact
