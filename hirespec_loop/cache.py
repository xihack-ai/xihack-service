from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter

from .errors import InputError
from .evaluation import semantic_hash, text_hash, verify_evaluation
from .models import Candidate, EvaluationRecord, RankingSnapshot, SpecVersion, VerifiedEvaluation
from .ranking import rank_candidates
from .service import HireSpecAI


@dataclass(frozen=True)
class CacheKey:
    job_id: str
    candidate_id: str
    resume_hash: str
    criterion_id: str
    criterion_semantic_hash: str
    evaluator_version: str


class EvaluationCache:
    """Server-side verified values only. Replace with a DB repository in production."""

    def __init__(self) -> None:
        self._values: dict[CacheKey, VerifiedEvaluation] = {}

    def get(self, key: CacheKey) -> VerifiedEvaluation | None:
        return self._values.get(key)

    def put(self, key: CacheKey, value: VerifiedEvaluation) -> None:
        if value.resume_hash != key.resume_hash or value.criterion_id != key.criterion_id:
            raise InputError("评估结果与缓存键不匹配。")
        self._values[key] = value

    def __len__(self) -> int:
        return len(self._values)


async def build_snapshot(
    ai: HireSpecAI,
    spec: SpecVersion,
    candidates: tuple[Candidate, ...],
    cache: EvaluationCache,
    *, concurrency: int = 4,
) -> RankingSnapshot:
    if not candidates or len({c.candidate_id for c in candidates}) != len(candidates):
        raise InputError("候选人列表不能为空且 ID 不可重复。")
    if type(concurrency) is not int or concurrency < 1:
        raise InputError("并发数必须为正整数。")
    ai._check_identity()
    started = perf_counter()
    semaphore = asyncio.Semaphore(concurrency)
    model_evaluations = 0
    cache_hits = 0

    async def evaluate(candidate: Candidate, criterion) -> EvaluationRecord:
        nonlocal model_evaluations, cache_hits
        key = CacheKey(
            job_id=spec.job_id, candidate_id=candidate.candidate_id,
            resume_hash=text_hash(candidate.resume_text), criterion_id=criterion.criterion_id,
            criterion_semantic_hash=semantic_hash(criterion), evaluator_version=ai.evaluator_version,
        )
        verified = cache.get(key)
        if verified is None:
            async with semaphore:
                model_evaluations += 1
                draft = await ai.evaluate_criterion(candidate.resume_text, criterion)
                verified = verify_evaluation(candidate.resume_text, draft, expected_criterion_id=criterion.criterion_id)
                cache.put(key, verified)
        else:
            cache_hits += 1
        # Rebinding to v2 creates a new immutable record. v1 records remain intact.
        return EvaluationRecord(
            job_id=spec.job_id, candidate_id=candidate.candidate_id, spec_version=spec.version,
            criterion_semantic_hash=key.criterion_semantic_hash, evaluator_version=ai.evaluator_version,
            evaluation=verified,
        )

    # Drain every task even on failure: no orphan writes after caller sees an error.
    results = await asyncio.gather(
        *(evaluate(candidate, criterion) for candidate in candidates for criterion in spec.criteria),
        return_exceptions=True,
    )
    errors = [result for result in results if isinstance(result, BaseException)]
    if errors:
        raise errors[0]
    records = tuple(results)
    entries = rank_candidates(spec, candidates, records, evaluator_version=ai.evaluator_version)
    return RankingSnapshot(
        job_id=spec.job_id, spec_version=spec.version, entries=entries, records=records,
        model_evaluations=model_evaluations, cache_hits=cache_hits,
        elapsed_seconds=perf_counter() - started,
    )
