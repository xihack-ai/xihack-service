# Ubuntu 真实模型演示部署

前端以 `HIRESPEC_STATIC_EXPORT=1 NEXT_PUBLIC_API_BASE=/api npm run build` 构建。
Nginx 托管导出的 `out/` 并把 `/api/` 转发到仅监听本机 8010 的 FastAPI。
数据库继续仅监听服务器 127.0.0.1:5432，不对公网开放。

目录约定：`/opt/hirespec/releases/<release>/backend` 为后端，`web` 为前端导出，
`venv` 为独立 Python 环境；`/opt/hirespec/current` 指向当前发布。
`hirespec.service` 使用普通系统用户 hirespec 和一个 worker（当前职位锁是进程内锁）。

数据库密码通过 `systemd-creds encrypt --name=pgpassword --with-key=host` 保存到
`/etc/credstore.encrypted/hirespec.pgpassword`。启动时 systemd 解密为运行时凭据，
`launch.py` 读取后放入进程环境；代码、命令行和 Git 中不包含密码。

DeepSeek 密钥同样加密保存为 `/etc/credstore.encrypted/hirespec.llmapikey`，
由 `LoadCredentialEncrypted=llmapikey:...` 在运行时解密。
`hirespec.service` 使用官方地址、`deepseek-flash` 和 `REPLAY_ONLY=0`；
两项运行时凭据不能为空。输入密钥时通过受保护的标准输入传输，不写入源码或公开目录。

公网入口统一经过 Nginx 的团队演示登录；用户名为 `demo`。访问密码单独加密保存在
`/etc/credstore.encrypted/hirespec.demo-password`，管理员可通过 SSH 读取：

```sh
systemd-creds decrypt --name=demo-password /etc/credstore.encrypted/hirespec.demo-password -
```

提交模板不表示服务器已经部署完成，必须以服务健康、模型真实请求和审批闭环的服务器验证结果为准。
Git 不携带本地 `.env` 或任何密钥；仅拉代码无法启动真实模型服务。
模型缓存写入 `/var/lib/hirespec/llm_cache.json`，由服务用户拥有且使用私有权限，
不会写入只读发布目录，版本回滚也不会带走业务数据库。
当前 IP HTTP 演示入口尚未配置 HTTPS；只使用虚构简历，真实数据须先配置域名证书与正式组织权限。
开发依赖中 npm audit 报告的 braces 链不随静态前端部署；没有执行破坏性降级。

查看服务与重启：

```sh
systemctl status hirespec nginx --no-pager
systemctl restart hirespec
```

部署前备份保存在 `/opt/hirespec/backups/`。再次发布应先创建新 release，再检查后台健康与页面，
最后切换 current；失败时恢复旧链接并重启 hirespec，不能删除现有数据库或重建 PostgreSQL 容器。
