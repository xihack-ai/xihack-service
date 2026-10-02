# HireSpec：模型与 LangGraph 集成说明

本次交付包含 `hirespec_loop/` AI 模块、`app/recruiting_service.py` 持久化闭环、`app/recruiting_store.py` 数据库兼容层和 `/api/hiring` 接口。未添加招聘 LangGraph 图，队长可以在现有图中调用这些服务。

## 安装与验证

在仓库根目录、Python 3.11+ 虚拟环境中执行，Windows/macOS/Linux 均可：

```sh
python -m pip install -e ".[dev]"
python -m pytest -q tests/hirespec tests/test_database.py tests/test_recruiting_api.py
python -m hirespec_loop.demo
```

这组测试共 105 项，不需要数据库。历史知识库的全仓测试存在样本 tier `LEGEND` 与原 Schema 不一致的收集错误，招聘测试通过不代表原知识库全量测试通过。

真实数据库测试另见 `HIRING_PERSISTENCE.md`；需显式启用 `HIRESPEC_RUN_DB_TESTS=1`。示例脚本 `python scripts/recruiting_demo.py` 会保留一个新的独立模拟职位，不能在生产库随意反复运行。

## 接真实模型

`hirespec_loop.llm.JSONClient` 是唯一模型边界：

```python
async def generate_json(system_prompt, payload, output_schema):
    # 用你们的 SDK 发出结构化输出请求；返回 JSON 字符串或 dict。
    ...
```

将实现注入 `CallableJSONClient`，再构造 `HireSpecAI(client)`。`provider`、确切 `model` 版本和非敏感调用设置进入缓存身份；不要把 API key 放入 settings。模块会校验 JSON/Schema，失败修复一次；引用另经原文验证，调用失败不会变成无证据。

FastAPI 的接入点是 `app.recruiting_api.get_ai` 与 `get_loop`；当前仅允许显式 `offline_demo`，替换真实模型后也要明确传入真实 `model_mode`。仅设置原知识库 `LLM_API_KEY` 不会自动启用招聘模型。

## 接 LangGraph

工作流节点采用：保存反馈 → 生成并保存提议 → 等待人工审批 → 批准并重算。服务方法对应 `PersistentLoop.feedback/propose/edit/accept/recompute`；拒绝调用 `RecruitingStore.reject`。

- `feedback` 同时保存反馈并生成提议；模型失败会保留 feedback_id，随后用 `propose` 重试。不要重复创建同一条反馈。
- 在审批等待节点之前完成提议生成；审批节点恢复执行时不能再次调用模型生成提议。
- `accept` 内部已经发布版本并触发重算。图中正常接受后不要再无条件调用一次重算；仅失败恢复或新增简历时调用 `recompute`。
- 将 job_id、feedback_id、proposal_id 和审批决定作为图状态引用。数据库是 Spec、提议与可见排名的事实来源，checkpoint 不能覆盖数据库版本。
- 同一提议重复批准返回原收据；若批准已成功、重算失败，当前版本已升级，但旧完整排名仍可见。先读取职位状态，再调用 `recompute` 恢复。
- `BLOCKED` 和含糊理由没有批准入口；人工编辑仅保存草稿，仍需独立 Accept 与安全复检。
- 前端显示 `visible_spec_version` + `visible_snapshot`，不要用 current_spec_version 标记尚未完成的新排名。

持久化代码只操作带本模块归属标记的职位。认证仍是本机开发用 Bearer token；部署前需替换为真实用户身份、组织权限和职位访问控制。

## 已实际验证的种子闭环

真实 PostgreSQL 中已写入 8 张现有表，6 份模拟简历从 v1 到 v2：24 条初始评估，修改 C02 后只重评 6 条、复用 18 条。审批前版本不变、重复审批幂等、重新创建服务实例读回和敏感反馈 BLOCKED 均已验证。简历及模型回复仍是预置模拟数据，尚未验证真实模型效果；数据库结构未修改，密码未随代码保存。
