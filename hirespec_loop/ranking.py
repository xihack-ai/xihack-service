"""Transparent tier ranking for integration/demo. No LLM and no aggregate score."""

from __future__ import annotations

from .errors import InputError
from .evaluation import semantic_hash, text_hash
from .models import Candidate, Category, EvaluationRecord, RankingEntry, SpecVersion, Verdict


TIER_ORDER = {"QUALIFIED": 0, "NEEDS_VERIFICATION": 1, "REQUIREMENT_CONFLICT": 2, "VETO_TRIGGERED": 3}


def rank_candidates(
    spec: SpecVersion,
    candidates: tuple[Candidate, ...],
    records: tuple[EvaluationRecord, ...],
    *, evaluator_version: str | None = None,
) -> tuple[RankingEntry, ...]:
    by_candidate = {c.candidate_id: c for c in candidates}
    by_criterion = {c.criterion_id: c for c in spec.criteria}
    if not candidates or len(by_candidate) != len(candidates):
        raise InputError("候选人列表为空或 ID 重复。")
    if len(records) != len(candidates) * len(spec.criteria):
        raise InputError("排名需要同一版本的完整评估矩阵。")
    versions = {r.evaluator_version for r in records}
    if len(versions) != 1 or (evaluator_version is not None and versions != {evaluator_version}):
        raise InputError("排名不能混用不同评估器版本。")
    matrix: dict[tuple[str, str], EvaluationRecord] = {}
    for record in records:
        evaluation = record.evaluation
        pair = (record.candidate_id, evaluation.criterion_id)
        if pair in matrix or record.candidate_id not in by_candidate or evaluation.criterion_id not in by_criterion:
            raise InputError("排名含重复或未知的候选人 × 标准记录。")
        source = by_candidate[record.candidate_id].resume_text
        if (record.job_id != spec.job_id or record.spec_version != spec.version
            or record.criterion_semantic_hash != semantic_hash(by_criterion[evaluation.criterion_id])
            or evaluation.resume_hash != text_hash(source)):
            raise InputError("排名混用了职位、Spec 版本、条款语义或简历版本。")
        for evidence in evaluation.evidence:
            if (source[evidence.start:evidence.end] != evidence.quote
                or evidence.start_utf16 != len(source[:evidence.start].encode("utf-16-le")) // 2
                or evidence.end_utf16 != len(source[:evidence.end].encode("utf-16-le")) // 2):
                raise InputError("已验证引用与当前简历文本不一致。")
        matrix[pair] = record

    result: list[RankingEntry] = []
    for candidate in candidates:
        veto = False
        conflict = False
        pending = False
        nice_met = 0
        explanations: list[str] = []
        for criterion in spec.criteria:
            evaluation = matrix[(candidate.candidate_id, criterion.criterion_id)].evaluation
            verdict = evaluation.verdict
            if criterion.category == Category.VETO:
                if verdict == Verdict.SATISFIED:
                    veto = True
                    explanations.append(f"{criterion.criterion_id}：有证据触发一票否决")
                elif verdict == Verdict.NO_EVIDENCE:
                    pending = True
                    explanations.append(f"{criterion.criterion_id}：否决条件尚待核实")
            elif criterion.category == Category.MUST_HAVE:
                if verdict == Verdict.NOT_SATISFIED:
                    conflict = True
                    explanations.append(f"{criterion.criterion_id}：有证据不满足必须项")
                elif verdict == Verdict.NO_EVIDENCE:
                    pending = True
                    explanations.append(f"{criterion.criterion_id}：必须项缺少证据")
            elif verdict == Verdict.SATISFIED:
                nice_met += 1
        tier = "VETO_TRIGGERED" if veto else "REQUIREMENT_CONFLICT" if conflict else "NEEDS_VERIFICATION" if pending else "QUALIFIED"
        if not explanations:
            explanations.append("必须项已有证据支持，未发现已触发或待核实的否决条件。")
        result.append(RankingEntry(
            candidate_id=candidate.candidate_id, tier=tier,
            nice_to_have_met=nice_met, explanations=tuple(explanations),
        ))
    return tuple(sorted(result, key=lambda entry: (TIER_ORDER[entry.tier], -entry.nice_to_have_met, entry.candidate_id)))
