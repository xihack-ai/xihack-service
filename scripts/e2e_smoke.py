"""端到端冒烟:对着真实 PG 跑一遍 建职位→评估→反馈→Edit 审批→局部重评,并回读落库结果。

用法(需 DATABASE_URL 或 PGHOST/PGUSER/... 指向目标库):
    python scripts/e2e_smoke.py

跑完删除自己造的职位(cascade 清干净),不污染数据。
"""

import uuid

from fastapi.testclient import TestClient

from app import db
from app.main import app

MARK = f"[e2e_smoke] {uuid.uuid4().hex[:8]}"
TABLES = ("jobs", "candidates", "spec_versions", "evaluations", "feedback",
          "change_proposals", "audit_events")

BODY = {
    "title": MARK,
    "requirement": "后端工程师,要求高并发经验",
    "candidates": [
        {"name": "甲", "resume_text": "5 年后端开发\n主导过订单项目\n技术栈匹配团队要求"},
        {"name": "乙", "resume_text": "2 年前端开发\n负责 React 页面"},
    ],
}

JOB_ID: list[str] = []


def counts(cur) -> dict:
    out = {}
    for t in TABLES:
        cur.execute(f"select count(*) as n from {t} where job_id = %s", (JOB_ID[0],))
        out[t] = cur.fetchone()["n"]
    return out


def main() -> int:
    client = TestClient(app)
    storage = client.get("/health").json()["storage"]
    print("storage:", storage)
    if storage != "postgres":
        print("本脚本必须对着 PG 跑(未检测到 DATABASE_URL/PGHOST)")
        return 2

    # 1. 建职位 → jobs/candidates/spec_versions/evaluations/audit
    out = client.post("/jobs", json=BODY).json()
    JOB_ID.append(out["job_id"])
    print("1) create:", out["status"], "v" + str(out["spec_version"]),
          {e["name"]: e["verdict"] for e in out["evaluations"]})

    # 2. 反馈 → feedback + PENDING 提案
    r = client.post(f"/calibrations/{JOB_ID[0]}/feedback",
                    json={"text": "更看重分布式经验", "created_by": "e2e"}).json()
    print("2) feedback:", r["status"], "proposal=", bool(r["change_proposal"]))

    # 3. Edit 审批 → 新版本 + 局部重评
    edited = [
        {"id": "c1", "type": "MUST_HAVE", "text": "相关项目经验", "weight": 3},
        {"id": "v1", "type": "VETO", "text": "仅前端经验", "weight": 3},
    ]
    r2 = client.post(f"/calibrations/{JOB_ID[0]}/approval",
                     json={"action": "edit", "criteria": edited, "note": "e2e 改写"}).json()
    print("3) edit-approve: v" + str(r2["spec_version"]),
          {e["name"]: e["verdict"] for e in r2["evaluations"]})

    # 4. 红线拦截
    r3 = client.post(f"/calibrations/{JOB_ID[0]}/feedback",
                     json={"text": "要 985 毕业、30 岁以下的"}).json()
    print("4) redline:", r3["risk_flags"], "proposal=", r3["change_proposal"])

    with db.connect() as conn, conn.cursor() as cur:
        print("5) 落库行数:", counts(cur))
        cur.execute("select version, source_proposal_id is not null as from_proposal "
                    "from spec_versions where job_id=%s order by version", (JOB_ID[0],))
        print("   spec_versions:", cur.fetchall())
        cur.execute("select status, count(*) as n from change_proposals where job_id=%s "
                    "group by 1", (JOB_ID[0],))
        print("   提案状态:", cur.fetchall())
        cur.execute("select verdict, scores::text, tradeoff::text from evaluations "
                    "where job_id=%s and spec_version=2 order by candidate_id", (JOB_ID[0],))
        for row in cur.fetchall():
            print("   eval:", row["verdict"], row["scores"][:58], "| tradeoff:",
                  row["tradeoff"][:66])
        cur.execute("select event_type from audit_events where job_id=%s order by id",
                    (JOB_ID[0],))
        print("   审计:", [x["event_type"] for x in cur.fetchall()])

        h = client.get(f"/calibrations/{JOB_ID[0]}/history").json()
        print("6) history:", {k: len(v) for k, v in h.items() if isinstance(v, list)})

        cur.execute("delete from jobs where id = %s", (JOB_ID[0],))
        print("7) 清理后剩余:", counts(cur))

    print("E2E_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
