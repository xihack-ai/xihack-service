from __future__ import annotations

import hashlib
import json

from .errors import InputError
from .models import (
    Criterion, EvaluationDraft, SemanticCriterion, Verdict, VerificationIssue,
    VerifiedEvaluation, VerifiedEvidence,
)
from .prompts import EVALUATE


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def semantic_hash(criterion: Criterion | SemanticCriterion) -> str:
    semantic = criterion.semantic() if isinstance(criterion, Criterion) else criterion
    body = semantic.model_dump(exclude={"criterion_id"}, mode="json")
    return text_hash(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def evaluator_hash(cache_identity: str) -> str:
    # Changes to schema, prompt or verifier invalidate previous results.
    return text_hash(json.dumps({
        "identity": cache_identity,
        "prompt": EVALUATE,
        "schema": EvaluationDraft.model_json_schema(),
        "verifier": "exact-quotes-utf16-v1",
    }, sort_keys=True, ensure_ascii=False))


def verify_evaluation(
    resume_text: str,
    draft: EvaluationDraft,
    *,
    expected_criterion_id: str | None = None,
) -> VerifiedEvaluation:
    if not isinstance(resume_text, str) or not resume_text.strip():
        raise InputError("简历文本为空或解析失败，请重新提供完整文本。")
    if expected_criterion_id is not None and draft.criterion_id != expected_criterion_id:
        raise InputError("评估标准 ID 与请求不一致。")
    issues: list[VerificationIssue] = []
    evidence: list[VerifiedEvidence] = []
    for index, item in enumerate(draft.evidence):
        if not item.quote.strip():
            issues.append(VerificationIssue(evidence_index=index, code="EMPTY_QUOTE"))
            continue
        start = resume_text.find(item.quote)
        if start < 0:
            issues.append(VerificationIssue(evidence_index=index, code="QUOTE_NOT_FOUND"))
            continue
        end = start + len(item.quote)
        evidence.append(VerifiedEvidence(
            quote=item.quote, start=start, end=end,
            start_utf16=len(resume_text[:start].encode("utf-16-le")) // 2,
            end_utf16=len(resume_text[:end].encode("utf-16-le")) // 2,
        ))
    common = {"criterion_id": draft.criterion_id, "resume_hash": text_hash(resume_text)}
    if issues:
        return VerifiedEvaluation(
            **common, verdict=Verdict.NO_EVIDENCE, evidence=(),
            reason="引用未通过原文逐字校验，结论已降级为无证据。",
            uncertainty="模型引用无效，不能据此判断该标准。",
            followup_question="请提供能够证明该条标准的具体经历。",
            verification_issues=tuple(issues),
        )
    return VerifiedEvaluation(
        **common, verdict=draft.verdict, evidence=tuple(evidence), reason=draft.reason,
        uncertainty=draft.uncertainty, followup_question=draft.followup_question,
    )
