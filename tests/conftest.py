"""测试全离线:LLM 走确定性回退,PG 环境变量清空,图用内存 checkpointer。"""

import os

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
