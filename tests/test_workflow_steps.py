"""分步校准流程回归:首次标准确认关卡、interrupt/resume、快照恢复、
风险编辑、remove 语义、同 ID 文本变动重抽、workflow.history。

全程离线(REPLAY_ONLY + 内存 checkpointer,见 conftest.py)。
"""

from fastapi.testclient import TestClient

from app.main import app

BODY = {
    "title": "后端工程师",
    "requirement": "负责核心服务开发,要求熟悉 Python 与高并发",
    "candidates": [
        {"name": "张三", "resume_text": "5 年后端开发\n主导过 Python Web 项目\n分布式系统设计经验\n参与分布式系统设计"},
        {"name": "李四", "resume_text": "3 年前端开发\n负责 React 页面"},
    ],
}


def _client() -> TestClient:
    return TestClient(app)


def _confirmed_job(client: TestClient) -> str:
    """require_spec_confirmation=true 建职位并确认初始标准,返回 job_id。"""
    out = client.post("/jobs", json={**BODY, "require_spec_confirmation": True}).json()
    job_id = out["job_id"]
    r = client.post(f"/calibrations/{job_id}/spec/confirm",
                    json={"criteria": out["criteria"]})
    assert r.status_code == 200
    return job_id


# ---------------- 初始:确认关卡 interrupt ----------------

def test_require_spec_confirmation_interrupts_before_extract() -> None:
    c = _client()
    out = c.post("/jobs", json={**BODY, "require_spec_confirmation": True}).json()
    assert out["status"] == "AWAITING_SPEC_CONFIRMATION"
    assert out["workflow"]["waiting_for"] == "spec_confirmation"
    assert out["workflow"]["next_nodes"] == ["spec_confirmation"]
    # 未确认绝不 extract/evaluate:没有任何证据与评估结果
    assert out["evaluations"] == []
    assert out["evidence"] == []
    # 已完成的节点来自真实 checkpoint 轨迹
    assert out["workflow"]["completed_nodes"] == ["clarify", "generate_spec"]
    assert out["spec_version"] == 1
    assert out["criteria"]                      # 初始标准已生成待确认


def test_default_job_unchanged_compat() -> None:
    """POST /jobs 不带新字段:旧行为(一次跑完)完全不变。"""
    out = _client().post("/jobs", json=BODY).json()
    assert out["status"] == "EVALUATED"
    assert out["workflow"]["waiting_for"] is None
    assert out["workflow"]["next_nodes"] == []
    assert out["workflow"]["model_mode"] == "offline_fallback"
    assert out["workflow"]["checkpointer"] == "memory"
    assert out["evaluations"]


