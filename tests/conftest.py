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
