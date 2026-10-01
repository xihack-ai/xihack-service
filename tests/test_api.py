"""API / 状态机测试:闸门、非法动作、编造 id 降级、完整谜境流程。

场景来自 data/sample_kb.json:myst_s01(谜境)/ thea_s01(剧场 501)/ sand_s01(沙盒 501)。
"""

from fastapi.testclient import TestClient

from app import llm
from app.main import app

client = TestClient(app)

MYSTERY = "myst_s01"


def _new_session(scene_id: str = MYSTERY) -> str:
    resp = client.post("/sessions", json={"scene_id": scene_id})
    assert resp.status_code == 200
    return resp.json()["session_id"]


def _act(sid: str, **body):
    return client.post(f"/sessions/{sid}/actions", json=body)


def test_scenes_listed():
    scenes = client.get("/scenes").json()
    ids = {s["id"] for s in scenes}
    assert {"myst_s01", "thea_s01", "sand_s01"} <= ids


def test_theater_and_sandbox_not_implemented():
    assert client.post("/sessions", json={"scene_id": "thea_s01"}).status_code == 501
    assert client.post("/sessions", json={"scene_id": "sand_s01"}).status_code == 501


def test_unknown_scene_404():
    assert client.post("/sessions", json={"scene_id": "nope"}).status_code == 404


def test_unknown_session_404():
    assert _act("ghostsid", action="plan").status_code == 404


def test_unknown_action_rejected():
    sid = _new_session()
    assert _act(sid, action="teleport").status_code == 422  # schema 层拒绝


def test_requires_gate_blocks_locked_clue():
    sid = _new_session()
    # myst_cl_theory 需要 myst_cl_material 先解锁,开场直接调查应被拒
    resp = _act(sid, action="investigate", clue_id="myst_cl_theory")
    assert resp.status_code == 400
    assert "myst_cl_material" in resp.json()["detail"]
    body = client.get(f"/sessions/{sid}").json()
    node = {c["id"]: c for c in body["clues"]}
    assert node["myst_cl_env"]["unlocked"] is False
    assert node["myst_cl_theory"]["unlocked"] is False


def test_close_without_evidence_rejected():
    sid = _new_session()
    assert _act(sid, action="close").status_code == 400


def test_hypothesize_requires_pool():
    sid = _new_session()
    assert _act(sid, action="hypothesize", text="它是礼器").status_code == 400


def test_investigate_unlocks_with_fact_tier():
    sid = _new_session()
    out = _act(sid, action="investigate", clue_id="myst_cl_env").json()
    node = {c["id"]: c for c in out["clues"]}
    assert node["myst_cl_env"]["unlocked"] is True
    tiers = [st["tier"] for st in out["result"]["statements"]]
    assert tiers == ["FACT", "FACT"]  # myst_c01 / myst_c03
    assert out["pool"][0]["tier"] == "FACT"


def test_fabricated_ids_from_llm_are_downgraded(monkeypatch):
    # 模拟模型不听话:引用编造 id
    monkeypatch.setattr(
        llm,
        "chat_json",
        lambda messages, fallback, **kw: {
            "statements": [{"text": "它出土于第三层", "claim_ids": ["myst_c01", "c_fabricated"]}]
        },
    )
    sid = _new_session()
    out = _act(sid, action="investigate", clue_id="myst_cl_env").json()
    st = out["result"]["statements"][0]
    assert st["claim_ids"] == ["myst_c01"]
    assert st["invalid_claim_ids"] == ["c_fabricated"]
    assert st["tier"] == "THEORY" and st["downgraded"] is True


def test_theory_clue_labeled_theory():
    sid = _new_session()
    _act(sid, action="investigate", clue_id="myst_cl_env")
    _act(sid, action="investigate", clue_id="myst_cl_material")
    _act(sid, action="investigate", clue_id="myst_cl_theory")
    out = _act(sid, action="opinions").json()
    tiers = {st["tier"] for st in out["result"]["statements"]}
    assert tiers == {"THEORY"}
    assert any(out["result"]["disputes"])


def test_plan_fallback_picks_lowest_order(monkeypatch):
    # 模型给出非法 clue id 时,回退到 order 最小的可用线索
    monkeypatch.setattr(llm, "chat_json", lambda messages, fallback, **kw: {"clue_id": "cl_ghost"})
    sid = _new_session()
    out = _act(sid, action="plan").json()
    assert out["result"]["clue_id"] == "myst_cl_env"


def test_full_mystery_run_offline():
    sid = _new_session()
    plan = _act(sid, action="plan").json()
    assert plan["result"]["clue_id"] == "myst_cl_env"

    for clue_id in ("myst_cl_env", "myst_cl_material", "myst_cl_residue", "myst_cl_theory"):
        resp = _act(sid, action="investigate", clue_id=clue_id)
        assert resp.status_code == 200, clue_id

    opinions = _act(sid, action="opinions").json()
    assert any(st["tier"] == "THEORY" for st in opinions["pool"])

    hypo = _act(sid, action="hypothesize", text="它是礼仪用器").json()
    assert hypo["state"] == "reasoning"
    assert hypo["result"]["verdict"] in ("supported", "refuted", "undetermined")

    closed = _act(sid, action="close").json()
    assert closed["state"] == "closed"
    assert closed["report"] and "结案报告" in closed["report"]
    # 结案后继续调查被拒
    assert _act(sid, action="investigate", clue_id="myst_cl_similar").status_code == 400


def test_kb_search_scoped_to_scene():
    hits = client.get("/api/kb/search", params={"q": "出土", "scene_id": "myst_s01"}).json()
    assert hits and hits[0]["id"].startswith("myst_")
    assert all(h["id"].startswith("myst_") for h in hits)
