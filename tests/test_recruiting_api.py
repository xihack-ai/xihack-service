"""Local authorization, request contracts and error redaction without database writes."""

from fastapi.testclient import TestClient
import psycopg
import pytest
from pydantic import ValidationError

from app.main import app
from app import recruiting_api
from app.database import DatabaseCheckError
from app.recruiting_api import CandidateIn
from app.recruiting_store import PROPOSAL_STATUS, TIER_VERDICT
from hirespec_loop.models import ProposalStatus


@pytest.fixture(autouse=True)
def isolate_auth(monkeypatch):
    monkeypatch.delenv("HIRESPEC_API_TOKEN", raising=False)
    monkeypatch.delenv("HIRESPEC_AI_MODE", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)  # app.main 的 load_dotenv 会把它装回来
    recruiting_api.get_ai.cache_clear()
    yield
    recruiting_api.get_ai.cache_clear()
    app.dependency_overrides.clear()


def test_disabled_routes_and_missing_token_are_not_public(monkeypatch):
    client = TestClient(app)
    assert client.get("/api/hiring/mode").status_code == 503
    monkeypatch.setenv("HIRESPEC_API_TOKEN", "local-test-token")
    assert client.get("/api/hiring/mode").status_code == 401
    assert client.get("/api/hiring/mode", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_mode_reports_unconfigured_ai_and_actor_is_server_bound(monkeypatch):
    monkeypatch.setenv("HIRESPEC_API_TOKEN", "local-test-token")
    monkeypatch.setenv("HIRESPEC_AI_MODE", "offline_demo")  # 环境变量不再能激活任何模拟模式
    monkeypatch.setenv("HIRESPEC_LOCAL_ACTOR", "manager-test")
    response = TestClient(app).get("/api/hiring/mode", headers={"Authorization": "Bearer local-test-token", "X-Reviewer-ID": "forged"})
    assert response.status_code == 200
    assert response.json()["simulated"] is False
    assert response.json()["model_mode"] == "unconfigured"
    assert response.json()["actor"] == "manager-test"
    assert "local-test-token" not in response.text


def test_body_cannot_supply_a_different_actor(monkeypatch):
    monkeypatch.setenv("HIRESPEC_API_TOKEN", "local-test-token")
    response = TestClient(app).post("/api/hiring/jobs", headers={"Authorization": "Bearer local-test-token"}, json={"title": "job", "requirement": "backend", "reviewer_id": "forged"})
    assert response.status_code == 422


def test_no_implicit_offline_fallback(monkeypatch):
    monkeypatch.setenv("HIRESPEC_API_TOKEN", "local-test-token")
    response = TestClient(app).post("/api/hiring/jobs/00000000-0000-0000-0000-000000000001/recompute", headers={"Authorization": "Bearer local-test-token"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "AI_NOT_CONFIGURED"


@pytest.mark.parametrize("error,code", [
    (psycopg.OperationalError("private-password resume source"), "DATABASE_TASK_FAILED"),
    (DatabaseCheckError("DB_NOT_CONFIGURED", "请配置数据库。"), "DB_NOT_CONFIGURED"),
])
def test_database_failures_are_redacted(monkeypatch, error, code):
    monkeypatch.setenv("HIRESPEC_API_TOKEN", "local-test-token")
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(recruiting_api.RecruitingStore, "create_job", fail)
    response = TestClient(app).post("/api/hiring/jobs", headers={"Authorization": "Bearer local-test-token"}, json={"title": "job", "requirement": "backend"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == code
    assert "private-password" not in response.text


def test_proposal_and_tier_enums_map_to_database_and_verdict_contracts():
    assert PROPOSAL_STATUS == {
        ProposalStatus.PENDING_APPROVAL: "PENDING_APPROVAL",
        ProposalStatus.BLOCKED: "BLOCKED",
        ProposalStatus.NO_CHANGE: "EMPTY",
        ProposalStatus.NEEDS_CLARIFICATION: "NEEDS_CLARIFICATION",
    }
    assert TIER_VERDICT == {
        "QUALIFIED": "推荐",
        "NEEDS_VERIFICATION": "待核实",
        "REQUIREMENT_CONFLICT": "不推荐",
        "VETO_TRIGGERED": "否决",
    }


def test_candidate_in_accepts_resume_file_metadata_and_parser_fields():
    candidate = CandidateIn(
        name="候选人",
        resume_text="Python backend experience",
        file_key="resumes/candidate.pdf",
        original_filename="candidate.pdf",
        mime_type="application/pdf",
        file_size=0,
        parsed_data={"skills": ["Python"]},
        parse_status="COMPLETED",
        parser_version="parser-v1",
    )
    assert candidate.model_dump() == {
        "name": "候选人",
        "resume_text": "Python backend experience",
        "source_file": None,
        "file_key": "resumes/candidate.pdf",
        "original_filename": "candidate.pdf",
        "mime_type": "application/pdf",
        "file_size": 0,
        "parsed_data": {"skills": ["Python"]},
        "parse_status": "COMPLETED",
        "parser_version": "parser-v1",
    }


@pytest.mark.parametrize("field,value", [
    ("file_size", -1),
    ("parse_status", "UNKNOWN"),
    ("parsed_data", ["not", "an", "object"]),
])
def test_candidate_in_rejects_invalid_file_or_parser_fields(field, value):
    payload = {"name": "候选人", "resume_text": "resume", field: value}
    with pytest.raises(ValidationError):
        CandidateIn(**payload)


def test_recruitment_routes_are_registered_before_static_mount():
    paths = TestClient(app).get("/openapi.json").json()["paths"]
    assert "/api/hiring/jobs/{job_id}/proposals/{proposal_id}/accept" in paths
    assert "/api/hiring/jobs/{job_id}/feedback/{feedback_id}/propose" in paths
