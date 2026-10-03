"""校准全流程测试(离线):分档 → 反馈提案 → 审批(Accept/Edit/Reject)→ 局部重评 → 红线拦截。"""

from fastapi.testclient import TestClient
import pytest

pytestmark = pytest.mark.usefixtures("labelled_calibration_evidence")

from app.main import app

VERDICTS = {"推荐", "待核实", "不推荐", "否决"}

BODY = {
    "title": "后端工程师",
    "requirement": "负责核心服务开发,要求熟悉 Python 与高并发",
    "candidates": [
        {"name": "张三", "resume_text": "5 年后端开发\n主导过 Python Web 项目\n技术栈匹配团队要求\n参与分布式系统设计"},
        {"name": "李四", "resume_text": "3 年前端开发\n负责 React 页面\n仅有前端经验"},
    ],
}


def _client() -> TestClient:
    return TestClient(app)


def _new_job(client: TestClient) -> str:
    return client.post("/jobs", json=BODY).json()["job_id"]


def test_create_job_rule_based_tiers() -> None:
    out = _client().post("/jobs", json=BODY).json()
    assert out["status"] == "EVALUATED"
    assert out["spec_version"] == 1
    assert len(out["criteria"]) == 3                       # 离线回退标准 c1-c3
    assert {e["verdict"] for e in out["evaluations"]} <= VERDICTS

    zs = next(e for e in out["evaluations"] if e["name"] == "张三")
    ls = next(e for e in out["evaluations"] if e["name"] == "李四")
    assert zs["scores"]["c1"] == 2                         # 硬性要求均有简历原文
    assert zs["verdict"] == "推荐"
    assert ls["verdict"] == "不推荐"                      # 全部必须项无证据，本轮不推荐
    assert ls["scores"]["c1"] is None and ls["scores"]["c2"] is None
    assert "不代表候选人没有相关能力" in ls["tier_reason"]


def test_no_evidence_is_not_zero() -> None:
    """三态证据:无证据必须是 null,不能压成 0(否则"没看到"被当成"不合格")。"""
    out = _client().post("/jobs", json=BODY).json()
    zs = next(e for e in out["evaluations"] if e["name"] == "张三")
    assert zs["scores"]["c3"] is None                      # 加分项无证据 → null
    assert zs["scores"]["c1"] == 2


def test_tradeoff_map() -> None:
    out = _client().post("/jobs", json=BODY).json()
    zs = next(e for e in out["evaluations"] if e["name"] == "张三")
    strengths = {s["criterion_id"] for s in zs["tradeoff"]["strengths"]}
    assert {"c1", "c2"} <= strengths
    assert all(s["quote"] for s in zs["tradeoff"]["strengths"])     # 优势必带原文
    gaps = {g["criterion_id"]: g["reason"] for g in zs["tradeoff"]["gaps"]}
    assert gaps["c3"] == "简历中未见证据"


def test_ranking_puts_recommended_first() -> None:
    out = _client().post("/jobs", json=BODY).json()
    assert [e["name"] for e in out["evaluations"]][0] == "张三"
    assert out["tiers"]["推荐"] == 1 and out["tiers"]["不推荐"] == 1


def test_feedback_approval_reeval() -> None:
    c = _client()
    job_id = _new_job(c)

    r = c.post(f"/calibrations/{job_id}/feedback", json={"text": "更看重分布式系统经验"}).json()
    assert r["status"] == "AWAITING_APPROVAL"
    assert r["change_proposal"]["add"]

    # 待审批期间再提交反馈应被拒绝(避免叠提案)
    assert c.post(f"/calibrations/{job_id}/feedback",
                  json={"text": "再看看"}).status_code == 409

    r2 = c.post(f"/calibrations/{job_id}/approval", json={"approved": True}).json()
    assert r2["status"] == "EVALUATED"
    assert r2["spec_version"] == 2
    assert len(r2["criteria"]) == 4
    dist = next(e for e in r2["evaluations"] if e["name"] == "张三")
    assert dist["scores"]["c4"] == 2                       # 局部重评后补上新标准证据


def test_approval_edit_replaces_criteria_and_triggers_veto() -> None:
    """Edit:审批人改写整套标准(可引入否决项),而不是只能在 AI 提案上二选一。"""
    c = _client()
    job_id = _new_job(c)
    c.post(f"/calibrations/{job_id}/feedback", json={"text": "更看重项目主导经验"})

    edited = [
        {"id": "c1", "type": "MUST_HAVE", "text": "相关项目经验", "weight": 3},
        {"id": "v1", "type": "VETO", "text": "仅前端经验", "weight": 3},
    ]
    r = c.post(f"/calibrations/{job_id}/approval",
               json={"action": "edit", "criteria": edited, "note": "按团队口径改写"}).json()
    assert r["spec_version"] == 2
    assert {c["id"] for c in r["criteria"]} == {"c1", "v1"}     # 整串被替换

    by_name = {e["name"]: e for e in r["evaluations"]}
    assert by_name["李四"]["verdict"] == "否决"                  # 否决项命中
    assert by_name["张三"]["verdict"] == "待核实"
    assert by_name["张三"]["judgments"]["v1"] == "UNKNOWN"


def test_approval_rejected_keeps_spec() -> None:
    c = _client()
    job_id = _new_job(c)
    c.post(f"/calibrations/{job_id}/feedback", json={"text": "更看重开源贡献"})
    r = c.post(f"/calibrations/{job_id}/approval", json={"approved": False}).json()
    assert r["spec_version"] == 1
    assert len(r["criteria"]) == 3


def test_bias_redline_blocks_proposal() -> None:
    c = _client()
    job_id = _new_job(c)
    r = c.post(f"/calibrations/{job_id}/feedback",
               json={"text": "更看重性别男、35岁以下"}).json()
    assert r["status"] == "EVALUATED"                      # 不进人工审批
    assert any("年龄" in flag or "性别" in flag for flag in r["risk_flags"])
    assert r["change_proposal"] is None
    assert r["spec_version"] == 1


def test_bias_redline_catches_school_and_age_tone() -> None:
    """seed_data F3:「年轻有冲劲」「985」这类措辞也必须拦下。"""
    c = _client()
    job_id = _new_job(c)
    r = c.post(f"/calibrations/{job_id}/feedback",
               json={"text": "还是找年轻有冲劲的吧,最好是 985"}).json()
    assert r["change_proposal"] is None
    assert r["risk_flags"]


def test_edit_without_criteria_is_422() -> None:
    c = _client()
    job_id = _new_job(c)
    c.post(f"/calibrations/{job_id}/feedback", json={"text": "更看重算法能力"})
    r = c.post(f"/calibrations/{job_id}/approval", json={"action": "edit"})
    assert r.status_code == 422


def test_history_requires_database() -> None:
    c = _client()
    job_id = _new_job(c)
    assert c.get(f"/calibrations/{job_id}/history").status_code == 503


def test_feedback_without_job_404() -> None:
    r = _client().post("/calibrations/nope/feedback", json={"text": "更看重算法"})
    assert r.status_code == 404
