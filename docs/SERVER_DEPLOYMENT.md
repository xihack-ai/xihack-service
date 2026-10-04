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

当前发布：`/opt/hirespec/releases/release-20261004-feedback`（反馈审核与经理澄清闭环）。上一版 `release-20261003-578a88d-da5169f-webfix` 保留供回滚。GitHub提交号与运行文件哈希记录在服务器 manifest.json 中；发布源码来自 `xihack-ai/xihack-service` 和 `xihack-ai/xihack-web`。
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
