"""知识库启动校验测试:坏数据必须给出明确错误,指到具体 id。"""

import copy
import json
from pathlib import Path

import pytest

from app.kb import KbValidationError, load_kb_dict, validate_kb

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample_kb.json"


def _raw() -> dict:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def test_sample_kb_is_valid():
    assert validate_kb(_raw()) == []


def test_theory_requires_dispute_note():
    raw = _raw()
    for c in raw["claims"]:
        if c["tier"] == "THEORY":
            c["dispute_note"] = None
    errors = validate_kb(raw)
    assert any("dispute_note" in e for e in errors)


def test_claim_bad_source_reference():
    raw = _raw()
    raw["claims"][0]["source_id"] = "s_nonexistent"
    errors = validate_kb(raw)
    assert any("s_nonexistent" in e for e in errors)


def test_duplicate_claim_id():
    raw = _raw()
    raw["claims"].append(copy.deepcopy(raw["claims"][0]))
    errors = validate_kb(raw)
    assert any("重复" in e for e in errors)


def test_relation_requires_evidence():
    raw = _raw()
    raw["relations"].append(
        {"src": "myst_e01", "dst": "myst_e02", "label": "无依据", "claim_ids": []}
    )
    errors = validate_kb(raw)
    assert any("没有任何依据 claim" in e for e in errors)


def test_clue_requires_bad_reference():
    raw = _raw()
    raw["scenes"][0]["clues"][0]["requires"] = ["cl_ghost"]
    errors = validate_kb(raw)
    assert any("cl_ghost" in e for e in errors)


def test_requires_cycle_detected():
    raw = _raw()
    clues = raw["scenes"][0]["clues"]
    clues[0]["requires"] = ["myst_cl_theory"]  # env <-> theory 成环
    errors = validate_kb(raw)
    assert any("循环依赖" in e for e in errors)


def test_anomaly_and_root_question_optional():
    raw = _raw()
    scene = raw["scenes"][0]
    scene.pop("anomaly")
    scene.pop("root_question")
    assert validate_kb(raw) == []


def test_hotspot_bad_reference():
    raw = _raw()
    raw["scenes"][1]["hotspots"].append(
        {"id": "thea_h99", "label": "坏引用", "entity_ids": ["e_ghost"], "claim_ids": ["c_ghost"]}
    )
    errors = validate_kb(raw)
    assert any("thea_h99" in e for e in errors)


def test_start_entity_must_exist():
    raw = _raw()
    raw["scenes"][2]["start_entity"] = "e_ghost"
    errors = validate_kb(raw)
    assert any("start_entity" in e for e in errors)


def test_load_kb_dict_raises_on_bad_data(tmp_path):
    raw = _raw()
    raw["claims"][0]["source_id"] = "s_nonexistent"
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(KbValidationError) as ei:
        load_kb_dict(p)
    assert any("s_nonexistent" in m for m in ei.value.messages)