def test_feedback_before_spec_confirmation_is_409() -> None:
    c = _client()
    job_id = c.post("/jobs", json={**BODY, "require_spec_confirmation": True}).json()["job_id"]
    r = c.post(f"/calibrations/{job_id}/feedback", json={"text": "更看重分布式经验"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SPEC_CONFIRMATION_REQUIRED"
    # 409 之后流程状态未被破坏
    assert c.get(f"/calibrations/{job_id}").json()["status"] == "AWAITING_SPEC_CONFIRMATION"


# ---------------- 确认关卡:编辑 / 风险 / 恢复 ----------------

def test_confirm_with_edited_criteria_then_evaluate() -> None:
    c = _client()
    out = c.post("/jobs", json={**BODY, "require_spec_confirmation": True}).json()
    job_id = out["job_id"]
    edited = [
        {"id": "c1", "type": "MUST_HAVE", "text": "后端项目经验", "weight": 3},
        {"id": "c2", "type": "NICE_TO_HAVE", "text": "分布式系统设计", "weight": 2},
    ]
    r = c.post(f"/calibrations/{job_id}/spec/confirm", json={"criteria": edited}).json()
    assert r["status"] == "EVALUATED"
    assert {c["id"] for c in r["criteria"]} == {"c1", "c2"}      # 按编辑后的 criteria 生效
    assert r["spec_version"] == 1                                 # 初始确认不升版本
    assert r["evaluations"] and r["evidence"]
    assert "spec_confirmation" in r["workflow"]["completed_nodes"]


def test_risky_spec_confirm_422_and_checkpoint_clean() -> None:
    """风险编辑在进入 Command(resume) 前拦截:422 且 checkpoint 停在原处可重试。"""
    c = _client()
    out = c.post("/jobs", json={**BODY, "require_spec_confirmation": True}).json()
    job_id = out["job_id"]
    bad = out["criteria"] + [{"id": "v1", "type": "MUST_HAVE", "text": "35岁以下", "weight": 3}]
    r = c.post(f"/calibrations/{job_id}/spec/confirm", json={"criteria": bad})
    assert r.status_code == 422
    # 未污染 checkpoint:仍在确认关卡,原标准未变,可用合法 criteria 重试
    snap = c.get(f"/calibrations/{job_id}").json()
    assert snap["status"] == "AWAITING_SPEC_CONFIRMATION"
    assert snap["criteria"] == out["criteria"]
    ok = c.post(f"/calibrations/{job_id}/spec/confirm", json={"criteria": out["criteria"]})
    assert ok.status_code == 200
    assert ok.json()["status"] == "EVALUATED"


def test_spec_confirm_validation_unique_ids_and_blank() -> None:
    c = _client()
    out = c.post("/jobs", json={**BODY, "require_spec_confirmation": True}).json()
    job_id = out["job_id"]
    dup = [{"id": "c1", "type": "MUST_HAVE", "text": "A", "weight": 2},
           {"id": "c1", "type": "NICE_TO_HAVE", "text": "B", "weight": 2}]
    assert c.post(f"/calibrations/{job_id}/spec/confirm", json={"criteria": dup}).status_code == 422
    assert c.post(f"/calibrations/{job_id}/spec/confirm",
                  json={"criteria": [{"text": "  ", "type": "MUST_HAVE"}]}).status_code == 422
    # 非法确认未推进状态
    assert c.get(f"/calibrations/{job_id}").json()["status"] == "AWAITING_SPEC_CONFIRMATION"


def test_spec_confirm_not_pending_is_409() -> None:
    c = _client()
    job_id = _confirmed_job(c)
    r = c.post(f"/calibrations/{job_id}/spec/confirm",
               json={"criteria": [{"text": "任意", "type": "MUST_HAVE"}]})
    assert r.status_code == 409


# ---------------- GET 快照恢复 ----------------

def test_snapshot_recovery_from_checkpoint() -> None:
    """确认并重跑后,GET 从 checkpoint 恢复完整状态(与确认响应一致)。"""
    c = _client()
    job_id = _confirmed_job(c)
    snap = c.get(f"/calibrations/{job_id}").json()
    assert snap["status"] == "EVALUATED"
    assert snap["workflow"]["waiting_for"] is None
    assert snap["workflow"]["next_nodes"] == []
    assert {"clarify", "generate_spec", "spec_confirmation",
            "extract_evidence", "validate_evidence", "evaluate"} <= set(
                snap["workflow"]["completed_nodes"])
    assert snap["workflow"]["history"] and all(
        isinstance(h, dict) and h.get("node") for h in snap["workflow"]["history"])
    # 澄清信息透传给前端(离线回退为需求原文)
    assert snap["clarified_requirement"]
    assert isinstance(snap["clarification_questions"], list)


# ---------------- 反馈 → 审批(不先变 rank)----------------

def test_feedback_interrupt_does_not_change_ranking_until_resumed() -> None:
    c = _client()
    job_id = _confirmed_job(c)
    before = c.get(f"/calibrations/{job_id}").json()
    before_order = [(e["name"], e["verdict"]) for e in before["evaluations"]]

    r = c.post(f"/calibrations/{job_id}/feedback",
               json={"text": "更看重开源贡献"}).json()
    assert r["status"] == "AWAITING_APPROVAL"
    assert r["workflow"]["waiting_for"] == "human_approval"
    # 提案未批准前,标准与排名一律不变
    assert r["spec_version"] == before["spec_version"]
    assert r["criteria"] == before["criteria"]
    assert [(e["name"], e["verdict"]) for e in r["evaluations"]] == before_order

    # resume=reject → 排名仍不变
    r2 = c.post(f"/calibrations/{job_id}/approval", json={"approved": False}).json()
    assert r2["status"] == "EVALUATED"
    assert [(e["name"], e["verdict"]) for e in r2["evaluations"]] == before_order


def test_feedback_resume_accept_applies_add() -> None:
    c = _client()
    job_id = _confirmed_job(c)
    r = c.post(f"/calibrations/{job_id}/feedback",
               json={"text": "更看重分布式系统设计"}).json()
    assert r["change_proposal"]["add"]
    r2 = c.post(f"/calibrations/{job_id}/approval", json={"approved": True}).json()
    assert r2["status"] == "EVALUATED"
    assert r2["spec_version"] == 2
    assert len(r2["criteria"]) == len(r["criteria"]) + len(r["change_proposal"]["add"])


def test_feedback_resume_edit_risky_criteria_422_then_valid_edit_ok() -> None:
    """Edit 风险校验在 Command 之前:422 后审批关卡原样保留,可再次合法提交。"""
    c = _client()
    job_id = _confirmed_job(c)
    c.post(f"/calibrations/{job_id}/feedback", json={"text": "更看重项目主导经验"})

    bad = [{"id": "c1", "type": "MUST_HAVE", "text": "男性优先", "weight": 3}]
    r = c.post(f"/calibrations/{job_id}/approval", json={"action": "edit", "criteria": bad})
    assert r.status_code == 422
    snap = c.get(f"/calibrations/{job_id}").json()
    assert snap["status"] == "AWAITING_APPROVAL"          # checkpoint 未被破坏

    good = [{"id": "c1", "type": "MUST_HAVE", "text": "后端项目经验", "weight": 3}]
    r2 = c.post(f"/calibrations/{job_id}/approval", json={"action": "edit", "criteria": good})
    assert r2.status_code == 200
    assert {c["id"] for c in r2.json()["criteria"]} == {"c1"}


# ---------------- remove 语义 / 同 ID 文本变动 / 仅改类型 ----------------

def test_accept_applies_remove_semantics() -> None:
    """提案 remove 的标准在批准后必须真正从标准集中删除。"""
    c = _client()
    job_id = _confirmed_job(c)
    r = c.post(f"/calibrations/{job_id}/feedback",
               json={"text": "去掉沟通协作能力这条要求"}).json()
    assert "c3" in r["change_proposal"]["remove"]
    r2 = c.post(f"/calibrations/{job_id}/approval", json={"approved": True}).json()
    assert "c3" not in {c["id"] for c in r2["criteria"]}
    assert r2["spec_version"] == 2
    assert all("c3" not in e["scores"] for e in r2["evaluations"])


def test_same_id_text_change_reextracts_evidence() -> None:
    """同 ID 只改文本:旧证据作废重抽,不能拿旧措辞的证据套新标准。"""
    c = _client()
    job_id = _confirmed_job(c)
    before = c.get(f"/calibrations/{job_id}").json()
    old_c1 = next(c for c in before["criteria"] if c["id"] == "c1")
    c.post(f"/calibrations/{job_id}/feedback", json={"text": "更看重开源贡献"})
    edited = [dict(c) for c in before["criteria"]]
    for c2 in edited:
        if c2["id"] == "c1":
            c2["text"] = "分布式系统设计"          # 同 ID 改文本
    r = c.post(f"/calibrations/{job_id}/approval",
               json={"action": "edit", "criteria": edited}).json()
    assert next(c for c in r["criteria"] if c["id"] == "c1")["text"] == "分布式系统设计"
    zs = next(e for e in r["evaluations"] if e["name"] == "张三")
    assert zs["scores"]["c1"] == 2                   # 按新措辞重新抽到证据
    ev_c1 = [e for e in r["evidence"] if e["criterion_id"] == "c1"
             and e["candidate_id"] == zs["candidate_id"]]
    assert all("分布式" in e["quote"] for e in ev_c1)   # 旧措辞证据不残留
    assert old_c1["text"] == "相关项目经验"


def test_type_only_change_keeps_evidence_and_reranks() -> None:
    """同 ID 同文本只改类型:不重抽证据(证据原样保留),仅重新分档聚合。"""
    c = _client()
    job_id = _confirmed_job(c)
    before = c.get(f"/calibrations/{job_id}").json()
    before_evidence = sorted((e["candidate_id"], e["criterion_id"], e["quote"])
                             for e in before["evidence"])
    c.post(f"/calibrations/{job_id}/feedback", json={"text": "更看重开源贡献"})
    edited = [dict(c) for c in before["criteria"]]
    for c2 in edited:
        if c2["id"] == "c1":
            c2["type"] = "VETO"                      # 文本不变,只改类型
    r = c.post(f"/calibrations/{job_id}/approval",
               json={"action": "edit", "criteria": edited}).json()
    after_evidence = sorted((e["candidate_id"], e["criterion_id"], e["quote"])
                            for e in r["evidence"])
    assert after_evidence == before_evidence         # 未重抽:证据完全一致
    zs = next(e for e in r["evaluations"] if e["name"] == "张三")
    # 张三满足 c1(相关项目经验)→ c1 改为 VETO 后命中否决 → 重新聚合为否决
    assert zs["verdict"] == "否决"


# ---------------- history:端点行为 + 图内轨迹 ----------------

def test_history_endpoint_still_503_without_pg() -> None:
    c = _client()
    job_id = _confirmed_job(c)
    assert c.get(f"/calibrations/{job_id}/history").status_code == 503


def test_workflow_history_covers_initial_feedback_approval() -> None:
    """workflow.history 来自 checkpoint 轨迹:初始/反馈/审批记录齐全,不虚构。"""
    c = _client()
    job_id = _confirmed_job(c)
    c.post(f"/calibrations/{job_id}/feedback", json={"text": "更看重开源贡献"})
    c.post(f"/calibrations/{job_id}/approval", json={"approved": True})
    snap = c.get(f"/calibrations/{job_id}").json()
    nodes = [h["node"] for h in snap["workflow"]["history"]]
    assert nodes[:2] == ["clarify", "generate_spec"]
    assert "spec_confirmation" in nodes
    assert "interpret_feedback" in nodes and "human_approval" in nodes
    # interrupt 节点只在恢复完成后记录一次
    assert nodes.count("human_approval") == 1
