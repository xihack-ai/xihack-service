"""Test-only offline fixture client and synthetic data.

Production code must never import this module; it lives in tests/ as a test double.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from hirespec_loop.models import Candidate, Category, CriterionBody
from hirespec_loop.prompts import PROMPTS
from hirespec_loop.safety import rule_review

DATA_FILE = Path(__file__).resolve().parent / "data" / "synthetic_candidates.json"


REQUIREMENT = "我要一个能独立负责 AI 产品的高级后端"
ANSWERS = {
    "独立负责范围": "技术方案和上线；运维经验作为加分项。",
    "AI 经历门槛": "先接受参与 AI 项目，是否必须独立负责生产系统待样本校准。",
    "是否管理团队": "不要求管理团队；必须能交付 Python 后端，云部署经验加分。",
}
OWNERSHIP_FEEDBACK = "他只是参与项目，我们需要真正负责过上线的人。"
PROMOTION_FEEDBACK = "参与 AI 项目应该是必须项，证据门槛不变。"
VAGUE_FEEDBACK = "感觉不合适。"
UNCHANGED_FEEDBACK = "没有 Python 后端交付经历，现有必须项保持不变。"


def seed_bodies() -> tuple[CriterionBody, ...]:
    return (
        CriterionBody(category=Category.MUST_HAVE, definition="Python 后端交付经验",
                      satisfied_when="明确使用 Python 开发并交付后端服务",
                      not_satisfied_when="明确表示从未使用 Python 开发或交付后端服务",
                      evidence_rule="引用具体 Python 后端开发及交付经历，只有技能列表不足以证明满足"),
        CriterionBody(category=Category.NICE_TO_HAVE, definition="参与过 AI 项目",
                      satisfied_when="明确参与 AI 项目的开发或技术交付",
                      not_satisfied_when="明确说明没有参与过 AI 项目",
                      evidence_rule="引用具体 AI 项目及参与职责，不要求独立承担决策和上线责任"),
        CriterionBody(category=Category.NICE_TO_HAVE, definition="生产服务运维经验",
                      satisfied_when="明确负责生产服务监控、告警或故障处理",
                      not_satisfied_when="明确表示从未承担生产服务运维职责",
                      evidence_rule="引用生产服务的具体运维职责"),
        CriterionBody(category=Category.NICE_TO_HAVE, definition="云平台部署经验",
                      satisfied_when="明确在云平台部署服务",
                      not_satisfied_when="明确表示从未部署过云平台服务",
                      evidence_rule="引用云平台部署的具体经历"),
    )


def ownership_body() -> CriterionBody:
    return CriterionBody(
        category=Category.MUST_HAVE, definition="独立负责过生产级 AI 系统",
        satisfied_when="明确承担核心技术决策及上线责任",
        not_satisfied_when="明确说明只参与辅助开发，未承担核心决策及上线责任",
        evidence_rule="引用具体项目、职责和上线经历；只有参与 AI 项目或技能关键词不足以证明满足",
    )


def load_fixture_data() -> dict[str, Any]:
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def load_candidates() -> tuple[Candidate, ...]:
    return tuple(Candidate(candidate_id=r["candidate_id"], resume_text=r["resume_text"])
                 for r in load_fixture_data()["candidates"])


class OfflineDemoClient:
    """Only supports known scenario inputs and pre-authored responses.

    Its safety answers are fixtures, not a semantic classifier. Unknown inputs fail.
    The injected architecture is tested here; model capabilities require a live provider.
    """

    cache_identity = "offline-fixtures-v1;not-a-live-model"

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.payloads: list[tuple[str, dict[str, Any]]] = []
        self._rows = load_fixture_data()["candidates"]

    async def generate_json(self, system_prompt: str, payload: dict, output_schema: dict) -> dict:
        task = next((name for name, prompt in PROMPTS.items() if prompt == system_prompt), None)
        if task is None:
            raise ValueError("未知的离线任务")
        self.calls[task] += 1
        self.payloads.append((task, payload))
        data = payload["data"]
        if task == "safety_review":
            texts = tuple(data["texts"])
            result = rule_review(texts)
            if result.status != "SAFE":
                return result.model_dump(mode="json")
            allowed = {REQUIREMENT, *ANSWERS.values(), OWNERSHIP_FEEDBACK, PROMOTION_FEEDBACK, VAGUE_FEEDBACK, UNCHANGED_FEEDBACK}
            allowed.update("\n".join((c.definition, c.satisfied_when, c.not_satisfied_when, c.evidence_rule))
                           for c in (*seed_bodies(), ownership_body()))
            if any(text not in allowed for text in texts):
                raise ValueError("离线演示不支持未知安全检查输入，请接入真实语义检查模型")
            return {"status": "SAFE", "findings": [], "clarification_question": None}
        if task == "clarify":
            if data["requirement"] != REQUIREMENT:
                raise ValueError("离线演示只支持预置需求")
            return {
                "questions": ["独立负责包括技术方案、上线和运维中的哪些责任？",
                              "必须有生产级 AI 经历，还是先接受参与项目？", "需要管理团队吗？"],
                "pending_assumptions": ["独立负责范围及 AI 项目证据门槛尚未确认。"],
            }
        if task == "draft_spec":
            if data["requirement"] != REQUIREMENT or data["answers"] != ANSWERS:
                raise ValueError("离线演示只支持预置回答")
            return {"criteria": [c.model_dump(mode="json") for c in seed_bodies()], "pending_assumptions": []}
        if task == "evaluate_criterion":
            row = next((r for r in self._rows if r["resume_text"] == data["resume_text"]), None)
            definitions = {body.definition: name for body, name in zip(seed_bodies(), ("python", "participation", "operations", "cloud"))}
            definitions[ownership_body().definition] = "ownership"
            criterion = data["criterion"]
            if row is None or criterion["definition"] not in definitions:
                raise ValueError("离线演示不支持未知简历或标准")
            annotation = row["annotations"][definitions[criterion["definition"]]]
            verdict = annotation["verdict"]
            return {
                "criterion_id": criterion["criterion_id"], "verdict": verdict,
                "evidence": [{"quote": annotation["quote"]}] if annotation["quote"] else [],
                "reason": "模拟响应：原文支持此条件。" if verdict == "SATISFIED" else
                          "模拟响应：原文明示未承担此职责。" if verdict == "NOT_SATISFIED" else
                          "模拟响应：简历没有足够证据证明此条件。",
                "uncertainty": "需要核实该条职责。" if verdict == "NO_EVIDENCE" else None,
                "followup_question": "请说明你在该条标准对应项目中的具体职责。" if verdict == "NO_EVIDENCE" else None,
            }
        if task == "propose_change":
            reason = data["feedback"]["reason"]
            if reason in {OWNERSHIP_FEEDBACK, PROMOTION_FEEDBACK}:
                after = ownership_body() if reason == OWNERSHIP_FEEDBACK else CriterionBody.model_validate({
                    **seed_bodies()[1].model_dump(), "category": Category.MUST_HAVE,
                })
                return {
                    "status": "PENDING_APPROVAL",
                    "changes": [{"operation": "UPDATE", "criterion_id": "C02", "after": after.model_dump(mode="json")}],
                    "explanation": "建议校准 C02；等待经理明确批准后才生效。",
                }
            if reason == VAGUE_FEEDBACK:
                return {"status": "NEEDS_CLARIFICATION", "changes": [], "explanation": "理由不足以推断新标准。",
                        "clarification_question": "哪项岗位职责或能力没有得到证明？"}
            if reason == UNCHANGED_FEEDBACK:
                return {"status": "NO_CHANGE", "changes": [], "explanation": "反馈重复现有 Python 必须项。"}
            raise ValueError("离线演示不支持未知反馈")
        raise ValueError("离线演示未实现任务")
