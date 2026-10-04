# 服务器部署与使用

2026-10-03 已部署到 `47.243.206.67`，入口为 `http://47.243.206.67/workspace`。
这是带团队演示口令的 IP HTTP 入口，仅使用模拟简历演示；尚未配置 HTTPS 或正式组织权限。

## 登录

网页会弹出演示登录框，用户名 `demo`。管理员在已登录的服务器终端读取口令：

```sh
systemd-creds decrypt --name=demo-password /etc/credstore.encrypted/hirespec.demo-password -
```

口令未写入 Git、部署包或验收报告。DeepSeek 密钥仅由后端运行时读取，浏览器无需模型密钥。

## 运行状态与配置

- Nginx 监听80，静态页面与 `/api` 同源代理均受演示口令保护。
- FastAPI 监听 `127.0.0.1:8010`，单 worker；PostgreSQL Docker 仍监听 `127.0.0.1:5432`，数据库 xks。
- 服务用户 hirespec，`hirespec.service` 开机启动，真实模型模式 `REPLAY_ONLY=0`。
- DeepSeek 官方地址 `https://api.deepseek.com`，模型 `deepseek-flash`。
- 数据库密码与模型密钥分别保存为主机加密 systemd 凭据；凭据 ID 为 `pgpassword` 和 `llmapikey`，与 `--name` 一致。
- 模型缓存位于 `/var/lib/hirespec/llm_cache.json`，使用服务用户的私有权限。

```sh
systemctl status hirespec nginx --no-pager
curl -fsS http://127.0.0.1:8010/health
systemctl restart hirespec
journalctl -u hirespec -n 50 --no-pager
```

## 发布版本与备份

当前发布：`/opt/hirespec/releases/release-20261004-company`（指定公司名单与类型保留，防止重复追问）。上一版 `release-20261004-requirement` 保留供回滚。此前 GitHub 提交号与运行文件哈希记录在对应版本 manifest.json 中；本次需求澄清修复从本地已验证源码部署，尚未推送到 GitHub，新版 manifest 保存逐文件 SHA-256，不冒充已有 GitHub 提交。
`/opt/hirespec/current` 为发布链接，backend 是清洁源码安装，web 是静态生产构建。
原始发布 `/opt/hirespec/releases/release-20261003-1791004141` 保留供回滚；新发布 manifest 记录两个仓库的提交和上传包 SHA-256。`release-20261003-578a88d-da5169f` 的后端正确，但其前端误取了旧 `out/`，不应作为前端回滚目标。

更新前备份：`/opt/hirespec/backups/xks_before_release-20261003-578a88d-da5169f.dump`，已验证可读取归档目录。未删除数据库或重建容器。
升级时上传新 release，先检查健康与调用再切换链接；保留旧 release 供回滚。服务器发布目录不包含 `.git`，仅在本地 pull 不会自动更新服务器。

2026-10-03 更新验收：新 Python 环境 `pip check` 通过；静态网页和同源 `/api/health` 在演示口令认证后均返回 200；后端健康报告 PostgreSQL 正常；强制 LIVE 的 DeepSeek 小请求成功。新分档规则对所有必须项均无证据的样本返回“不推荐”，逐条分数仍为无证据。`hirespec.service` 运行稳定且未自动重启。

前端纠错验收：设置 `HIRESPEC_STATIC_EXPORT=1` 且自定义 `HIRESPEC_BUILD_DIR=.next-deploy-20261003` 时，**导出内容位于该自定义目录本身**，不能打包仓库中已有的 `out/`。正确构建的 `workspace.html` SHA-256 为 `a6029de10e2785a1b7f2e8d7b0e409779a0571b1f7105e92afe14677b6e433fa`；Nginx 实际响应与该哈希一致，页面引用的 14 个静态资源均可读取。

## 服务器验收

- 官方 API 直接请求200，无缓存、无离线兜底。
- 3份模拟简历完成创建、Spec确认、真实评估、反馈、Reject、Accept、v2局部重评、敏感反馈拦截及数据库历史查询。
- 13条新成功模型回复进入服务器缓存；“使用Python”不足以证明年限或独立上线责任。
- 拒绝提议不改版本和评估；重复Accept被409拒绝，没有再次发布版本。
- 服务重启后v2标准与评估恢复一致，LangGraph checkpoint使用PostgreSQL。
- 服务器内部认证访问页面与API均200；公网未登录访问返回401，证明入口可达且认证生效。
- 端口验证只检查本地监听地址，避免把对端通配地址误认成公开监听。

