"""PostgreSQL 连接 + 建表。

环境变量二选一:
- DATABASE_URL: 如 postgresql://user:pass@host:5432/xks
- 或标准 libpq 变量 PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE

未配置时 is_configured() 返回 False,业务数据退回进程内存储(仅限本地开发/测试)。

库结构唯一来源是同目录的 schema.sql(幂等,可重复执行)。
"""

import os
from pathlib import Path

import psycopg
import psycopg.rows

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


def is_configured() -> bool:
    return bool(os.getenv("DATABASE_URL") or os.getenv("PGHOST") or os.getenv("PGDATABASE"))


def connect() -> psycopg.Connection:
    """每次调用新建连接(MVP 流量下足够;后续可换连接池)。"""
    url = os.getenv("DATABASE_URL")
    if url:
        return psycopg.connect(url, autocommit=True, row_factory=psycopg.rows.dict_row)
    return psycopg.connect(autocommit=True, row_factory=psycopg.rows.dict_row)


def ensure_schema() -> None:
    """执行 schema.sql;全部语句幂等,重启安全。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
