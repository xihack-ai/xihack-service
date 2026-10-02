from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import (
    ApprovalReceipt, Candidate, ChangeImpact, ChangeProposal, Clarification, Criterion, CriterionBody,
    EvaluationDraft, EvaluationRecord, Feedback, ProposedChange, RankingSnapshot, RecomputeState,
    ProposalOutput, SafetyAssessment, SemanticCriterion, SpecDraft, SpecDraftOutput, SpecVersion, VerifiedEvaluation,
)


SCHEMA_MODELS = (Criterion, CriterionBody, SemanticCriterion, SpecDraftOutput, SpecDraft, SpecVersion, Clarification, EvaluationDraft,
                 VerifiedEvaluation, EvaluationRecord, Candidate, Feedback, ProposedChange,
                 ProposalOutput, ChangeProposal, SafetyAssessment, ChangeImpact, RankingSnapshot, RecomputeState, ApprovalReceipt)


def export_schemas(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for model in SCHEMA_MODELS:
        (directory / f"{model.__name__}.json").write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2), encoding="utf-8",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="导出前后端共用 JSON Schema")
    parser.add_argument("--output", type=Path, default=Path("schemas"))
    args = parser.parse_args()
    export_schemas(args.output)
    print(f"已导出 {len(SCHEMA_MODELS)} 个 Schema：{args.output.resolve()}")


if __name__ == "__main__":
    main()
