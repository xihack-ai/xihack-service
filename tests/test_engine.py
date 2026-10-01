"""引擎级测试(不经 API):陷阱线索拒答判定,用合成知识库。"""

import json
from pathlib import Path

from app import explore
from app.kb import KnowledgeBase, load_kb_dict

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample_kb.json"


def _kb_with_trap(tmp_path: Path) -> KnowledgeBase:
    raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
    scene = raw["scenes"][0]
    scene["clues"].append(
        {
            "id": "myst_cl_trap",
            "tag": "神秘传闻",
            "title": "来自失落的海洋文明?",
            "question": "有传闻说这件器物来自失落的海洋文明,是真的吗?",
            "claim_ids": [],
            "requires": ["myst_cl_theory"],
            "kind": "trap",
            "topics": ["传闻"],
            "order": 9,
        }
    )
    p = tmp_path / "kb_trap.json"
    p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return KnowledgeBase(load_kb_dict(p))


def test_trap_clue_refusal(tmp_path):
    kb = _kb_with_trap(tmp_path)
    sid = explore.create_session("myst_s01", kb)
    explore.do_action(sid, "investigate", {"clue_id": "myst_cl_env"}, kb)
    explore.do_action(sid, "investigate", {"clue_id": "myst_cl_material"}, kb)
    explore.do_action(sid, "investigate", {"clue_id": "myst_cl_theory"}, kb)

    # 闸门:trap 需 theory 先解锁(此处已满足);调查陷阱线索
    out = explore.do_action(sid, "investigate", {"clue_id": "myst_cl_trap"}, kb)
    result = out.result
    assert result["trap"] is True
    assert result["refused"] is True  # 无资料 -> 拒答,不给 FACT/THEORY 结论


def test_trap_gate_blocks(tmp_path):
    kb = _kb_with_trap(tmp_path)
    sid = explore.create_session("myst_s01", kb)
    import pytest

    with pytest.raises(explore.ActionError):
        explore.do_action(sid, "investigate", {"clue_id": "myst_cl_trap"}, kb)
