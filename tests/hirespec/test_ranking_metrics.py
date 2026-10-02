import asyncio
from datetime import datetime, timezone

import pytest

from hirespec_loop.errors import InputError
from hirespec_loop.evaluation import semantic_hash, verify_evaluation
from hirespec_loop.metrics import DatasetSplit, compare_top5, precision_at_5
from hirespec_loop.models import (
    Candidate, Criterion, EvaluationDraft, EvaluationRecord, EvidenceDraft, SpecVersion,
)
from hirespec_loop.ranking import rank_candidates

from .test_workflow import started


@pytest.mark.parametrize("tamper", ["spec", "resume", "semantic", "evaluator", "pair", "job", "offset"])
def test_ranking_rejects_inconsistent_or_mixed_matrix(client, tamper):
    async def scenario():
        ai, session = await started(client)
        records = list(session.visible_snapshot.records)
        data = records[0].model_dump()
        if tamper == "spec":
            data["spec_version"] = 2
        elif tamper == "resume":
            data["evaluation"]["resume_hash"] = "another-source"
        elif tamper == "semantic":
            data["criterion_semantic_hash"] = "another-standard"
        elif tamper == "evaluator":
            data["evaluator_version"] = "another-model"
        elif tamper == "pair":
            data = records[1].model_dump()
        elif tamper == "job":
            data["job_id"] = "another-job"
        else:
            data["evaluation"]["evidence"][0]["start"] += 1
        records[0] = EvaluationRecord.model_validate(data)
        with pytest.raises(InputError):
            rank_candidates(session.current_spec, session.candidates, tuple(records), evaluator_version=ai.evaluator_version)
    asyncio.run(scenario())


def test_veto_missing_evidence_is_pending_not_safe_and_satisfied_means_triggered():
    criterion = Criterion(criterion_id="C01", category="VETO", definition="明确无法承担岗位要求的值班",
                          satisfied_when="明确表示无法承担值班", not_satisfied_when="明确确认能够承担值班",
                          evidence_rule="引用候选人对岗位值班职责的明确陈述")
    spec = SpecVersion(job_id="J1", version=1, criteria=(criterion,), created_at=datetime.now(timezone.utc),
                       approved_by="manager", next_criterion_number=2)
    people = (Candidate(candidate_id="A", resume_text="能够承担值班"),
              Candidate(candidate_id="B", resume_text="未提到值班"),
              Candidate(candidate_id="C", resume_text="无法承担值班"))
    records = []
    for person, verdict, quotes in zip(people, ("NOT_SATISFIED", "NO_EVIDENCE", "SATISFIED"),
                                      (("能够承担值班",), (), ("无法承担值班",))):
        draft = EvaluationDraft(criterion_id="C01", verdict=verdict,
                                evidence=tuple(EvidenceDraft(quote=q) for q in quotes), reason="根据明确陈述判断")
        records.append(EvaluationRecord(job_id="J1", candidate_id=person.candidate_id, spec_version=1,
                                        criterion_semantic_hash=semantic_hash(criterion), evaluator_version="test-v1",
                                        evaluation=verify_evaluation(person.resume_text, draft)))
    ranked = rank_candidates(spec, people, tuple(records))
    assert [e.tier for e in ranked] == ["QUALIFIED", "NEEDS_VERIFICATION", "VETO_TRIGGERED"]
    assert [e.candidate_id for e in ranked] == ["A", "B", "C"]


def test_metric_is_precision_not_recall():
    labels = {f"P{i}": i % 2 == 0 for i in range(8)}
    assert precision_at_5(list(labels), labels) == 3 / 5
    with pytest.raises(InputError):
        precision_at_5(["P1", "P2"], labels)


def test_comparison_uses_same_heldout_set_without_calibration_ids():
    split = DatasetSplit(tuple(f"C{i}" for i in range(10)), tuple(f"T{i}" for i in range(30)))
    labels = {candidate_id: index < 5 for index, candidate_id in enumerate(split.test_ids)}
    rankings = {"keyword_baseline": tuple(reversed(split.test_ids)), "spec_v1": split.test_ids,
                "spec_v2": tuple(reversed(split.test_ids))}
    assert compare_top5(split, rankings, labels) == {"keyword_baseline": 0.0, "spec_v1": 1.0, "spec_v2": 0.0}
    # Deliberately allow worse v2: never manufacture an improvement.
    rankings["spec_v2"] = split.test_ids[:-1] + (split.calibration_ids[0],)
    with pytest.raises(InputError):
        compare_top5(split, rankings, labels)
    with pytest.raises(InputError):
        DatasetSplit(tuple(f"X{i}" for i in range(10)), tuple(f"X{i}" for i in range(30)))
