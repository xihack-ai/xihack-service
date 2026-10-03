# xihack-service

招聘标准校准服务:FastAPI 管业务,LangGraph 管有状态、可暂停恢复的校准工作流。

```text
Next.js → FastAPI → LangGraph → {LLM, PostgreSQL}
```

Windows 本地连接招聘数据库 `xks`：见 [操作步骤](docs/DATABASE_LOCAL.md)。
使用 `python scripts/local_backend.py` 隐藏输入数据库密码；`/health/database` 仅检查连接和表结构。

招聘闭环真实数据库读写：见 [接口与运行说明](docs/HIRING_PERSISTENCE.md)。
模型与 LangGraph 接入边界：见 [队长集成说明](docs/HIRING_INTEGRATION.md)。
`python scripts/local_backend.py --recruiting-demo` 启用明确模拟 AI 的持久化接口；
`python scripts/recruiting_demo.py` 创建一个独立模拟职位并跑完审批、重算与重启恢复验证。

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
| POST | `/jobs` | 建职位+候选人并启动校准；`require_spec_confirmation=true` 时停在首次标准确认（默认 false=旧行为一次跑完） |
| GET | `/calibrations/{job_id}` | 查看当前标准/评估/状态/workflow 元信息 |
| POST | `/calibrations/{job_id}/spec/confirm` | 首次标准确认：body `{"criteria": CriterionIn[]}`（完整标准集，可编辑）；确认后 save_spec → 抽证 → 评估 |
| POST | `/calibrations/{job_id}/feedback` | 提交 HR 反馈 → 产出标准变更提案，停在待审批；停在 `AWAITING_SPEC_CONFIRMATION` 时返回 409 |
| POST | `/calibrations/{job_id}/approval` | Accept / Edit / Reject 恢复审批 → 选择性重评；Edit 的风险/格式校验在 resume 前，非法返回 422 |

`status`：`AWAITING_SPEC_CONFIRMATION`（等确认初始标准）/ `EVALUATED`（已评估）/ `AWAITING_APPROVAL`（等人点批准，响应带 `change_proposal`）。

响应中的 `workflow` 字段全部来自真实 checkpoint 状态（`get_state().next` + 节点轨迹，不虚构）：
`{next_nodes, completed_nodes, waiting_for: "spec_confirmation"|"human_approval"|null,
model_mode: "offline_fallback"|"live", checkpointer: "memory"|"postgres", history}`。
`workflow.history` 是当前图状态里的节点操作记录（初始/反馈/审批，随 checkpoint 持久化）；
`/calibrations/{id}/history` 端点仍是数据库决策日志，不配 PG 依旧 503，两者互不影响。
同一 job 的变更接口串行：并发提交直接 409 `JOB_BUSY`（非阻塞，不排队）。

同 ID 标准的编辑语义：改文本 → 旧证据作废重抽；只改类型/权重 → 不重抽，仅重新分档聚合；
删除的标准真正从标准集移除，且新增标准不复用已删除的 ID。

## 测试

```bash
pytest        # 全离线:LLM 回退 + 内存 checkpointer
```

## 环境变量

- `DATABASE_URL` 或 PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE(可选)
- `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` / `REPLAY_ONLY`(可选)
- `CORS_ORIGINS`:逗号分隔,默认允许 `localhost` 和 `127.0.0.1` 的 3000、3001、3002 端口；其他来源需显式配置。修改配置后需要重启后端；设置此变量会覆盖默认来源。
