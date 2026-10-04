# AI软件赛道 - HireSpec Loop - 大秦帝国

赛事提交材料见 [项目说明文档](PROJECT.md)，包含架构图、Agent 工作流、部署使用说明和 [测试用例与结果](docs/submission/TEST_RESULTS.md)。模型接入使用个人 DeepSeek API Key，密钥不随代码提交。

招聘标准校准服务:FastAPI 管业务,LangGraph 管有状态、可暂停恢复的校准工作流。

创建职位前的逐题需求澄清与接口契约：见 [需求问答说明](docs/REQUIREMENT_CLARIFICATION.md)。模糊需求返回问题，经理回答后再生成待确认草稿。

```text
Next.js → FastAPI → LangGraph → {LLM, PostgreSQL}
```

Windows 本地连接招聘数据库 `xks`：见 [操作步骤](docs/DATABASE_LOCAL.md)。
使用 `python scripts/local_backend.py` 隐藏输入数据库密码；`/health/database` 仅检查连接和表结构。

招聘闭环真实数据库读写：见 [接口与运行说明](docs/HIRING_PERSISTENCE.md)。
模型与 LangGraph 接入边界：见 [队长集成说明](docs/HIRING_INTEGRATION.md)。
招聘闭环的模拟 AI 演示模式已移除：`/api/hiring/*` 的 AI 端点复用主接口的 `LLM_*` 配置接真实模型，未配置 `LLM_API_KEY` 时返回 503。

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

- API 文档: http://127.0.0.1:8000/docs
- 不配 `LLM_API_KEY` / 设 `REPLAY_ONLY=1` 时走确定性回退,断网可演示
- 不配 PG 时业务数据在进程内存,checkpointer 用内存版;配了 `DATABASE_URL` 自动切 `PostgresSaver`(跨进程可恢复)

## 校准工作流(LangGraph)

```text
START ─(新职位)→ clarify → generate_spec ─(require_spec_confirmation=true)→ spec_confirmation(interrupt 暂停)
                                     │                       └(/spec/confirm 确认或编辑后)→ extract_evidence
                                     └(默认旧行为)→ extract_evidence
        extract_evidence → validate_evidence → evaluate → END
      └(HR反馈)→ interpret_feedback → bias_risk_check ─(命中红线)→ END
                                        └→ propose_change → human_approval(interrupt 暂停)
                                                └(批准)→ selective_reeval(只重跑受影响候选人)→ END
```

原则：LLM 只做理解与措辞；证据必须是简历原文（代码校验）、打分/红线/变更应用全部确定性执行，编造引用一律丢弃。
未确认前绝不抽证据/评估；人工编辑（首次确认、审批 Edit）的文本/类型/唯一 ID/偏见风险校验全部在
`Command(resume=...)` 之前完成，非法编辑直接 422 且 checkpoint 不受污染。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（含 storage 模式） |
| GET | `/health/database` | 只读检查数据库连接和表结构，配置/连接失败返回 `503` |
| POST | `/jobs` | 建职位+候选人并启动校准；`require_spec_confirmation=true` 时停在首次标准确认（默认 false=旧行为一次跑完） |
| GET | `/calibrations/{job_id}` | 查看当前标准/评估/状态/workflow 元信息 |
| POST | `/calibrations/{job_id}/spec/confirm` | 首次标准确认：body `{"criteria": CriterionIn[]}`（完整标准集，可编辑）；确认后 save_spec → 抽证 → 评估 |
| POST | `/calibrations/{job_id}/feedback` | 提交 HR 反馈 → 产出标准变更提案，停在待审批；停在 `AWAITING_SPEC_CONFIRMATION` 时返回 409 |
| POST | `/calibrations/{job_id}/approval` | Accept / Edit / Reject 恢复审批 → 选择性重评；Edit 的风险/格式校验在 resume 前，非法返回 422 |
| GET | `/calibrations/{job_id}/history` | 查询标准版本、反馈、提案和审计记录；需要 PostgreSQL |

`status`：`AWAITING_SPEC_CONFIRMATION`（等确认初始标准）/ `EVALUATED`（已评估）/ `AWAITING_APPROVAL`（等人点批准，响应带 `change_proposal`）。

响应中的 `workflow` 字段全部来自真实 checkpoint 状态（`get_state().next` + 节点轨迹，不虚构）：
`{next_nodes, completed_nodes, waiting_for: "spec_confirmation"|"human_approval"|null,
model_mode: "offline_fallback"|"live", checkpointer: "memory"|"postgres", history}`。
`workflow.history` 是当前图状态里的节点操作记录（初始/反馈/审批，随 checkpoint 持久化）；
`/calibrations/{id}/history` 端点仍是数据库决策日志，不配 PG 依旧 503，两者互不影响。
同一 job 的变更接口串行：并发提交直接 409 `JOB_BUSY`（非阻塞，不排队）。

