"""只读连接、错误脱敏及数据库健康路由；不依赖远程服务器。"""

from contextlib import nullcontext

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import database
from app.main import app


@pytest.fixture(autouse=True)
def clear_pg_env(monkeypatch):
    for key in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGCONNECT_TIMEOUT"):
        monkeypatch.delenv(key, raising=False)


def test_password_required_and_never_in_repr(monkeypatch):
    with pytest.raises(database.DatabaseCheckError) as caught:
        database.DatabaseSettings.from_env()
    assert caught.value.code == "DB_NOT_CONFIGURED"
    monkeypatch.setenv("PGPASSWORD", "test-password+/only")
    config = database.DatabaseSettings.from_env()
    assert config.host == "127.0.0.1" and config.port == 15432
    assert config.database == "xks"
    assert "test-password" not in repr(config)


@pytest.mark.parametrize("key,value", [
    ("PGPORT", "oops"), ("PGPORT", "0"), ("PGPORT", "65536"),
    ("PGCONNECT_TIMEOUT", "0"), ("PGCONNECT_TIMEOUT", "31"), ("PGHOST", " "),
])
def test_bad_settings_return_safe_errors(monkeypatch, key, value):
    monkeypatch.setenv("PGPASSWORD", "secret")
    monkeypatch.setenv(key, value)
    with pytest.raises(database.DatabaseCheckError) as caught:
        database.DatabaseSettings.from_env()
    assert caught.value.code == "DB_CONFIG_INVALID"
    assert "secret" not in str(caught.value)


class FakeCursor:
    def __init__(self, tables):
        self.tables = tables
        self.queries = []

    def execute(self, sql):
        self.queries.append(sql)

    def fetchone(self):
        return "xks", "postgres", "17.11", "on"

    def fetchall(self):
        return [(table,) for table in self.tables]


class FakeConnection:
    def __init__(self, tables):
        self.cur = FakeCursor(tables)
        self.read_only = False

    def cursor(self):
        return nullcontext(self.cur)


def test_read_only_check_uses_parameters_and_only_metadata(monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "test-password+/only")
    connection = FakeConnection(sorted(database.EXPECTED_TABLES))
    calls = []

    def connect(**kwargs):
        calls.append(kwargs)
        return nullcontext(connection)

    monkeypatch.setattr(database.psycopg, "connect", connect)
    report = database.inspect_database()
    assert report["status"] == "ok" and report["read_only"] is True
    assert connection.read_only is True
    assert calls[0]["password"] == "test-password+/only"
    assert "default_transaction_read_only=on" in calls[0]["options"]
    assert "statement_timeout=5000" in calls[0]["options"]
    assert len(connection.cur.queries) == 2
    assert all(sql.startswith("SELECT ") for sql in connection.cur.queries)
    assert "information_schema.tables" in connection.cur.queries[1]
    assert "test-password" not in str(report)


def test_missing_tables_is_failure(monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "secret")
    monkeypatch.setattr(database.psycopg, "connect", lambda **kw: nullcontext(FakeConnection([])))
    with pytest.raises(database.DatabaseCheckError) as caught:
        database.inspect_database()
    assert caught.value.code == "DB_SCHEMA_MISMATCH"


@pytest.mark.parametrize("error,expected", [
    (psycopg.OperationalError("secret driver error"), "DB_CONNECTION_FAILED"),
    (psycopg.errors.InvalidPassword("secret driver error"), "DB_AUTH_FAILED"),
])
def test_driver_errors_do_not_expose_credentials(monkeypatch, error, expected):
    monkeypatch.setenv("PGPASSWORD", "secret")

    def fail(**kwargs):
        raise error

    monkeypatch.setattr(database.psycopg, "connect", fail)
    with pytest.raises(database.DatabaseCheckError) as caught:
        database.inspect_database()
    assert caught.value.code == expected
    assert "secret" not in str(caught.value)


def test_database_route_unconfigured_is_503_and_old_health_still_works():
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    response = client.get("/health/database")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "DB_NOT_CONFIGURED"


def test_database_route_hides_account_and_table_names(monkeypatch):
    monkeypatch.setattr(database, "inspect_database", lambda: {
        "status": "ok", "database": "xks", "read_only": True,
        "expected_tables_present": True, "user": "postgres", "tables": ["jobs"],
    })
    response = TestClient(app).get("/health/database")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok", "database": "xks", "read_only": True,
        "expected_tables_present": True,
    }
