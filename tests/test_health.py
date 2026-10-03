"""骨架健康检查测试。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app


def test_health() -> None:
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.parametrize("origin", [
    f"http://{host}:{port}"
    for host in ("localhost", "127.0.0.1")
    for port in (3000, 3001, 3002)
])
def test_local_frontend_cors(origin: str) -> None:
    response = TestClient(app).options("/jobs", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


def test_untrusted_origin_is_not_allowed() -> None:
    response = TestClient(app).options("/jobs", headers={
        "Origin": "https://untrusted.example",
        "Access-Control-Request-Method": "POST",
    })
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
