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

当前发布：`/opt/hirespec/releases/release-20261003-1791004141`。
`/opt/hirespec/current` 为发布链接，backend 是清洁源码安装，web 是静态生产构建。
基础版本：后端 d8af22a、前端 ac95db9，附带本次真实模型凭据/缓存部署修正；发布 manifest 记录逐文件哈希。

部署前备份：`/opt/hirespec/backups/xks_before_release-20261003-1791004141.dump`，已验证可读取归档目录。未删除数据库或重建容器。
升级时上传新 release，先检查健康与调用再切换链接；保留旧 release 供回滚。服务器发布目录不包含 `.git`，仅在本地 pull 不会自动更新服务器。

## 服务器验收

- 官方 API 直接请求200，无缓存、无离线兜底。
- 3份模拟简历完成创建、Spec确认、真实评估、反馈、Reject、Accept、v2局部重评、敏感反馈拦截及数据库历史查询。
- 13条新成功模型回复进入服务器缓存；“使用Python”不足以证明年限或独立上线责任。
- 拒绝提议不改版本和评估；重复Accept被409拒绝，没有再次发布版本。
- 服务重启后v2标准与评估恢复一致，LangGraph checkpoint使用PostgreSQL。
- 服务器内部认证访问页面与API均200；公网未登录访问返回401，证明入口可达且认证生效。
- 端口验证只检查本地监听地址，避免把对端通配地址误认成公开监听。

模拟回归不能代表真实招聘准确率或Precision@5。用户真实招聘数据上线前还需要HTTPS、组织权限与真实标注评测。