同 ID 标准的编辑语义：改文本 → 旧证据作废重抽；只改类型/权重 → 不重抽，仅重新分档聚合；
删除的标准真正从标准集移除，且新增标准不复用已删除的 ID。

## API 调用方式

以下为当前 Next.js 前端使用的 LangGraph 接口。默认 Base URL 为 `http://127.0.0.1:8000`，写请求使用 `Content-Type: application/json`。示例采用 macOS/Linux shell 语法；`JOB_ID` 请替换为创建接口返回的 `job_id`，不是固定测试任务 ID。

这套接口当前没有用户鉴权，`created_by`、`decided_by` 是客户端提交的审计字段，不代表身份验证。生产部署前应增加认证和职位访问权限，不要直接公开未鉴权的写接口。

### 创建任务

```bash
curl -X POST http://127.0.0.1:8000/jobs \
  -H 'Content-Type: application/json' \
  -d '{"title":"后端工程师","requirement":"招聘负责核心服务的后端工程师。","candidates":[{"name":"候选人姓名","resume_text":"候选人简历原文"}],"require_spec_confirmation":true}'
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `title` | string | 必填职位名称 |
| `requirement` | string | 必填非空招聘需求 |
| `candidates` | array | 可选，默认空数组；每项包含 `name`、非空 `resume_text` |
| `require_spec_confirmation` | boolean | 默认 `false`；前端传 `true`，在人工确认标准前暂停 |

职位及候选人 ID 由后端生成。创建时 clarify 和 generate_spec 节点执行，澄清问题通过响应中的 `clarification_questions` 返回；此接口没有独立的澄清回答提交步骤。

### 读取任务、确认标准

```bash
curl http://127.0.0.1:8000/calibrations/JOB_ID

curl -X POST http://127.0.0.1:8000/calibrations/JOB_ID/spec/confirm \
  -H 'Content-Type: application/json' \
  -d '{"criteria":[{"id":"C1","type":"MUST_HAVE","text":"核心服务开发经验","weight":3}]}'
```

确认时传入**完整标准集**，不是局部 patch。`type` 为 `MUST_HAVE`、`NICE_TO_HAVE` 或 `VETO`；`text` 必须非空；`weight` 为 1～3，默认 2；未提供 `id` 时后端生成 ID，编辑已有标准时应保留原 ID。标准集不能为空，ID 不得重复。

### 提交反馈、处理提案

```bash
curl -X POST http://127.0.0.1:8000/calibrations/JOB_ID/feedback \
  -H 'Content-Type: application/json' \
  -d '{"text":"更看重团队管理经验。","created_by":"manager"}'

# 接受提案
curl -X POST http://127.0.0.1:8000/calibrations/JOB_ID/approval \
  -H 'Content-Type: application/json' \
  -d '{"action":"accept","decided_by":"manager","note":"确认变更"}'

# 编辑后接受：提交替换后的完整标准集
curl -X POST http://127.0.0.1:8000/calibrations/JOB_ID/approval \
  -H 'Content-Type: application/json' \
  -d '{"action":"edit","decided_by":"manager","criteria":[{"id":"C1","type":"MUST_HAVE","text":"独立交付核心服务的经验","weight":3}]}'

# 拒绝提案
curl -X POST http://127.0.0.1:8000/calibrations/JOB_ID/approval \
  -H 'Content-Type: application/json' \
  -d '{"action":"reject","decided_by":"manager"}'
