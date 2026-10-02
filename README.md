# xihack

Windows 本地连接招聘数据库 `xks`：见 [操作步骤](docs/DATABASE_LOCAL.md)。
使用 `python scripts/local_backend.py` 隐藏输入数据库密码；`/health/database` 仅检查连接和表结构。

招聘闭环真实数据库读写：见 [接口与运行说明](docs/HIRING_PERSISTENCE.md)。
模型与 LangGraph 接入边界：见 [队长集成说明](docs/HIRING_INTEGRATION.md)。
`python scripts/local_backend.py --recruiting-demo` 启用明确模拟 AI 的持久化接口；
`python scripts/recruiting_demo.py` 创建一个独立模拟职位并跑完审批、重算与重启恢复验证。

探索式历史知识库服务(FastAPI)。当前为阶段 0:通用框架 + 占位数据(与秦代无关,由数据同学在 `data/sample_kb.json` 维护)。

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate   # .venv 不提交,每人本地自建
pip install -e .

cp .env.example .env      # 填入 LLM_API_KEY;不填走离线 stub
uvicorn app.main:app --reload
```

- 前端: http://127.0.0.1:8000/ ;API 文档: http://127.0.0.1:8000/docs
- **给前端同学的契约: `docs/api_contract.md`(含示例请求/响应 JSON)**

## 三种模式

| mode | 说明 | 状态 |
|---|---|---|
| `mystery` 谜境 | 线索迷雾 → 证据池 → 假设评判 → 结案报告 | 已实现 |
| `theater` 剧场 | genres + hotspots 出发,阶段 2 实装 | 建会话返回 501 |
| `sandbox` 沙盒 | start_entity 起点自由游走,阶段 2 实装 | 建会话返回 501 |

## tier 与校验模型

tier 四级:`FACT > THEORY > FICTION > MODEL_GUESS`。

模型必须输出结构化 JSON `{"statements":[{"text","claim_ids"}]}`;后端校验:

- 编造的 claim_id 丢弃,该条 tier 降一级(FACT→THEORY→FICTION→MODEL_GUESS);
- tier = 有效引用中最差一档,含 THEORY 即显示 THEORY 并透出 `dispute_note`;
- 无任何有效引用 → `MODEL_GUESS`;
- 模型不按 JSON 输出时,回退旧正则 `[claim-id]` 抽取路径(`validate_answer_text`)。

## 谜境状态机

```
state: investigating --hypothesize--> reasoning --close--> closed
action: plan | investigate | opinions | hypothesize | close
```

线索带 `requires` 前置闸门(迷雾地图解锁逻辑),启动校验保证依赖图**无环且有起点**;
`plan` 由 LLM 在可及线索中选择,代码校验 id,失败回退 `order` 最小。

## 数据工具(给数据同学)

```bash
python scripts/check_kb.py data/sample_kb.json          # 校验,错误指到具体 id
python scripts/csv_to_kb.py csv_dir/ -o data/kb.json    # 7 张表 CSV → kb.json(utf-8-sig,多值 `;` 分隔)
```

CSV 约定见 `scripts/csv_to_kb.py` 文件头:文件名 `<表名>.csv`(sources/claims/entities/relations/scenes/clues/hotspots),
`clues/hotspots` 用 `scene_id` 列挂到场景;合并后自动校验,不通过不写回。

**陷阱题不进知识库**,统一放 `eval/run_eval.py` 的 `TRAP_QUESTIONS`。

## 测试与评测

```bash
pip install -e ".[dev]"
pytest                    # 34 项:闸门/编造 id/非法动作/THEORY 标签/环检测/完整流程/陷阱拒答
python eval/run_eval.py   # 指标:引用覆盖率/争议识别率/recall@k/陷阱拒答率,不达标退出码 1
```

## 离线与断网演练

- 无 `LLM_API_KEY`:全部动作走确定性 stub,前端/评测可完整运行;
- `REPLAY_ONLY=1`:禁止网络,只吃 `.cache/llm_cache.json` 缓存(演示前预跑填满);
- 真实调用带失败重试与磁盘缓存。

## 目录结构

```
xihack-service/
├── app/
│   ├── main.py          # FastAPI 入口,路由
│   ├── schemas.py       # pydantic 契约(数据模型 + API)
│   ├── kb.py            # 新格式加载 + 启动校验(含环检测) + 二字词检索
│   ├── llm.py           # chat/chat_json + 缓存 + 重试 + REPLAY_ONLY
│   ├── explore.py       # 谜境会话引擎(状态机/闸门/证据池)
│   └── validate.py      # 结构化校验 + tier 降级(旧正则路径保留)
├── data/sample_kb.json  # 占位数据(数据同学维护)
├── docs/api_contract.md # 接口契约(前端同学)
├── eval/run_eval.py     # 评测:覆盖率/争议/recall@k/陷阱拒答(陷阱题在此)
├── scripts/             # check_kb.py / csv_to_kb.py(数据同学用)
├── tests/               # pytest(全程离线)
├── web/index.html       # 静态前端(场景/迷雾线索/证据池/报告)
├── .env.example
└── pyproject.toml
```
