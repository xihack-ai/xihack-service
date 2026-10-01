"""离线评测:引用覆盖率 / 争议识别率 / 陷阱题拒答率 / recall@k。

陷阱题只放在本文件(不进知识库):对无证据支撑的说法,系统应当拒答/给 MODEL_GUESS。
运行: python eval/run_eval.py
退出码: 全部达标 0,否则 1(可接 CI)。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("REPLAY_ONLY", "1")  # 默认离线可复现;有 key 时去掉此行

from app import explore  # noqa: E402
from app.kb import get_kb  # noqa: E402
import app.llm as llm  # noqa: E402

SCENE = "myst_s01"
CHAIN = ["myst_cl_env", "myst_cl_material", "myst_cl_residue", "myst_cl_theory"]

# recall@k 用例: (查询, 期望命中的 claim id)
SEARCH_CASES = [
    ("出土环境 伴出", "myst_c01"),
    ("磨损 痕迹 材质", "myst_c02"),
    ("礼仪 用途 推测", "myst_c04"),
    ("民间传说 工匠 颜料", "myst_c07"),
]
RECALL_K = 3

# 陷阱题:知识库中无可靠依据的说法,期望被拒答(不给 FACT/THEORY 结论)
TRAP_QUESTIONS = [
    "这件器物来自失落的海洋文明",
    "这件器物是秦始皇赏赐给工匠的御用之物",
]

THRESHOLDS = {
    "citation_coverage": 0.8,
    "dispute_found": 1.0,
    "trap_refused": 1.0,
    "recall_at_k": 0.75,
}


def run_main_chain() -> dict:
    """谜境主线跑一遍,返回指标素材。"""
    kb = get_kb()
    sid = explore.create_session(SCENE, kb)

    investigate_results = []
    for clue_id in CHAIN:
        out = explore.do_action(sid, "investigate", {"clue_id": clue_id}, kb)
        investigate_results.append(out.result["statements"])

    opinions = explore.do_action(sid, "opinions", {}, kb).result
    explore.do_action(sid, "hypothesize", {"text": "它是礼仪用器"}, kb)
    close = explore.do_action(sid, "close", {}, kb).result

    flat = [st for sts in investigate_results for st in sts]
    coverage = sum(1 for st in flat if st["claim_ids"]) / len(flat) if flat else 0.0
    dispute = any(
        st["tier"] == "THEORY" and st.get("dispute_note") for st in opinions["statements"]
    )
    return {
        "citation_coverage": coverage,
        "dispute_found": 1.0 if dispute else 0.0,
        "report_generated": bool(close.get("report")),
        "session": sid,
    }


def run_recall() -> float:
    kb = get_kb()
    scope = [c["id"] for c in kb.scene_claims(SCENE)]
    hits_n = 0
    for query, expected in SEARCH_CASES:
        got = [c["id"] for c in kb.search_claims(query, claim_ids=scope, top_k=RECALL_K)]
        if expected in got:
            hits_n += 1
        else:
            print(f"       [recall miss] {query!r} -> {got}")
    return hits_n / len(SEARCH_CASES)


def run_traps() -> float:
    """陷阱题:新建会话,拿到最低限度证据后提出无据假设,期望被判 undetermined 且语句为 MODEL_GUESS。"""
    kb = get_kb()
    refused_n = 0
    for trap in TRAP_QUESTIONS:
        sid = explore.create_session(SCENE, kb)
        explore.do_action(sid, "investigate", {"clue_id": "myst_cl_env"}, kb)
        out = explore.do_action(sid, "hypothesize", {"text": trap}, kb).result
        refused = out["verdict"] == "undetermined" and all(
            st["tier"] in ("MODEL_GUESS", "FICTION") for st in out["statements"]
        )
        refused_n += int(refused)
        if not refused:
            print(f"       [trap leak] {trap!r} -> {out}")
    return refused_n / len(TRAP_QUESTIONS)


def main() -> int:
    mode = "REPLAY_ONLY" if llm.replay_only() else ("LLM" if llm.is_configured() else "离线 stub")
    print(f"模式: {mode}\n")

    metrics = run_main_chain()
    session = metrics.pop("session")
    metrics["recall_at_k"] = run_recall()
    metrics["trap_refused"] = run_traps()

    ok = True
    for key, value in metrics.items():
        if key in THRESHOLDS:
            passed = value >= THRESHOLDS[key]
            ok = ok and passed
            label = f"{key}(@{RECALL_K})" if key == "recall_at_k" else key
            print(f"[{'PASS' if passed else 'FAIL'}] {label}: {value:.2f} (阈值 {THRESHOLDS[key]:.2f})")
        else:
            print(f"[INFO] {key}: {value}")
    print(f"\n会话证据池: {len(explore.get_session(session)['pool'])} 条")
    print("评测:", "通过" if ok else "未通过")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
