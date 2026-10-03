# 招聘闭环：真实数据库持久化

## 已实现

本地 FastAPI 提供职位、文本简历、澄清、Spec 草稿、初始批准、逐条评估、反馈、修改提议、编辑、接受、拒绝、重算和候选人标注接口。状态保存到已有 `xks` 8 张表，未创建或修改表结构。

模型接口仍可替换。当前提供的是**明确启用的模拟 AI**，仅支持预置需求、回答、简历和反馈；不支持的输入会报模型任务错误，不会偷偷给出伪造评估。没有真实模型配置时不自动回退为模拟结果。

## 本机运行

保持 SSH 通道窗口打开。另一个 PowerShell：

```powershell
cd "C:\Users\lenovo\Documents\Codex\2026-09-29\new-chat\xihack-service"
.\.venv\Scripts\python.exe scripts\local_backend.py --recruiting-demo
```

输入 PostgreSQL 数据库密码。终端会打印**本次本地访问令牌**，它不是数据库密码；在 http://127.0.0.1:8000/docs 点 Authorize，填入令牌即可调用 `/api/hiring` 下的接口。无需自己输入 `Bearer` 前缀。

默认账号为 `manager-local`，由服务器环境 `HIRESPEC_LOCAL_ACTOR` 绑定，不能通过请求正文或自报 reviewer_id 冒充其他账号。创建的职位有归属标记，当前账号只能操作自身通过此模块创建的职位，旧系统职位不会被此模块修改。

这是绑定本机 127.0.0.1 的开发认证，**还不是部署用的用户登录与组织权限系统**。正式多人部署应替换 `actor` 依赖，将令牌验证结果映射到真实用户和职位权限。

## 一条命令运行完整数据库演示

```powershell
.\.venv\Scripts\python.exe scripts\recruiting_demo.py
```

隐藏输入数据库密码后，程序创建一个独立的 `[HireSpec 模拟样本] 数据库闭环` 职位，导入 6 份模拟简历，并执行：

1. 澄清 → 草稿 → 人工批准 v1 → 保存 24 条单标准评估。
2. 保存拒绝反馈 → 保存待审批提议；此时仍为 v1。
3. 批准修改 C02 → 保存 v2 → 6 条重评、18 条缓存复用。
4. 用新实例读取数据库，验证重新启动后状态仍存在，重复批准不创建 v3。
5. 提交敏感反馈，保存 BLOCKED 提议，禁止批准。
6. 写入一条明确为模拟的校准集标注，用于验证 `candidate_labels` 写入。不是经理真实标签，也不用于宣称模型效果。

演示保留这个独立职位供复查；不会删除或覆盖团队已有职位。候选人 UUID 随机生成，实际排序由档位、满足的加分项数量、稳定 UUID 决定，不写死排名移动。

## 前端接口顺序

所有招聘接口带 `Authorization: Bearer <本地访问令牌>`。服务端生成 UUID，不接受客户端指定职位所有者。

| 方法与路径（共同前缀 `/api/hiring`） | 请求/行为 |
|---|---|
| GET `/mode` | 返回模拟标记及服务器绑定账号 |
| POST `/jobs` | `{title, requirement}`，保存职位 |
| GET `/jobs/{job_id}` | Spec、完整可见排名、重算状态、草稿、候选人、提议 |
| POST `/jobs/{job_id}/clarify` | `{answers:{}}`，返回最多 3 个问题 |
| POST `/jobs/{job_id}/drafts` | `{answers:{...}}`，生成并保存草稿 |
| PUT `/jobs/{job_id}/draft` | 完整 SpecDraft；安全复检后保存新草稿，不发布 |
| POST `/jobs/{job_id}/candidates` | `{name,resume_text,source_file?,file_key?,original_filename?,mime_type?,file_size?,parsed_data?,parse_status?,parser_version?}`，保存不可变简历文本及可选文件/解析元数据；相同文本幂等复用 |
| POST `/jobs/{job_id}/spec/approve` | `{draft_id}`，批准 v1 并评估已导入候选人 |
| POST `/jobs/{job_id}/feedback` | `{candidate_id,spec_version,decision:"YES"或"NO",reason}` |
| POST `/jobs/{job_id}/feedback/{feedback_id}/propose` | 模型任务失败后，以已保存反馈重试生成提议 |
| GET `/jobs/{job_id}/proposals/{proposal_id}` | 当前提议、数据库审批状态、diff、是否可批准 |
| PUT `/jobs/{job_id}/proposals/{proposal_id}` | `{changes:[...]}`，只保存编辑草稿 |
| POST `/jobs/{job_id}/proposals/{proposal_id}/accept` | 重新安全检查、锁定当前版本、事务批准并重算 |
| POST `/jobs/{job_id}/proposals/{proposal_id}/reject` | 保存拒绝决策，Spec 与排名不变 |
| POST `/jobs/{job_id}/recompute` | 重算失败或新增候选人后重试；缓存可跨进程复用 |
| PUT `/jobs/{job_id}/candidates/{candidate_id}/label` | `{label:true或false,split:"calibration"或"test"}`；标签不传入 AI |

初始演示输入：

```json
{
  "title": "高级 AI 后端",
  "requirement": "我要一个能独立负责 AI 产品的高级后端"
}
```

澄清回答：

```json
{
  "answers": {
    "独立负责范围": "技术方案和上线；运维经验作为加分项。",
    "AI 经历门槛": "先接受参与 AI 项目，是否必须独立负责生产系统待样本校准。",
    "是否管理团队": "不要求管理团队；必须能交付 Python 后端，云部署经验加分。"
  }
}
```

