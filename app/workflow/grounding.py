"""Ground model standards in requirements, and verdicts in literal resume quotes."""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

NUMBER = r"(?:\d+(?:\.\d+)?|[一二三四五六七八九十两]+)"
DURATION = re.compile(rf"({NUMBER})\s*(?:个)?\s*(年|个月|月)")


def _number(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        digits = dict(zip("一二三四五六七八九", range(1, 10))) | {"两": 2}
        if "十" in value:
            left, right = value.split("十", 1)
            return float(digits.get(left, 1) * 10 + digits.get(right, 0))
        return float(digits.get(value, 0))


def duration_months(text: str) -> set[float]:
    # Calendar years such as 2020年 are dates, not 2020 years of experience.
    return {_number(n) * (12 if unit == "年" else 1) for n, unit in DURATION.findall(text)
            if not (unit == "年" and _number(n) >= 1900)}


def invented_duration(text: str, source: str) -> bool:
    return not duration_months(text).issubset(duration_months(source)) or any(
        word in text and word not in source for word in ("多年", "数年"))


def grounded_standard(raw: dict, requirement: str) -> bool:
    source = raw.get("source_quote")
    text = str(raw.get("text") or "")
    if not isinstance(source, str) or not source.strip() or source not in requirement:
        return False
    # Waived responsibilities and vague seniority labels are clarification data.
    if any(word in text for word in ("高级", "资深")) or text.startswith(("不要求", "不需要", "无需", "非必须")):
        return False
    if any(word in source for word in ("不要求", "不需要", "无需", "不必")):
        waived = re.findall(r"(?:不要求|不需要|无需|不必)([^，,。；;]+)", source)
        # A positive criterion can explicitly waive an extra proof requirement.
        # "高并发经历，不要求项目名称" must not disappear just because of the waiver.
        if (len(source) <= 15 and source.startswith(("不要求", "不需要", "无需", "不必"))) or any(w.strip() in text and not any(prefix + w.strip() in text for prefix in ("不要求", "不需要", "无需", "不必"))
               for w in waived if w.strip()):
            return False
    if raw.get("type") == "VETO" and not any(
        word in source for word in ("禁止", "不得", "不能", "一票否决", "不可", "拒绝")):
        return False
    if invented_duration(text, source):
        return False
    if any(word in text and word not in source for word in ("学历", "本科", "硕士", "博士", "985", "211")):
        return False
    if raw.get("type") == "VETO" and any(word in text for word in ("无证据", "未提供", "无法提供", "未提及")):
        return False
    return True


class Assessment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    criterion_id: str
    verdict: Literal["SATISFIED", "NOT_SATISFIED", "NO_EVIDENCE"]
    evidence: list[str] = Field(max_length=3)
    reason: str = Field(min_length=1)


def decisive_quotes(assessment: Assessment, resume: str, criterion: str) -> list[str]:
    if assessment.verdict == "NO_EVIDENCE" or not assessment.evidence:
        return []
    if any(not q.strip() or q not in resume for q in assessment.evidence):
        return []
    required = duration_months(criterion)
    if required:
        skills = set(re.findall(r"[A-Za-z][A-Za-z0-9+#.]*", criterion.lower()))
        # 8 years Java + a separate Python mention is not 8 years Python.
        clauses = re.split(r"[，,。；;\n]", "\n".join(assessment.evidence).lower())
        actual = set().union(*(duration_months(c) for c in clauses if all(s in c for s in skills)))
        if not actual:
            return []
        if assessment.verdict == "SATISFIED" and max(actual) < max(required):
            return []
    return assessment.evidence
