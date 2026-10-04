"""Defense in depth: explicit screening rules plus a fail-closed semantic review.

These rules are an MVP product policy, not a legal-compliance guarantee.
They intentionally detect conditions, not isolated demographic words.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .errors import ClarificationRequired, PolicyError
from .llm import JSONClient, validated_call
from .models import SafetyAssessment, SafetyFinding
from .prompts import SAFETY


_RULES = (
    # Explicit candidate age thresholds/prejudices; user-product demographics are allowed.
    (r"(?:候选人|应聘者|人选|这个人|他|她).{0,16}(?:年龄|年纪).{0,10}(?:大|小|老|年轻)", "年龄筛选"),
    (r"(?:年龄|年纪).{0,8}(?:偏大|太大|有点大|大了|太小|年轻|不超过|不能超过|小于|大于|至少|以下|以上|[<>≤≥])", "年龄筛选"),
    (r"(?:只招|只要|不要|拒绝|排除|优先).{0,6}(?:年轻人|老年人|年长|年轻的候选人)", "年龄筛选"),
    (r"(?:招|找|要).{0,4}年轻(?:有冲劲|一点|点|的候选人)", "年龄筛选"),
    (r"(?:年龄\s*[:：]?\s*\d|\d{2}\s*岁\s*(?:以下|以上|以内|左右)|\d{2}\s*[-~至]\s*\d{2}\s*岁)", "年龄门槛"),
    (r"(?:女性|男性|女生|男生|女人|男人).{0,8}(?:不适合|不合适|不能胜任|不考虑|优先|更适合)", "性别筛选"),
    (r"(?:只招|只要|限招|限|不要|拒绝|排除|必须是|要求是|优先).{0,5}(?:女性|男性|女生|男生)", "性别筛选"),
    (r"(?:性别).{0,6}(?:男|女)", "性别门槛"),
    (r"(?:候选人|应聘者|人选|他|她).{0,15}(?:怀孕|备孕|已婚|未婚|已育|未育|生育|长相|颜值)", "婚育或外貌筛选"),
    (r"(?:只招|只要|不要|拒绝|排除|必须|要求|优先).{0,8}(?:已婚|未婚|已育|未育|怀孕|备孕|残疾|残障|汉族)", "敏感属性门槛"),
    (r"(?:只招|只要|限招|必须|要求|排除).{0,8}(?:某民族|某宗教|基督徒|穆斯林|某种族)", "民族、宗教或种族筛选"),
    (r"\b(?:age\s*(?:[<>≤≥]|under|over|below|above)|(?:under|over)\s+\d{2}\s*(?:years?\s*old)?)", "年龄筛选"),
    (r"\b(?:women|females?|men|males?)\s+(?:are\s+)?(?:unsuitable|not\s+suitable|not\s+fit|preferred)", "性别筛选"),
    (r"\b(?:only\s+hire|no|exclude|reject|prefer)\s+(?:women|men|females?|males?|pregnant|married|disabled)\b", "敏感属性筛选"),
)


def rule_review(texts: tuple[str, ...]) -> SafetyAssessment:
    findings: list[SafetyFinding] = []
    seen: set[str] = set()
    for text in texts:
        normalized = unicodedata.normalize("NFKC", text).casefold()
        for pattern, label in _RULES:
            if label not in seen and re.search(pattern, normalized):
                seen.add(label)
                findings.append(SafetyFinding(
                    code="NON_JOB_RELATED" if label == "学校个人喜好" else "SENSITIVE_ATTRIBUTE",
                    explanation=f"检测到{label}；请重新提交岗位相关的职责或能力要求。",
                ))
    return SafetyAssessment(status="BLOCKED", findings=tuple(findings)) if findings else SafetyAssessment(status="SAFE")


async def review_safety(
    client: JSONClient,
    *,
    scope: str,
    texts: tuple[str, ...],
    context: dict[str, Any] | None = None,
    timeout_seconds: float = 30.0,
) -> SafetyAssessment:
    rules = rule_review(texts)
    if rules.status == "BLOCKED":
        return rules
    # Every rule-pass is semantically reviewed; failures propagate, never fail open.
    return await validated_call(
        client, SAFETY, {"scope": scope, "texts": texts, "context": context or {}},
        SafetyAssessment, timeout_seconds=timeout_seconds,
    )


def require_safe(assessment: SafetyAssessment) -> None:
    if assessment.status == "BLOCKED":
        raise PolicyError("；".join(f.explanation for f in assessment.findings))
    if assessment.status == "NEEDS_CLARIFICATION":
        raise ClarificationRequired(assessment.clarification_question or "请澄清岗位相关条件。")
