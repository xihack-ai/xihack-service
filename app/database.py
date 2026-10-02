"""PostgreSQL 只读连接检查；不创建表，不读取招聘业务记录。"""

import os
from dataclasses import dataclass, field

import psycopg

EXPECTED_TABLES = frozenset({
    "audit_events", "candidate_labels", "candidates", "change_proposals",
    "evaluations", "feedback", "jobs", "spec_versions",
})


class DatabaseCheckError(Exception):
    """可安全返回给客户端的错误，不包含驱动原始消息或密码。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)
    connect_timeout: int = 5

    @classmethod
    def from_env(cls, *, require_password: bool = True) -> "DatabaseSettings":
        try:
            port = int(os.getenv("PGPORT", "15432"))
            timeout = int(os.getenv("PGCONNECT_TIMEOUT", "5"))
        except ValueError:
            raise DatabaseCheckError("DB_CONFIG_INVALID", "数据库端口和超时必须是整数。") from None
        if not 1 <= port <= 65535 or not 1 <= timeout <= 30:
            raise DatabaseCheckError("DB_CONFIG_INVALID", "数据库端口或超时超出有效范围。")
        password = os.getenv("PGPASSWORD", "")
        if require_password and not password:
            raise DatabaseCheckError(
                "DB_NOT_CONFIGURED", "请用 scripts/local_backend.py 启动并输入数据库密码。"
            )
        values = {
            "host": os.getenv("PGHOST", "127.0.0.1"),
            "database": os.getenv("PGDATABASE", "xks"),
            "user": os.getenv("PGUSER", "postgres"),
        }
        if any(not value.strip() for value in values.values()):
            raise DatabaseCheckError("DB_CONFIG_INVALID", "数据库地址、库名和用户名不能为空。")
        return cls(**values, port=port, password=password, connect_timeout=timeout)


def inspect_database(settings: DatabaseSettings | None = None) -> dict:
    """通过真实认证检查库名、只读事务及表结构；失败不伪装成成功。"""
    config = settings or DatabaseSettings.from_env()
    try:
        # 参数分别传入，密码中的 + / 等字符无须 URL 编码。
        # 会话从建立时即为只读，并限制 SQL 执行时间。
        with psycopg.connect(
            host=config.host, port=config.port, dbname=config.database,
            user=config.user, password=config.password,
            connect_timeout=config.connect_timeout,
            application_name="xihack-local-check",
            options="-c default_transaction_read_only=on -c statement_timeout=5000",
        ) as conn:
            conn.read_only = True
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT current_database(), current_user, "
                    "current_setting('server_version'), current_setting('transaction_read_only')"
                )
                database, user, server_version, read_only = cursor.fetchone()
                cursor.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                    "ORDER BY table_name"
                )
                tables = [row[0] for row in cursor.fetchall()]
    except psycopg.Error as exc:
        if exc.sqlstate in {"28P01", "28000"}:
            code, message = "DB_AUTH_FAILED", "数据库认证失败，请检查 PostgreSQL 用户名和密码。"
        else:
            code, message = (
                "DB_CONNECTION_FAILED",
                "数据库连接或只读检查失败，请确认 SSH 通道仍打开、连接配置正确。",
            )
        raise DatabaseCheckError(code, message) from None

    if database != config.database or read_only != "on":
        raise DatabaseCheckError("DB_CHECK_FAILED", "数据库名称或只读事务检查未通过。")
    missing = sorted(EXPECTED_TABLES.difference(tables))
    if missing:
        raise DatabaseCheckError("DB_SCHEMA_MISMATCH", "缺少预期招聘表：" + ", ".join(missing))
    return {
        "status": "ok", "database": database, "user": user,
        "server_version": server_version, "read_only": True,
        "tables": tables, "expected_tables_present": True,
    }
