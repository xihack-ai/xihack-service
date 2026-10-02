"""真实数据库读写 + 明确模拟 AI；保留单独的模拟职位供复查。"""

import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from app.database import DatabaseCheckError, inspect_database
from app.recruiting_service import PersistentLoop
from app.recruiting_store import RecruitingStore
from hirespec_loop.errors import HireSpecError
from hirespec_loop.fixtures import ANSWERS, OWNERSHIP_FEEDBACK, REQUIREMENT, OfflineDemoClient, load_candidates
from hirespec_loop.service import HireSpecAI
import psycopg


async def run_demo(actor="manager-local"):
    store = RecruitingStore()
    loop = PersistentLoop(store, HireSpecAI(OfflineDemoClient()), model_mode="offline_demo")
    job = loop.create_job("[HireSpec 模拟样本] 数据库闭环", REQUIREMENT, actor)
    job_id = job["id"]
    print("已创建独立模拟职位：" + job_id, flush=True)
    clarify = await loop.clarify(job_id, actor, {})
    draft = await loop.draft(job_id, actor, ANSWERS)
    candidates = {}
    for candidate in load_candidates():
        candidates[candidate.candidate_id] = store.add_candidate(job_id, actor, "模拟候选人 " + candidate.candidate_id, candidate.resume_text, "synthetic-demo.txt")
    first = await loop.approve_initial(job_id, actor, draft["draft_id"])
    feedback = await loop.feedback(job_id, actor, candidates["P01"]["id"], "NO", OWNERSHIP_FEEDBACK, 1)
    before_accept = store.get_job(job_id, actor)
    assert before_accept["job"]["current_spec_version"] == 1
    proposal_id = feedback["proposal"]["proposal_id"]
    second = await loop.accept(job_id, actor, proposal_id)
    # New service/client simulates a process restart: no previous in-memory state.
    restarted = PersistentLoop(RecruitingStore(), HireSpecAI(OfflineDemoClient()), model_mode="offline_demo")
    view = restarted.store.get_job(job_id, actor)
    repeated = await restarted.accept(job_id, actor, proposal_id)
    assert repeated["already_decided"] and repeated["receipt"] == second["receipt"]
    blocked = await restarted.feedback(job_id, actor, candidates["P01"]["id"], "NO", "女性不适合这个岗位。", 2)
    restarted.store.label(job_id, actor, candidates["P04"]["id"], True, "calibration")
    name_by_id = {row["id"]: name for name, row in candidates.items()}
    order = lambda snapshot: [name_by_id[e["candidate_id"]] for e in snapshot["entries"]]
    with store.connect(read_only=True) as conn:
        counts = {
            table: conn.execute(f"SELECT count(*) AS n FROM {table} WHERE job_id=%s", (job_id,)).fetchone()["n"]
            for table in ("spec_versions", "candidates", "evaluations", "feedback", "change_proposals", "candidate_labels", "audit_events")
        }
    return {
        "contract": "hirespec-pg-v1", "data_kind": "SIMULATED_AI_REAL_DATABASE",
        "notice": "实际写入独立模拟职位；模型回复和简历是预置模拟样本。未验证真实模型效果。",
        "job_id": job_id, "actor": actor, "tables_written": ["jobs", *counts], "row_counts": {"jobs": 1, **counts},
        "v1_order": order(first["snapshot"]), "v2_order": order(second["snapshot"]),
        "v1_evaluations": first["snapshot"]["model_evaluations"],
        "v2_evaluations": second["snapshot"]["model_evaluations"],
        "v2_cache_hits": second["snapshot"]["cache_hits"],
        "spec_unchanged_before_accept": True, "restart_readback_verified": view["job"]["current_spec_version"] == 2,
        "duplicate_accept_verified": repeated["already_decided"],
        "sensitive_feedback_status": blocked["proposal"]["status"],
        "sensitive_feedback_can_approve": blocked["can_approve"],
        "approved_proposal_id": proposal_id,
        "clarification_question_count": len(clarify.questions),
        "credentials_saved": False, "server_schema_changed": False,
    }


def main():
    parser = argparse.ArgumentParser(description="运行真实数据库持久化演示（创建并保留独立模拟职位）")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    injected = False
    try:
        if not os.getenv("PGPASSWORD"):
            if not sys.stdin.isatty():
                print("请在交互式 PowerShell 中运行，以隐藏方式输入数据库密码。")
                return 1
            os.environ["PGPASSWORD"] = getpass.getpass("PostgreSQL 数据库密码（输入不显示）：")
            injected = True
        inspect_database()
        report = asyncio.run(run_demo(os.getenv("HIRESPEC_LOCAL_ACTOR", "manager-local")))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    except (HireSpecError, DatabaseCheckError) as exc:
        print(f"{exc.code}：{exc}")
        return 1
    except psycopg.Error:
        print("数据库任务失败。已经创建的模拟职位可按上方 ID 复查；未提交的事务已回滚。")
        return 1
    finally:
        if injected:
            os.environ.pop("PGPASSWORD", None)


if __name__ == "__main__":
    raise SystemExit(main())