模拟回归不能代表真实招聘准确率或Precision@5。用户真实招聘数据上线前还需要HTTPS、组织权限与真实标注评测。

## 2026年10月4日反馈澄清更新

- SAFE / NEEDS_CLARIFICATION / BLOCKED 分别处理；大厂、学校、主动性未明确时向经理追问，不作为敏感条件拦截。
- 前端删除通用能力替换按钮，加入审核范围、原反馈编辑、逐题回答及本次问答历史。
- 本地 215 passed、18 skipped；前端类型、ESLint、生产构建通过。
- 真实 DeepSeek LIVE 验证五类边界输入；模拟职位在真实 PostgreSQL 完成澄清→提议→批准 v2→敏感反馈拦截。验证任务 `e44a9c4e-3530-4853-8b16-8174340c830d`。
- 重启后版本与状态恢复，数据库审计可读；网站认证访问200，公网未认证401；14个静态资源读取成功。
- workspace.html SHA-256：`1213ad18607bcc44c3e09293badbee078fd2a15447c1f6c530dd8a37ca8bacf6`。
- 部署前数据库备份 `/opt/hirespec/backups/xks_before_feedback_20261004.dump`。未改表结构、未重算历史任务。
- 接口及政策范围详见 [反馈审核说明](FEEDBACK_REVIEW.md)。

## 创建前需求澄清更新

2026-10-04 已上线 `release-20261004-requirement`。入口 `http://47.243.206.67/workspace?v=20261004-requirement#requirement`，团队演示账号和口令不变。新增 `/requirements/clarify`，将模糊需求作为逐题问答而不是创建失败的顶部警告；回答再次审核后生成待确认草稿。

- 本地：221 passed、18 skipped，前端类型、源码 ESLint、静态生产构建通过。
- 切换前：新版独立运行环境使用真实 DeepSeek LIVE 和真实 PostgreSQL，以模拟输入完成 3 个澄清问题→经理回答→SAFE→标准草稿暂停→人工确认→1 位候选人评估；敏感要求返回 BLOCKED。
- 模拟验收职位：`7209ab9c-ab62-43fb-bd9d-d6ff249c4f4c`。不作为真实招聘准确率证明。
- 切换后：认证页面与同源 API 通过；12 个页面 JS/CSS 资源读取成功；新版问答组件文案存在；新任务和旧版 v2 任务都能恢复。外部未登录请求返回 401，与演示认证一致。
- `workspace.html` SHA-256：`fd39133c6dc4284e8327e581175c5c47515ba9f929ca9d7355080d5213f8c381e`，与 Nginx 响应一致。
- 备份：`/opt/hirespec/backups/xks_before_requirement_20261004.dump`；未改表结构。新版本保留独立运行环境，检查失败自动回滚旧发布链接。
- 问答契约见 [创建前需求澄清说明](REQUIREMENT_CLARIFICATION.md)。

## 指定公司名单修复发布

2026-10-04 已上线 `release-20261004-company`，入口 `http://47.243.206.67/workspace?v=20261004-company#requirement`。生成标准必须保留已澄清的公司范围；审核同时接收标准类型，生成模型漏写条件时先自动修复草稿。公司问题的输入示例改为公司名单。

- 223 passed、18 skipped；源码检查及静态生产构建通过。
- 新版本切换前用真实 DeepSeek LIVE / PostgreSQL 复测“腾讯 阿里 字节 美团”“优先考虑项，非必须”，连续两次需求审核 SAFE；生成完整名单加分项，确认及一位模拟候选人评估通过，敏感筛选仍 BLOCKED。
- 模拟任务 `94c09fc5-aecb-4a5b-b369-8b94e2a0514c`。不构成真实招聘准确率证明。
- 切换后验证 12 个 JS/CSS 资源、页面 SHA-256、同源 API、新旧任务恢复。公网未登录请求返回 401，演示账号不变。
- workspace.html SHA-256：`56096b50fbadf79cfaf5bc00864c92c3530f6a5218e1b20b8e891a540814a8dd`。
- 数据库备份 `/opt/hirespec/backups/xks_before_company_20261004.dump`，上一版目录保留；未修改表结构。