示例反馈理由为 `他只是参与项目，我们需要真正负责过上线的人。`。敏感理由如 `女性不适合这个岗位。` 会返回 BLOCKED。演示只支持已明确列出的模拟输入，自己的其他简历需要接入真实模型适配器。

## 8 张表的兼容方式

| 表 | 存储内容 |
|---|---|
| `jobs` | 原始需求、当前已批准版本、任务状态 |
| `spec_versions` | 不可覆盖的正式版本；`criteria` 保持标准数组；来源提议 ID |
| `candidates` | UUID、姓名、完整简历文本及 SHA-256；可选 `file_key`、原始文件名、MIME 类型、文件大小、解析 JSON、解析状态、解析器版本和 `parsed_at`；同职位相同文本去重。当前仅持久化已提供的元数据，不包含文件上传或解析器 |
| `evaluations` | 一候选人 × 一版本一行；`scores` 是带契约版本的逐标准记录 JSON，**没有总分** |
| `feedback` | 原始理由、候选人、版本、创建人；YES/NO 决策保存在对应审计事件 |
| `change_proposals` | 原始 AI 提议、编辑草稿/最终审批、风险状态、审批人和时间 |
| `candidate_labels` | 校准/测试标注，独立于 Prompt 和反馈流程 |
| `audit_events` | `HIRESPEC_` 命名空间内的归属、草稿、完整 Spec 元数据、决策、缓存及排名发布日志 |

额外工作流元数据采用 `contract: "hirespec-pg-v1"` 标记，不更改原表约束。前端以招聘 API 契约读取，不直接猜测旧系统 JSON 的含义。

`evaluations.verdict` 按现有约束映射为「推荐 / 待核实 / 不推荐 / 否决」：`QUALIFIED` → 推荐，`NEEDS_VERIFICATION` → 待核实，`REQUIREMENT_CONFLICT` → 不推荐，`VETO_TRIGGERED` → 否决。原始档位保留在 `scores.tier`，单条 `SATISFIED / NOT_SATISFIED / NO_EVIDENCE` 保留在 records 中；不能仅用顶层中文状态恢复所有语义。

提议 AI 状态与审批状态分开：

| AI 状态 | 数据库状态 | 能否批准 |
|---|---|---|
| PENDING_APPROVAL | PENDING_APPROVAL | 是，仍须提交前复检 |
| NEEDS_CLARIFICATION | NEEDS_CLARIFICATION | 否；保留追问，不变成可批准提议 |
| NO_CHANGE | EMPTY | 否 |
| BLOCKED | BLOCKED | 否 |
| 已接受 / 已拒绝 | APPROVED / REJECTED | 再次相同决策返回原收据；相反决策拒绝 |

`one_pending_per_job` 限制一个职位同时只有一个待审批提议。新反馈会先保存；如已有待审批提议，响应给出 feedback_id，处理上一提议后可重试生成。

可先批准初始标准再导入简历：此时返回 `snapshot:null` 和 `requires_candidates:true`；导入后调用 `/recompute`。新增候选人时旧排名继续保留，`pending_candidate_ids` 列出尚未进入可见排名的候选人，`snapshot_stale` 表明需要重算。

## 事务、失败与恢复

- 变更前读当前 Spec 生成 diff；安全检查在发布事务之外执行，事务内再次检查提议内容及基础版本，防止并发修改。
- 所有职位写入使用相同的 PostgreSQL advisory lock 和职位行锁；模型评估期间使用会话锁，其他写入快速返回 JOB_BUSY。
- 只有批准操作才插入下一版本；提议状态、版本和审计在同一事务内提交。重复接受不会新增版本或调用评估。
- `jobs.current_spec_version` 表示已批准版本；**不是前端可见排名版本**。前端以 `visible_spec_version` 和 `visible_snapshot` 显示排名。
- 全部候选人评估成功后，在一个事务中保存整版评估与 RANKING_PUBLISHED 日志；前端再切换。不能把 current_spec_version 直接套在旧列表上。
- 模型失败时反馈仍保存，已批准版本仍保存，上一版完整排名继续可见，状态为 FAILED。`POST /recompute` 可重试。
- 保存整版评估的事务失败时，全部新版评估与排名发布日志一起回滚，旧排名继续可见；已经通过校验的单条结果可单独保存为缓存供重试。
- 已完成的单条评估缓存以审计批次持久化，键包括职位、候选人、简历哈希、条款 ID、语义哈希、评估器版本。类别改变复用评估；改变 C02 语义只重评 C02。
- 进程被强制停止时，数据库自动释放会话锁。残留 RUNNING 状态可经 `/recompute` 重试；尚未实现自动后台恢复调度。

## 测试与当前边界

本地无数据库测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_database.py tests/test_recruiting_api.py
```

真实数据库测试在 `tests/test_recruiting_postgres.py`，必须显式设置 `HIRESPEC_RUN_DB_TESTS=1` 并提供运行时数据库密码。每项测试创建独立 UUID 职位，并只清理自己创建且归属校验通过的测试职位；不按模糊名称批量删除。

当前完成持久化 MVP。尚未完成真实模型适配、文件上传、简历解析器、前端页面联调、生产账号权限、连接池、后台任务调度、同一候选人的多版简历管理和部署。当前简历记录不可变；提供文件或解析字段时仅保存对应元数据，新上传仍视为独立候选人，不覆盖旧简历证据。

事务与锁的行为参照 [PostgreSQL 官方锁文档](https://www.postgresql.org/docs/17/explicit-locking.html) 和 [Psycopg 官方事务文档](https://www.psycopg.org/psycopg3/docs/basic/transactions.html)。
