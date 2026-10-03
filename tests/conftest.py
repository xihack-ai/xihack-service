"""测试默认用显式假模型(测试替身):生产代码已删除兜底编造数据,模型不可用时如实报错。

标了 @pytest.mark.offline_llm 的测试保持真实离线环境。
PG 环境变量全程清空,图用内存 checkpointer。
"""

import os
import json

os.environ["REPLAY_ONLY"] = "1"
os.environ.pop("LLM_API_KEY", None)
for _k in ("DATABASE_URL", "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE"):
    os.environ.pop(_k, None)

import pytest  # noqa: E402

from app.workflow import graph as wf  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_graph():
    wf.reset_for_tests()
    yield
    wf.reset_for_tests()


@pytest.fixture(autouse=True)
def _fake_live_model(monkeypatch, request):
    """测试专用假模型:只存在于测试层,替代原先生产代码里的兜底标准。"""
    if request.node.get_closest_marker("offline_llm"):
        return
    from app.workflow import nodes
    monkeypatch.setattr(nodes.llm, "is_configured", lambda: True)
    monkeypatch.setattr(nodes.llm, "replay_only", lambda: False)

    def fake_chat_json(messages, fallback=None, **kwargs):
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if "判断招聘文本" in system:
            return {"status": "SAFE", "reason": "测试替身：岗位相关"}
        if "把反馈翻译" in system:
            payload = json.loads(user)
            text = payload["feedback"]
            # Explicit test fixture behavior, never a production fallback.
            if text == "感觉不合适":
                return {"reason": text, "status": "NEEDS_CLARIFICATION", "clarification_question": "哪项岗位能力不足？"}
            add = [{"text": text[:40], "type": "NICE_TO_HAVE", "weight": 2}] if any(k in text for k in ("更看重", "增加", "要求", "必须")) else []
            remove = [c["id"] for c in payload["criteria"] if any(k in text for k in ("取消", "删除", "去掉")) and any(t in text for t in nodes._tokens(c["text"]))]
            return {"reason": text, "add": add, "remove": remove, "update": []}
        if "source_quote" in system:  # generate_spec:把整段需求作为来源原文,回拟三条标准
            return {"criteria": [
                {"id": "c1", "type": "MUST_HAVE", "text": "相关项目经验", "weight": 3, "source_quote": user},
                {"id": "c2", "type": "MUST_HAVE", "text": "技术栈匹配", "weight": 2, "source_quote": user},
                {"id": "c3", "type": "NICE_TO_HAVE", "text": "沟通协作能力", "weight": 1, "source_quote": user},
            ]}
        if "提炼明确要求" in system:  # clarify:回显需求原文
            return {"clarified": user, "questions": []}
        return dict(fallback or {})  # 其余节点(如反馈启发式)自行处理空结果

    monkeypatch.setattr(nodes.llm, "chat_json", fake_chat_json)


@pytest.fixture
def labelled_calibration_evidence(monkeypatch):
    """Explicit labelled synthetic evidence for graph/ranking tests, not a production fallback."""
    from app.workflow import nodes
    labelled = {
        "相关项目经验": {"张三": "主导过 Python Web 项目"},
        "技术栈匹配": {"张三": "技术栈匹配团队要求"},
        "后端项目经验": {"张三": "主导过 Python Web 项目"},
        "分布式系统设计": {"张三": "参与分布式系统设计"},
        "更看重分布式系统经验": {"张三": "参与分布式系统设计"},
        "更看重分布式系统设计": {"张三": "参与分布式系统设计"},
        "仅前端经验": {"李四": "仅有前端经验"},
    }
    def extract(cand, criteria):
        result = []
        for criterion in criteria:
            quote = labelled.get(criterion['text'], {}).get(cand['name'])
            if quote and quote in cand['resume_text']:
                result.append({'candidate_id': cand['id'], 'criterion_id': criterion['id'],
                               'quote': quote, 'stance': 'support', 'valid': False})
        return result
    monkeypatch.setattr(nodes, '_extract_for_candidate', extract)
