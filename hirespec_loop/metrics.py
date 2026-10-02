"""Evaluation helpers. Test labels must stay outside prompts and calibration."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .errors import InputError


@dataclass(frozen=True)
class DatasetSplit:
    calibration_ids: tuple[str, ...]
    test_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        all_ids = self.calibration_ids + self.test_ids
        if len(self.calibration_ids) != 10 or len(self.test_ids) != 30 or len(set(all_ids)) != 40:
            raise InputError("验证集必须为固定、不重叠的 10 份校准样本和 30 份测试样本。")


def precision_at_5(ranked_ids: Sequence[str], labels: Mapping[str, bool]) -> float:
    if len(ranked_ids) < 5 or len(set(ranked_ids)) != len(ranked_ids):
        raise InputError("Precision@5 需要至少五个不重复的候选人。")
    if any(candidate_id not in labels or type(labels[candidate_id]) is not bool for candidate_id in ranked_ids):
        raise InputError("每个候选人必须有独立经理提供的布尔标签。")
    return sum(labels[candidate_id] for candidate_id in ranked_ids[:5]) / 5


def compare_top5(
    split: DatasetSplit,
    rankings: Mapping[str, Sequence[str]],
    test_labels: Mapping[str, bool],
) -> dict[str, float]:
    expected = set(split.test_ids)
    if set(rankings) != {"keyword_baseline", "spec_v1", "spec_v2"} or set(test_labels) != expected:
        raise InputError("必须提供三个方法及同一测试集的独立标签。")
    for ids in rankings.values():
        if len(ids) != 30 or len(set(ids)) != 30 or set(ids) != expected:
            raise InputError("三个方法必须在同一 30 份测试样本上排序。")
    return {method: precision_at_5(ids, test_labels) for method, ids in rankings.items()}
