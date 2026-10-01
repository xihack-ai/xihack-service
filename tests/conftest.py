"""测试全部走离线模式:不发起任何网络请求,LLM 一律回退到确定性 stub。"""

import os

os.environ["REPLAY_ONLY"] = "1"
os.environ.pop("LLM_API_KEY", None)
