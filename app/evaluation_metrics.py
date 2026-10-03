"""Metrics on independently labeled judgments, never fabricated model results."""

from app.hireloop_rules import quote_valid
from hirespec_loop.errors import InputError

CLASSES = {"criterion": ("SATISFIED", "NOT_SATISFIED", "NO_EVIDENCE"),
           "veto": ("HIT", "CLEAR", "UNKNOWN")}


def classification_report(expected: list[str], predicted: list[str], classes: tuple[str, ...]) -> dict:
    if len(expected) != len(predicted) or any(x not in classes for x in expected + predicted):
        raise InputError("标签数量或类别不合法。")
    per_class = {}
    matrix = {truth: {prediction: 0 for prediction in classes} for truth in classes}
    for truth, prediction in zip(expected, predicted):
        matrix[truth][prediction] += 1
    for label in classes:
        tp = matrix[label][label]
        fp = sum(matrix[other][label] for other in classes if other != label)
        fn = sum(matrix[label][other] for other in classes if other != label)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        per_class[label] = {"precision": precision, "recall": recall,
                            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
                            "support": tp + fn}
    return {"sample_count": len(expected), "accuracy": sum(a == b for a, b in zip(expected, predicted)) / len(expected) if expected else None,
            "macro_f1": sum(v["f1"] for v in per_class.values()) / len(classes) if expected else None,
            "per_class": per_class, "confusion_matrix": matrix}


def evaluate_rows(rows: list[dict]) -> dict:
    """One row per candidate x definition version. Kind separates VETO labels."""
    seen = set()
    grouped = {kind: [] for kind in CLASSES}
    quote_counts = {label: [0, 0] for classes in CLASSES.values() for label in classes}
    missing = hallucinations = failures = 0
    for row in rows:
        try:
            identity = (row["candidate_id"], row["criterion_id"], row["definition_version"])
            kind = row["kind"]
            if identity in seen or kind not in grouped:
                raise InputError("重复判定或 kind 不合法。")
            seen.add(identity)
            truth, prediction = row["expected"], row["predicted"]
            if truth not in CLASSES[kind]:
                raise InputError("判定类别不合法。")
            if prediction is None and row.get("error"):
                failures += 1
                continue  # Task failures are not NO_EVIDENCE and never enter completed-call metrics.
            if prediction not in CLASSES[kind]:
                raise InputError("判定类别不合法，调用失败须提供 predicted=null 和 error。")
            quotes, resume = row["quotes"], row["resume_text"]
            if not isinstance(quotes, list) or not isinstance(resume, str) or any(not isinstance(q, str) for q in quotes):
                raise InputError("quotes 与 resume_text 类型不合法。")
            grouped[kind].append((truth, prediction))
            if prediction not in {"NO_EVIDENCE", "UNKNOWN"} and not quotes:
                missing += 1
            for quote in quotes:  # Includes negative judgments and any unknown quotes.
                valid = quote_valid(resume, quote)
                quote_counts[prediction][0] += int(valid)
                quote_counts[prediction][1] += 1
                hallucinations += int(not valid)
        except (KeyError, TypeError):
            raise InputError("评测记录缺少必需字段。") from None
    valid_total = sum(v[0] for v in quote_counts.values())
    total = sum(v[1] for v in quote_counts.values())
    correct = sum(a == b for pairs in grouped.values() for a, b in pairs)
    return {"judgment_count": len(rows), "completed_count": len(rows) - failures,
        "task_failure_count": failures, "task_failure_rate": failures / len(rows) if rows else None,
        "end_to_end_accuracy": correct / len(rows) if rows else None,
        **{kind: classification_report(
        [a for a, _ in pairs], [b for _, b in pairs], CLASSES[kind]) for kind, pairs in grouped.items()},
        "quote_validity": {label: {"valid": valid, "count": count, "rate": valid / count if count else None}
                           for label, (valid, count) in quote_counts.items()},
        "overall_quote_validity": valid_total / total if total else None,
        "missing_required_evidence": missing, "hallucination_count": hallucinations,
        "scope": "输入记录上的模型层指标；子串校验不证明语义正确或真实招聘效果。"}