```

反馈正文 `text` 必填非空，`created_by` 可选。审批 `action` 支持 `accept`、`edit`、`reject`；`edit` 必须传 `criteria`，`note` 和 `decided_by` 可选。审批前提案不生效；拒绝不修改标准或评估结果。

### 公共任务响应

创建、读取、确认、反馈、审批接口均返回 `CalibrationOut`：

| 字段 | 含义 |
|---|---|
| `job_id`、`job_title`、`spec_version` | 职位及当前标准版本 |
| `status` | `AWAITING_SPEC_CONFIRMATION`、`AWAITING_APPROVAL` 或 `EVALUATED` |
| `criteria` | 标准条目数组 |
| `clarified_requirement`、`clarification_questions` | 后端需求澄清结果；问题可为空 |
| `evaluations` | 候选人的 `candidate_id`、`name`、`scores`、`verdict`、`tradeoff` |
| `evidence` | 简历原文证据及验证结果，前端仅展示有效证据 |
| `tiers` | 各档候选人数 |
| `change_proposal` | 可空提案，包含 `reason`、`add`、`remove` 等字段 |
| `risk_flags` | 风险标记数组 |
| `feedback_notes` | 流程备注，不是按标准版本一一对应的审计记录 |
| `workflow` | 节点、等待点、模型模式、checkpointer 与节点轨迹 |

`scores`：`2` 表示满足，`0` 表示有反证，`null` 表示无证据。`verdict`：推荐、待核实、不推荐、否决。无证据不等同于不合格。

### 查询版本历史

```bash
curl http://127.0.0.1:8000/calibrations/JOB_ID/history
```

响应是**对象，不是数组**：

```json
{
  "job_id": "任务 ID",
  "spec_versions": [],
  "feedback": [],
  "proposals": [],
  "audit": []
}
```

各数组内容来自数据库。没有 PostgreSQL 配置时返回 `503`；`workflow.history` 仅是 checkpoint 节点轨迹，不能代替版本快照、审批时间和反馈来源关联。

### 错误响应

FastAPI 错误放在 `detail` 中，可能是字符串、包含 `code`/`message` 的对象，或输入校验错误数组。

| HTTP 状态 | 常见原因 | 处理方式 |
|---|---|---|
| `404` | 职位或 checkpoint 不存在 | 核对 ID；memory 模式重启后任务会丢失 |
| `409` | 标准未确认、提案待审批、没有待审批变更、`JOB_BUSY` | 按 `workflow.waiting_for` 继续正确步骤，避免并发写入 |
| `422` | 空文本、重复 ID、非法类型/权重、风险标准、编辑未传标准集 | 修正请求体，不要重复提交相同非法请求 |
| `503` | 数据库未配置或检查失败 | 配置数据库后再查询历史或数据库健康 |

模型调用失败可进入 fallback，不能通过 HTTP `200` 单独证明模型调用成功。

## 独立 PostgreSQL 招聘 API（当前前端未接入）

`/api/hiring/*` 使用独立请求/响应合同，不能与 `/jobs`、`/calibrations/*` 的 ID 和流程混用。需要数据库和 `HIRESPEC_API_TOKEN`（设置后启用非 AI 端点）。所有请求带 `Authorization: Bearer TOKEN`，TOKEN 替换为你设置的令牌，不是模型密钥。

**此模块的模拟演示模式（`offline_demo`）已移除；`get_ai()` 现复用 `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` 接真实模型，未配置 key 时返回 503。**

| 方法 | 路径（共同前缀 `/api/hiring`） | 请求体/用途 |
|---|---|---|
| GET | `/mode` | 模型模式与服务端绑定账号 |
| POST | `/jobs` | `{title, requirement}` 创建职位 |
| GET | `/jobs/{job_id}` | 读取职位、标准、候选人和提案 |
| POST | `/jobs/{job_id}/clarify` | `{answers:{}}` 生成澄清问题 |
| POST | `/jobs/{job_id}/drafts` | `{answers:{问题:回答}}` 生成标准草稿 |
| PUT | `/jobs/{job_id}/draft` | 完整 `SpecDraft`，保存人工草稿 |
| POST | `/jobs/{job_id}/candidates` | `{name,resume_text}`，支持可选文件/解析元数据 |
| POST | `/jobs/{job_id}/spec/approve` | `{draft_id}` 批准初始标准 |
| POST | `/jobs/{job_id}/recompute` | 无正文，重新计算候选人评估 |
| POST | `/jobs/{job_id}/feedback` | `{candidate_id,spec_version,decision,reason}`；decision 为 YES/NO |
| POST | `/jobs/{job_id}/feedback/{feedback_id}/propose` | 无正文，重试提案生成 |
| GET | `/jobs/{job_id}/proposals/{proposal_id}` | 读取提案及审批状态 |
| PUT | `/jobs/{job_id}/proposals/{proposal_id}` | `{changes:[...]}` 编辑提案 |
| POST | `/jobs/{job_id}/proposals/{proposal_id}/accept` | 无正文，接受提案 |
| POST | `/jobs/{job_id}/proposals/{proposal_id}/reject` | 无正文，拒绝提案 |
| PUT | `/jobs/{job_id}/candidates/{candidate_id}/label` | `{label:true,split:"calibration"}`；split 可为 test |

```bash
curl http://127.0.0.1:8000/api/hiring/mode \
  -H 'Authorization: Bearer TOKEN'

curl -X POST http://127.0.0.1:8000/api/hiring/jobs \
  -H 'Authorization: Bearer TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"title":"职位名称","requirement":"招聘需求"}'
```

未启用返回 `503 HIRING_NOT_ENABLED`；令牌无效返回 `401 UNAUTHORIZED`；模型适配未配置返回 `503 AI_NOT_CONFIGURED`。完整字段和流程见 [持久化接口说明](docs/HIRING_PERSISTENCE.md) 与 `/docs` 的 OpenAPI schema。

## 测试

```bash
pytest        # 全离线:LLM 回退 + 内存 checkpointer
```

## 环境变量

- `DATABASE_URL` 或 PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE(可选)
- `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` / `REPLAY_ONLY`：LangGraph 主流程的模型配置，见下文。
- `HIRESPEC_API_TOKEN` / `HIRESPEC_LOCAL_ACTOR` / `HIRESPEC_AI_MODE`：独立 `/api/hiring/*` 接口的本地令牌、账号和模型模式，不与主流程的模型配置混用。
- `CORS_ORIGINS`:逗号分隔,默认允许 `localhost` 和 `127.0.0.1` 的 3000、3001、3002、9023 端口；其他来源需显式配置。修改配置后需要重启后端；设置此变量会覆盖默认来源。

## 外部模型 API 与调用方式

`app/llm.py` 使用 OpenAI 兼容的 Chat Completions 接口：

- 方法：`POST {LLM_BASE_URL}/chat/completions`
- 请求头：`Authorization: Bearer <LLM_API_KEY>`，JSON 正文。
- 请求体：`model`、`messages`（role/content 数组）、`temperature`。
- 读取响应：`choices[0].message.content`，再解析模型输出的 JSON。
- 默认单次超时 30 秒，最多重试 2 次；结果缓存到 `.cache/llm_cache.json`。
- 优先读取缓存；未配置密钥、`REPLAY_ONLY=1` 或模型请求失败时可回退为离线结果。

后端 `.env` 配置示例（**必须自行填写密钥，禁止提交真实密钥**）：

```dotenv
LLM_BASE_URL=https://api.openai-next.com/v1
LLM_API_KEY=填写你自己的服务商密钥
LLM_MODEL=deepseek-v4-1-flash-260910
REPLAY_ONLY=0
```

Base URL 必须带 `/v1`，代码会在其后追加 `/chat/completions`。调用的是所配置的第三方服务商，可能产生费用；招聘需求及简历相关内容可能被发送到该服务商，应先确认数据授权和隐私要求。密钥只放后端，不能使用 `NEXT_PUBLIC_*` 暴露给浏览器。

修改 `.env` 后重启后端。`workflow.model_mode=live` 表示已配置密钥且未开启 replay，并不保证每次请求都成功调用模型（可能命中缓存或失败回退）；实际成功调用需结合模型响应和后端日志确认。`.env` 与 `.cache/` 已被 `.gitignore` 忽略。

## 批量上传简历

### 人工修订招聘意图与标准

- 初始 Spec 待确认时，可调用 `POST /calibrations/{job_id}/requirement/reanalyze`，提交 `requirement` 与 `base_spec_version`。后端复制该草稿的候选人文本，生成新的待确认职位；原草稿保留。
- 初始 Spec 确认后，调用 `POST /calibrations/{job_id}/spec/manual-proposal`，提交完整 `criteria`、当前 `base_spec_version`，以及可选的修订后 `requirement`。这只创建待审批提案，不修改现行标准或排名。
- 审批使用现有 `/calibrations/{job_id}/approval`。接受后才发布下一版本、更新招聘意图并按变动条款重评；拒绝时保留现行版本。编辑待审批提案使用 `/calibrations/{job_id}/proposal/edit`。

至少保留一条标准。未改动条款保持原 ID，新增条款使用未占用的新 ID；敏感条件、重复标准和过期基础版本由后端拦截。

候选人页支持多选或拖拽 PDF、DOC、DOCX、RTF。先创建职位，再上传；上传前后都可以确认标准。文件通过 AnyDoc 0.2.4 在后端本地转为 Markdown 文本，不启用 hosted OCR。扫描件、加密文件和损坏文件按文件提示失败，其余文件继续导入。原始文件不保留，解析文本与来源文件名保存在候选人记录和 checkpoint 中。

- `POST /calibrations/{job_id}/candidates/upload`：multipart 的 `files` 字段，可重复；每批最多 50 份，每份 10 MiB，总计 50 MiB。返回逐文件 `imported` / `duplicate` / `failed` 及最新 `calibration`。同职位按解析文本哈希去重。
- `GET /calibrations/{job_id}/candidates/{candidate_id}/resume`：读取该职位候选人的解析原文。
- `POST /calibrations/{job_id}/candidates/evaluate`：`{"candidate_ids":["..."]}`；使用已确认的标准，只评估尚未完成的候选人。需要真实模型，离线模式返回 `503 LIVE_MODEL_REQUIRED`。标准或提案待确认时返回 409。
- `CalibrationOut.candidates` 返回候选人元数据与 `evaluated`，区分已导入和已评估。

前端逐文件上传、逐候选人评估，显示真实进度；失败后保留已完成结果，重试只处理未完成项。确认标准前上传文件不会调用模型；确认后由用户点击「开始评估」。旧的创建职位时直接提交文本简历流程保持兼容。

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q tests/test_candidate_upload.py
```
