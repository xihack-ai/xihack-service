"""LangGraph 图编排:招聘标准校准流程。

    START ─(entry_route)─> clarify → generate_spec ─(需确认)→ spec_confirmation(interrupt)
                                          │                    └(确认)→ extract_evidence
                                          └(旧行为)→ extract_evidence
                                                        → validate_evidence → evaluate → END
                └(带反馈)→ interpret_feedback → bias_risk_check ─(红线)→ END
                                                  └→ propose_change ─(有提案)→ human_approval(interrupt)
                                                            └(批准且涉及候选人)→ selective_reeval → END

轨迹:每个节点完成后向 state["workflow_history"] 追加一条记录(存盘在 checkpoint
里,重启恢复后仍可读),供 GET /calibrations/{job_id} 的 workflow.history 使用。

持久化:配置了 PG 用 PostgresSaver(可按 thread_id 恢复、跨进程审批);
本地开发/测试回退 InMemorySaver。
"""

from __future__ import annotations

import os
from functools import lru_cache

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command  # noqa: F401  (API 层从这里导入恢复命令)

from app import db
from app.workflow import nodes
from app.workflow.state import CalibrationState

_graph = None
_checkpointer = None


def get_checkpointer():
    global _checkpointer
    if _checkpointer is None:
        if db.is_configured():
            from langgraph.checkpoint.postgres import PostgresSaver

            conn = db.connect()
            _checkpointer = PostgresSaver(conn)
            _checkpointer.setup()
        else:
            from langgraph.checkpoint.memory import InMemorySaver

            _checkpointer = InMemorySaver()
    return _checkpointer


def build_graph(checkpointer=None):
    g = StateGraph(CalibrationState)
    g.add_node("clarify", _traced("clarify", nodes.clarify))
    g.add_node("generate_spec", _traced("generate_spec", nodes.generate_spec))
    g.add_node("spec_confirmation", _traced("spec_confirmation", nodes.spec_confirmation))
    g.add_node("extract_evidence", _traced("extract_evidence", nodes.extract_evidence))
    g.add_node("validate_evidence", _traced("validate_evidence", nodes.validate_evidence))
    g.add_node("evaluate", _traced("evaluate", nodes.evaluate))
    g.add_node("interpret_feedback", _traced("interpret_feedback", nodes.interpret_feedback))
    g.add_node("bias_risk_check", _traced("bias_risk_check", nodes.bias_risk_check))
    g.add_node("propose_change", _traced("propose_change", nodes.propose_change))
    g.add_node("human_approval", _traced("human_approval", nodes.human_approval))
    g.add_node("selective_reeval", _traced("selective_reeval", _selective_reeval))

    g.add_conditional_edges(START, nodes.entry_route,
                            {"clarify": "clarify", "interpret": "interpret_feedback"})
    g.add_edge("clarify", "generate_spec")
    g.add_conditional_edges("generate_spec", nodes.route_after_spec,
                            {"confirm": "spec_confirmation", "extract": "extract_evidence"})
    g.add_edge("spec_confirmation", "extract_evidence")
    g.add_edge("extract_evidence", "validate_evidence")
    g.add_edge("validate_evidence", "evaluate")
    g.add_edge("evaluate", END)

    g.add_edge("interpret_feedback", "bias_risk_check")
    g.add_conditional_edges("bias_risk_check", nodes.route_after_bias,
                            {"propose": "propose_change", "end": END})
    g.add_conditional_edges("propose_change", nodes.route_after_propose,
                            {"approval": "human_approval", "end": END})
    g.add_conditional_edges("human_approval", nodes.route_after_approval,
                            {"reeval": "selective_reeval", "end": END})
    g.add_edge("selective_reeval", END)
    return g.compile(checkpointer=checkpointer or get_checkpointer())


def _traced(name: str, fn):
    """节点包装:成功返回后向 workflow_history 追加一条轨迹。

    interrupt 节点(人工确认/审批)在暂停时不会追加;恢复后节点从头重跑并
    完整返回,因此每个实际完成的节点只会留下一条记录。
    """
    def wrapped(state: CalibrationState) -> dict:
        update = fn(state)
        mark = {"node": name, "spec_version": state.get("spec_version")}
        return {**update, "workflow_history": list(state.get("workflow_history", [])) + [mark]}
    return wrapped


def _selective_reeval(state: CalibrationState) -> dict:
    """只重跑受标准变更影响的候选人:重新抽证据 → 校验 → 打分。"""
    extracted = nodes.extract_evidence(state)
    merged = {**state, **extracted}
    validated = nodes.validate_evidence(merged)
    merged.update(validated)
    scored = nodes.evaluate(merged)
    return {**extracted, **validated, **scored}


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def reset_for_tests() -> None:
    """测试隔离:清掉模块级单例,让下一个用例重建图与 checkpointer。"""
    global _graph, _checkpointer
    _graph = None
    _checkpointer = None
