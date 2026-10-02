from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from time import perf_counter

from .fixtures import ANSWERS, OWNERSHIP_FEEDBACK, REQUIREMENT, OfflineDemoClient, load_candidates
from .service import HireSpecAI
from .workflow import LoopSession


async def run_demo() -> dict:
    client = OfflineDemoClient()
    ai = HireSpecAI(client)
    session = LoopSession(ai, job_id="JOB-DEMO", candidates=load_candidates(),
                          authorized_reviewers=frozenset({"manager-demo"}))
    clarification = await ai.clarify_requirement(REQUIREMENT)
    draft = await ai.draft_spec(REQUIREMENT, ANSWERS)
    initial = await session.approve_initial(draft, reviewer_id="manager-demo")
    proposal = await session.submit_feedback(candidate_id="P01", decision="NO", reason=OWNERSHIP_FEEDBACK,
                                             reviewer_id="manager-demo")
    impact = await session.preview(proposal.proposal_id)
    # This is a real state assertion, not an animated/hard-coded UI effect.
    assert session.current_spec.version == 1 and session.visible_snapshot == initial
    started = perf_counter()
    receipt = await session.accept(proposal.proposal_id, reviewer_id="manager-demo")
    approval_elapsed = perf_counter() - started
    updated = session.visible_snapshot
    assert updated is not None
    calls_before_repeat = dict(client.calls)
    repeated = await session.accept(proposal.proposal_id, reviewer_id="manager-demo")
    assert repeated == receipt and dict(client.calls) == calls_before_repeat
    blocked = await session.submit_feedback(candidate_id="P01", decision="NO", reason="女性不适合这个岗位。",
                                            reviewer_id="manager-demo")
    initial_positions = {entry.candidate_id: index for index, entry in enumerate(initial.entries, 1)}
    movements = [{"candidate_id": entry.candidate_id, "before": initial_positions[entry.candidate_id], "after": index}
                 for index, entry in enumerate(updated.entries, 1)]
    return {
        "data_kind": "SIMULATED_OFFLINE",
        "notice": "使用固定模拟简历及预置模型响应；审批、引用验证、diff、缓存和排序运行真实程序。未验证真实模型能力、60 秒在线耗时或真实招聘效果。",
        "requirement": REQUIREMENT, "answers": ANSWERS,
        "clarification": clarification.model_dump(mode="json"), "draft": draft.model_dump(mode="json"),
        "spec_v1": session.get_spec(1).model_dump(mode="json"),
        "ranking_v1": initial.model_dump(mode="json"),
        "proposal": proposal.model_dump(mode="json"), "impact": impact.model_dump(mode="json"),
        "approval": receipt.model_dump(mode="json"), "spec_v2": session.get_spec(2).model_dump(mode="json"),
        "ranking_v2": updated.model_dump(mode="json"), "movements": movements,
        "blocked_feedback": blocked.model_dump(mode="json"),
        "approval_elapsed_seconds_offline": approval_elapsed,
        "task_requests_offline": dict(client.calls), "idempotency_verified": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="HireSpec Loop 离线模拟闭环（不调用真实模型）")
    parser.add_argument("--output", type=Path, default=None, help="可选：保存完整前端演示数据 JSON")
    args = parser.parse_args()
    trace = asyncio.run(run_demo())
    print("HireSpec Loop — 明确标注的离线模拟演示")
    print(trace["notice"])
    print("v1：", " → ".join(e["candidate_id"] for e in trace["ranking_v1"]["entries"]))
    print("v2：", " → ".join(e["candidate_id"] for e in trace["ranking_v2"]["entries"]))
    print("C02 证据门槛改变：", trace["ranking_v2"]["model_evaluations"], "次评估请求；复用", trace["ranking_v2"]["cache_hits"], "条评估")
    print("敏感反馈：", trace["blocked_feedback"]["status"])
    print("重复批准：没有创建新版本或额外调用")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
        print("演示数据：", args.output.resolve())


if __name__ == "__main__":
    main()
