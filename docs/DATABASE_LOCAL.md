# Windows 本地连接 xks

已提供数据库只读连接检查和启动入口。当前代码没有招聘数据写入接口，也没有数据库迁移。

## 第一个 PowerShell：建立连接通道

```powershell
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:15432:127.0.0.1:5432 root@47.243.206.67
```

输入服务器 SSH 密码，密码字符不会显示。成功后窗口没有新的命令提示符是正常现象；保持打开。

## 第二个 PowerShell：启动本地后端

```powershell
cd "C:\Users\lenovo\Documents\Codex\2026-09-29\new-chat\xihack-service"
.\.venv\Scripts\python.exe scripts\local_backend.py
```

本程序提示 `PostgreSQL 数据库密码（输入不显示）：` 时输入队长给的数据库密码，按 Enter。Windows Terminal 可以用 Ctrl+Shift+V 粘贴，其他终端可右键粘贴；看不到字符是正常的。

密码仅用于当前进程，不写入代码或文件。停止后下次启动要重新输入。

成功后会打印实际数据库名称、版本和表名，然后启动后端。浏览器打开：

- 接口文档：http://127.0.0.1:8000/docs
- 数据库连接检查：http://127.0.0.1:8000/health/database

数据库检查应返回：

```json
{"status":"ok","database":"xks","read_only":true,"expected_tables_present":true}
```

这是连接检查，不代表招聘 AI 功能已经集成。现有知识库页面仍保留。

只检查连接而不启动后端：

```powershell
.\.venv\Scripts\python.exe scripts\local_backend.py --check-only
```

## 首次换电脑时安装依赖

本机已建立 `.venv` 后无需每次安装。新电脑先在项目目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

无需激活虚拟环境，不受 PowerShell 激活脚本执行策略影响。

## 参数放在哪里

默认连接参数已写在程序中，并同步到 `.env.example`：

```dotenv
PGHOST=127.0.0.1
PGPORT=15432
PGDATABASE=xks
PGUSER=postgres
PGCONNECT_TIMEOUT=5
```

如需调整地址、端口或账号，可以复制 `.env.example` 为 `.env`，再改这些非密码参数。启动程序优先读取已有环境变量，其次读取项目根目录的 `.env`。不要覆盖已有 `.env`。

不需要把含特殊字符的密码拼成 DATABASE_URL，程序将连接参数分别传入驱动。当前启动方式不要求保存 PGPASSWORD。

## 常见问题

- `无法访问数据库通道`：第一个窗口尚未建立通道，或已经关闭。
- `Address already in use` / `无法监听端口`：已有进程占用 15432；不要反复启动同一个通道。检查现有 SSH 窗口。
- `DB_AUTH_FAILED`：数据库账号密码错误；SSH 密码和数据库密码分别使用。
- `DB_CONNECTION_FAILED`：连接或查询失败，检查通道、数据库运行状态和账号权限。
- `DB_SCHEMA_MISMATCH`：连接到了缺少预期招聘表的数据库，检查库名，不要手动补建表。
- `8000` 已被占用：启动时加 `--port 8001`，浏览器改用 8001。
- 直接运行 `uvicorn app.main:app`：不会交互询问数据库密码，推荐使用本页的启动入口。

## 后续 AI 集成边界

数据库已存在 8 张招聘表。AI 接入前需要确认 JSON 内容契约、状态映射和审批事务：

- `evaluations` 当前按候选人 × Spec 版本保存一行；单条标准的结果需要放进双方约定的 JSON 结构。
- 顶层 `evaluations.verdict` 的数据库约束为「推荐 / 待定 / 证据不足」；单条标准的 SATISFIED 等结论不能直接写入该列。
- `change_proposals.status` 的现有约束为 BLOCKED / EMPTY / PENDING / APPROVED / REJECTED；需要保留 NEEDS_CLARIFICATION 等 AI 状态的含义，不能让待澄清提议直接进入可批准状态。
- 新版发布、审批幂等和完整排名切换必须放在后端事务及任务流程中完成。

本次连接代码没有 INSERT、UPDATE、DELETE 或 DDL，也没有改变任何现有表。

连接实现参照 [Psycopg 官方文档](https://www.psycopg.org/psycopg3/docs/basic/usage.html)；会话设置参照 [PostgreSQL 官方文档](https://www.postgresql.org/docs/17/runtime-config-client.html)。
