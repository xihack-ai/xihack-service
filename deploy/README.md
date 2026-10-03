# Ubuntu 演示部署模板（尚未在服务器执行）

前端以 `HIRESPEC_STATIC_EXPORT=1 NEXT_PUBLIC_API_BASE=/api npm run build` 构建。
Nginx 托管导出的 `out/` 并把 `/api/` 转发到仅监听本机 8010 的 FastAPI。
数据库继续仅监听服务器 127.0.0.1:5432，不对公网开放。

目录约定：`/opt/hirespec/releases/<release>/backend` 为后端，`web` 为前端导出，
`venv` 为独立 Python 环境；`/opt/hirespec/current` 指向当前发布。
`hirespec.service` 使用普通系统用户 hirespec 和一个 worker（当前职位锁是进程内锁）。

数据库密码通过 `systemd-creds encrypt --with-key=host` 保存到
`/etc/credstore.encrypted/hirespec.pgpassword`。启动时 systemd 解密为运行时凭据，
`launch.py` 读取后放入进程环境；代码、命令行和 Git 中不包含密码。

公网入口统一经过 Nginx 的团队演示登录；用户名为 `demo`。访问密码单独加密保存在
`/etc/credstore.encrypted/hirespec.demo-password`，管理员可通过 SSH 读取：

```sh
systemd-creds decrypt /etc/credstore.encrypted/hirespec.demo-password -
```

本目录是部署准备模板，提交它不表示服务器已经部署完成。
模板默认显式 `REPLAY_ONLY=1`，用于离线模型演示，LangGraph 与 PostgreSQL 真正运行。
本地已经验证 DeepSeek 官方 API，但服务器的真实模型密钥和 `REPLAY_ONLY=0`
需要单独配置；Git 不携带本地 `.env` 或任何密钥，不能直接用本模板声称真实模型已上线。
当前 IP HTTP 演示入口尚未配置 HTTPS；只使用虚构简历，真实数据须先配置域名证书与正式组织权限。
开发依赖中 npm audit 报告的 braces 链不随静态前端部署；没有执行破坏性降级。

查看服务与重启：

```sh
systemctl status hirespec nginx --no-pager
systemctl restart hirespec
```

部署前备份保存在 `/opt/hirespec/backups/`。再次发布应先创建新 release，再检查后台健康与页面，
最后切换 current；失败时恢复旧链接并重启 hirespec，不能删除现有数据库或重建 PostgreSQL 容器。
