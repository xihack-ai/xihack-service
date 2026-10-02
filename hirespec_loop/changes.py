"""Pure deterministic changes. This module never publishes a version."""

from __future__ import annotations

import re
import unicodedata

from .errors import InputError
from .evaluation import semantic_hash
from .models import (
    ChangeImpact, ChangeOperation, Criterion, CriterionDiff, ProposedChange, SpecDraft, SpecVersion,
)


def definition_key(text: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


def ensure_unique_definitions(criteria: tuple[Criterion, ...]) -> None:
    keys = [definition_key(c.definition) for c in criteria]
    if not all(keys) or len(set(keys)) != len(keys):
        raise InputError("标准定义为空或重复；请合并重复条件。")


def preview_changes(current: SpecVersion, changes: tuple[ProposedChange, ...]) -> ChangeImpact:
    if not changes:
        raise InputError("没有可批准的标准修改。")
    by_id = {c.criterion_id: c for c in current.criteria}
    used: set[str] = set()
    next_number = current.next_criterion_number
    diffs: list[CriterionDiff] = []
    for change in changes:
        before = None
        after = None
        if change.operation == ChangeOperation.ADD:
            criterion_id = f"C{next_number:02d}"
            next_number += 1
            assert change.after is not None
            after = Criterion(criterion_id=criterion_id, **change.after.model_dump())
            by_id[criterion_id] = after
        else:
            criterion_id = change.criterion_id
            if criterion_id in used or criterion_id not in by_id:
                raise InputError("修改引用未知标准，或同一次提议重复修改一个标准。")
            used.add(criterion_id)
            before = by_id[criterion_id]
            if change.operation == ChangeOperation.UPDATE:
                assert change.after is not None
                after = Criterion(criterion_id=criterion_id, **change.after.model_dump())
                if before == after:
                    raise InputError("UPDATE 未产生任何变化，应返回 NO_CHANGE。")
                by_id[criterion_id] = after
            else:
                del by_id[criterion_id]
        evaluation_changed = after is not None and (before is None or semantic_hash(before) != semantic_hash(after))
        diffs.append(CriterionDiff(
            criterion_id=criterion_id, operation=change.operation, before=before, after=after,
            evaluation_changed=evaluation_changed,
        ))
    prospective = tuple(by_id.values())
    if not prospective:
        raise InputError("不能删除所有标准。")
    # Recheck the complete schema and deterministic duplicate detection.
    SpecDraft(criteria=prospective)
    ensure_unique_definitions(prospective)
    reevaluate = tuple(d.criterion_id for d in diffs if d.evaluation_changed)
    return ChangeImpact(
        base_spec_version=current.version, criteria=prospective, next_criterion_number=next_number,
        diff=tuple(diffs), reevaluate_ids=reevaluate,
        reuse_ids=tuple(c.criterion_id for c in prospective if c.criterion_id not in reevaluate),
        removed_ids=tuple(d.criterion_id for d in diffs if d.operation == ChangeOperation.REMOVE),
    )
