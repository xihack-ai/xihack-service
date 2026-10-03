"""校准工作流各节点。

约束(与产品原则一致):
- LLM 只负责"理解与措辞",全部走 app.llm.chat_json 的回退路径,离线/断网可演示;
- 证据校验、分档、偏差检查、变更应用全部由确定性代码完成,编造的引用一律丢弃;
- 分档由规则决定(硬性要求是否满足),不用加权总分;weight 只用于档内排序与展示;
- 人工审批节点用 interrupt() 暂停,由 API 用 Command(resume=...) 恢复。
"""

import json
import re
from typing import Any

from app import llm, store
from app.workflow.state import Criterion, Evidence, Evaluation, Tradeoff
from hirespec_loop.errors import InputError, PolicyError

# 偏差/风险红线:命中则拦截提案,不进入人工审批
FORBIDDEN_TERMS = [
    "性别", "男", "女", "年龄", "岁", "婚", "育", "籍贯", "户籍", "民族", "宗教",
    "年轻", "985", "211", "第一学历", "学校层次",
]

_FALLBACK_CRITERIA: list[Criterion] = [
    {"id": "c1", "type": "MUST_HAVE", "text": "相关项目经验", "weight": 3},
    {"id": "c2", "type": "MUST_HAVE", "text": "技术栈匹配", "weight": 2},
    {"id": "c3", "type": "NICE_TO_HAVE", "text": "沟通协作能力", "weight": 1},
]

_TYPES = ("MUST_HAVE", "NICE_TO_HAVE", "VETO")


def _tokens(text: str) -> list[str]:
    """从标准文本提取匹配用 token:英文词整体,中文按二元组滑窗。"""
    toks: list[str] = []
    for m in re.findall(r"[A-Za-z][A-Za-z0-9+#.]*|[\u4e00-\u9fff]+", text):
        if re.match(r"[A-Za-z]", m):
            toks.append(m)
        elif len(m) == 1:
            toks.append(m)
        else:
            toks.extend(m[i:i + 2] for i in range(len(m) - 1))
    return toks


def _norm_type(raw: Any) -> str:
    t = str(raw or "").upper()
    return t if t in _TYPES else "NICE_TO_HAVE"


def normalize_criteria(raw: Any) -> list[Criterion]:
    """人工编辑的标准集校验:文本非空、类型合法、weight 合法、ID 唯一。

    供 API 在 Command(resume=...) **之前**调用,避免非法编辑进入 interrupt
    恢复路径后抛错污染 checkpoint。
    """
    if not isinstance(raw, list) or not raw:
        raise InputError("criteria 必须是非空列表")
    out: list[Criterion] = []
    seen: set[str] = set()
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            raise InputError(f"第 {i+1} 条标准格式非法")
        text = str(c.get("text") or "").strip()
        if not text:
            raise InputError(f"第 {i+1} 条标准文本不得为空")
        raw_type = str(c.get("type") or "NICE_TO_HAVE").strip().upper()
        if raw_type not in _TYPES:
            raise InputError(f"标准 {text[:20]} 的类型非法: {raw_type}")
        try:
            weight = int(c.get("weight", 2))
        except (TypeError, ValueError):
            raise InputError(f"标准 {text[:20]} 的 weight 必须是 1-3 的整数") from None
        if not 1 <= weight <= 3:
            raise InputError(f"标准 {text[:20]} 的 weight 必须是 1-3 的整数")
        cid = str(c.get("id") or f"c{i+1}").strip()
        if not cid:
            raise InputError(f"第 {i+1} 条标准 ID 不得为空")
        if cid in seen:
            raise InputError(f"标准 ID 重复: {cid}")
        seen.add(cid)
        out.append(Criterion(id=cid, type=raw_type, text=text, weight=weight))  # type: ignore[arg-type]
    return out


def criteria_risk_hits(criteria: list[Criterion]) -> list[str]:
    """人工编辑(确认/审批 Edit)同样要过偏见红线。"""
    return sorted({t for c in criteria for t in FORBIDDEN_TERMS if t in c["text"]})


def check_criteria_risk(criteria: list[Criterion]) -> None:
    hits = criteria_risk_hits(criteria)
    if hits:
        raise PolicyError(f"标准包含受限表述: {'、'.join(hits)}（年龄/性别/学校层级等不可作为招聘标准）")


def _extract_for_candidate(cand: dict, criteria: list[Criterion]) -> list[Evidence]:
    """证据抽取:优先 LLM,离线回退为"标准 token 命中简历行则整行引用"。"""
    lines = [ln.strip() for ln in cand["resume_text"].splitlines() if ln.strip()]
    out: list[Evidence] = []

    if llm.is_configured() and not llm.replay_only():
        msg = [
            {"role": "system", "content": (
                "你是招聘证据抽取器。只允许逐字引用简历原文,禁止改写或编造。"
                "简历内容明确否定该条时 stance=against,否则 support;找不到对应内容就不输出该条。"
                '输出 JSON: {"evidence": [{"criterion_id": "...", "quote": "简历原文整行", '
                '"stance": "support|against"}]}'
            )},
            {"role": "user", "content": json.dumps(
                {"criteria": criteria, "resume": cand["resume_text"]}, ensure_ascii=False)},
        ]
        data = llm.chat_json(msg, fallback={})
        for e in data.get("evidence", []):
            if isinstance(e, dict) and e.get("quote") and e.get("criterion_id"):
                out.append(Evidence(
                    candidate_id=cand["id"], criterion_id=str(e["criterion_id"]),
                    quote=str(e["quote"]).strip(),
                    stance="against" if str(e.get("stance")) == "against" else "support",
                    valid=False))

    if not out:  # 离线/失败回退:确定性抽取
        for c in criteria:
            toks = _tokens(c["text"])
            for ln in lines:
                if any(t.lower() in ln.lower() for t in toks):
                    out.append(Evidence(
                        candidate_id=cand["id"], criterion_id=c["id"], quote=ln,
                        stance="support", valid=False))
                    break
    return out


def _validate_evidence(candidates: list[dict], criteria: list[Criterion],
                       evidence: list[Evidence]) -> list[Evidence]:
    """代码校验:引用必须是简历原文(忽略空白差异),否则判无效并丢弃。"""
    resumes = {c["id"]: re.sub(r"\s+", "", c["resume_text"]) for c in candidates}
    criterion_ids = {c["id"] for c in criteria}
    ok: list[Evidence] = []
    for e in evidence:
        norm = re.sub(r"\s+", "", e["quote"])
        valid = e["candidate_id"] in resumes and e["criterion_id"] in criterion_ids \
            and bool(norm) and norm in resumes[e["candidate_id"]]
        if valid:
            ok.append({**e, "valid": True})
    return ok


def _criterion_status(cid: str, evidence: list[Evidence]) -> int | None:
    """单条标准判定:2=有支持证据且无反证,0=有反证,None=无证据(不臆断)。"""
    ev = [e for e in evidence if e["criterion_id"] == cid and e["valid"]]
    if any(e["stance"] == "against" for e in ev):
        return 0
    return 2 if ev else None


def _tradeoff(cid_cand: str, criteria: list[Criterion], evidence: list[Evidence],
              scores: dict[str, int | None]) -> Tradeoff:
    """Trade-off Map:该候选人的优势(带原文)与取舍(缺证据/反证)。"""
    strengths, gaps, veto_hit = [], [], []
    for c in criteria:
        s = scores[c["id"]]
        if s == 2:
            if c["type"] == "VETO":
                continue
            quote = next((e["quote"] for e in evidence
                          if e["candidate_id"] == cid_cand and e["criterion_id"] == c["id"]
                          and e["valid"] and e["stance"] == "support"), "")
            strengths.append({"criterion_id": c["id"], "text": c["text"],
                              "quote": quote, "type": c["type"]})
        elif s == 0:
            gaps.append({"criterion_id": c["id"], "text": c["text"],
                         "reason": "简历内容与该条冲突", "type": c["type"]})
        else:
            gaps.append({"criterion_id": c["id"], "text": c["text"],
                         "reason": "简历中未见证据", "type": c["type"]})
    if any(scores[c["id"]] == 2 for c in criteria if c["type"] == "VETO"):
        veto_hit = [c["id"] for c in criteria if c["type"] == "VETO" and scores[c["id"]] == 2]
    return Tradeoff(strengths=strengths, gaps=gaps, veto_hit=veto_hit)


def _score(candidate_id: str, criteria: list[Criterion],
           evidence: list[Evidence]) -> Evaluation:
    """确定性分档:按硬性要求的满足情况决定档位,不加权求和。

    否决项命中 → 否决;任一硬性要求有反证 → 不推荐;
    任一硬性要求缺证据 → 待核实;全部硬性要求有证据 → 推荐。
    weight 只用于档内排序(见 rank_key),不参与分档。
    """
    scores = {c["id"]: _criterion_status(c["id"],
                                        [e for e in evidence
                                         if e["candidate_id"] == candidate_id])
              for c in criteria}
    must = [c for c in criteria if c["type"] == "MUST_HAVE"]

    if any(scores[c["id"]] == 2 for c in criteria if c["type"] == "VETO"):
        verdict = "否决"
    elif any(scores[c["id"]] == 0 for c in must):
        verdict = "不推荐"
    elif any(scores[c["id"]] is None for c in must):
        verdict = "待核实"
    else:
        verdict = "推荐"

    return Evaluation(
        candidate_id=candidate_id, scores=scores,
        evidence_ids=[f"{candidate_id}:{c}" for c, s in scores.items() if s == 2],
        verdict=verdict,
        tradeoff=_tradeoff(candidate_id, criteria, evidence, scores))


# ---------------- graph nodes ----------------

def clarify(state: dict) -> dict:
    """需求澄清:LLM 提炼岗位要求要点;离线回退为原文。"""
    msg = [
        {"role": "system", "content": '提炼岗位要求,输出 JSON {"clarified": "...", "questions": ["..."]}'},
        {"role": "user", "content": state.get("requirement", "")},
    ]
    data = llm.chat_json(msg, fallback={"clarified": state.get("requirement", ""), "questions": []})
    questions = [q for q in data.get("questions", []) if isinstance(q, str)]
    return {
        "clarified_requirement": str(data.get("clarified") or state.get("requirement", "")),
        "clarification_questions": questions,
        "feedback_notes": list(state.get("feedback_notes", [])) + [f"澄清提问: {q}" for q in questions],
    }


def generate_spec(state: dict) -> dict:
    """生成招聘标准 v1;离线回退为三条通用标准。"""
    msg = [
        {"role": "system", "content": (
            "根据岗位要求生成 3-6 条可核验的招聘标准。"
            '输出 JSON: {"criteria": [{"id": "c1", "type": "MUST_HAVE|NICE_TO_HAVE|VETO", '
            '"text": "...", "weight": 1-3}]}'
        )},
        {"role": "user", "content": state.get("clarified_requirement", state.get("requirement", ""))},
    ]
    fallback = {"criteria": _FALLBACK_CRITERIA}
    data = llm.chat_json(msg, fallback=fallback)
    raw = [c for c in data.get("criteria", []) if isinstance(c, dict) and c.get("text")]
    criteria: list[Criterion] = [
        {"id": str(c.get("id") or f"c{i+1}"), "type": _norm_type(c.get("type")),
         "text": str(c["text"]), "weight": int(c.get("weight", 2))}
        for i, c in enumerate(raw)
    ] or [dict(c) for c in _FALLBACK_CRITERIA]

    version = int(state.get("spec_version") or 1)
    store.save_spec(state.get("job_id"), version, criteria)
    return {"criteria": criteria, "spec_version": version}


def spec_confirmation(state: dict) -> dict:
    """首次标准确认关卡:generate_spec 之后 interrupt,未确认绝不进入抽证/评估。

    恢复时的校验(text/type/unique id/风险)全部由 API 在 Command(resume=...)
    之前完成;这里只做防御性归一化,不抛业务错误,避免污染 checkpoint。
    """
    from langgraph.types import interrupt

    job_id = state.get("job_id")
    version = int(state.get("spec_version") or 1)
    criteria = [dict(c) for c in state.get("criteria", [])]
    decision = interrupt({
        "job_id": job_id, "spec_version": version, "criteria": criteria,
        "clarified_requirement": state.get("clarified_requirement", ""),
        "clarification_questions": state.get("clarification_questions", []),
        "question": "请确认或编辑初始招聘标准;确认前不会抽取证据或评估候选人。",
    })
    if not isinstance(decision, dict):
        decision = {"confirmed": bool(decision)}
    edited = decision.get("criteria")
    final = criteria
    is_edit = False
    if edited:
        try:
            final = normalize_criteria(edited)
            is_edit = True
        except InputError:
            pass  # API 层已拦截;异常情况保留原标准,不在 resume 路径报错
    store.save_spec(job_id, version, final)
    return {
        "criteria": final,
        "spec_confirmation": {
            "confirmed": True, "edited": is_edit,
            "note": str(decision.get("note") or ""),
            "decided_by": str(decision.get("decided_by") or "hr"),
        },
        "feedback_notes": list(state.get("feedback_notes", [])) + [
            f"初始标准已确认({'人工改写' if is_edit else '采纳'},v{version},{len(final)} 条)"],
    }


def extract_evidence(state: dict) -> dict:
    cands = state.get("candidates", [])
    crit_scope = state.get("reeval_criteria")       # None=全量;[]=不抽新证据;[...]=只抽这些
    if crit_scope == []:
        return {}
    scope_c = set(state.get("reeval_candidates") or [c["id"] for c in cands])
    crits = [c for c in state.get("criteria", []) if crit_scope is None or c["id"] in crit_scope]

    fresh: list[Evidence] = []
    for cand in cands:
        if cand["id"] in scope_c:
            fresh.extend(_extract_for_candidate(cand, crits))
    touched = {(e["candidate_id"], e["criterion_id"]) for e in fresh}
    kept = [e for e in state.get("evidence", [])
            if (e["candidate_id"], e["criterion_id"]) not in touched
            and (crit_scope is None or e["criterion_id"] not in crit_scope
                 or e["candidate_id"] not in scope_c)]
    return {"evidence": kept + fresh}


def validate_evidence(state: dict) -> dict:
    return {"evidence": _validate_evidence(
        state.get("candidates", []), state.get("criteria", []), state.get("evidence", []))}


def evaluate(state: dict) -> dict:
    cands = state.get("candidates", [])
    scope = set(state.get("reeval_candidates") or [c["id"] for c in cands])
    kept = [ev for ev in state.get("evaluations", []) if ev["candidate_id"] not in scope]
    fresh = [_score(c["id"], state.get("criteria", []), state.get("evidence", []))
             for c in cands if c["id"] in scope]
    result = kept + fresh
    store.save_evaluations(state.get("job_id"), state.get("spec_version"), result)
    return {"evaluations": result, "reeval_candidates": [], "reeval_criteria": None}


def interpret_feedback(state: dict) -> dict:
    """解读 HR 反馈 → 标准变更提案草案。"""
    text = state.get("feedback", "").strip()
    msg = [
        {"role": "system", "content": (
            "把招聘团队反馈翻译成对招聘标准的结构化变更提案。"
            '输出 JSON: {"reason": "...", "add": [{"text": "...", "type": "...", "weight": 1-3}], '
            '"remove": ["criterion_id"]};无需变更则 add/remove 为空。'
        )},
        {"role": "user", "content": json.dumps(
            {"criteria": state.get("criteria", []), "feedback": text}, ensure_ascii=False)},
    ]
    data = llm.chat_json(msg, fallback={}) if (llm.is_configured() and not llm.replay_only()) else {}
    if not data:  # 确定性回退
        add, remove = [], []
        if any(k in text for k in ("更看重", "增加", "要求", "必须")):
            add = [{"text": text[:40], "type": "NICE_TO_HAVE", "weight": 2}]
        for c in state.get("criteria", []):
            if any(k in text for k in ("取消", "删除", "去掉")) and _tokens(c["text"]) and \
                    any(t in text for t in _tokens(c["text"])):
                remove.append(c["id"])
        data = {"reason": text, "add": add, "remove": remove}
    proposal = {
        "reason": str(data.get("reason") or text),
        "add": [{"text": str(c["text"]), "type": _norm_type(c.get("type")),
                 "weight": int(c.get("weight", 2))}
                for c in data.get("add", []) if isinstance(c, dict) and c.get("text")],
        "remove": [str(x) for x in data.get("remove", [])],
    }
    fid = store.save_feedback(state.get("job_id"), state.get("spec_version"), text,
                              state.get("feedback_created_by"))
    return {"feedback_raw": text, "feedback_id": fid, "change_proposal": proposal, "feedback": "",
            "feedback_created_by": None,
            "feedback_notes": list(state.get("feedback_notes", [])) + [f"反馈: {text}"]}


def bias_risk_check(state: dict) -> dict:
    """确定性红线检查:命中偏见词一律拦截。

    注意要同时查**反馈原文**:LLM 可能把“年傆/学校层级”措辞洗成中性描述再提案,
    只查提案措辞会漏放;即使本轮没产生提案也要记下 BLOCKED 以便合规回看。
    """
    proposal = state.get("change_proposal") or {}
    corpus = [state.get("feedback_raw", ""), str(proposal.get("reason", ""))]
    corpus += [a.get("text", "") for a in proposal.get("add", [])]
    hits = sorted({t for text in corpus for t in FORBIDDEN_TERMS if t in text})
    if not hits:
        return {"risk_flags": []}
    store.save_proposal(state, status="BLOCKED", risk_flags=hits)
    return {"risk_flags": hits, "change_proposal": None,
            "feedback_notes": list(state.get("feedback_notes", [])) + [
                f"提案因涉及受限表述被自动拦截: {hits}"
                "（年傆/性别/学校层级等不可作为招聘标准）"]}


def route_after_bias(state: dict) -> str:
    return "end" if state.get("risk_flags") else "propose"


def propose_change(state: dict) -> dict:
    p = state.get("change_proposal") or {}
    if not p.get("add") and not p.get("remove"):
        store.save_proposal(state, status="EMPTY", risk_flags=[])
        return {"change_proposal": None}
    # 必须在 human_approval 之前落库:interrupt 恢复时该节点会从头重跑,
    # 写在它内部的插入语句会被重复执行。
    store.save_proposal(state, status="PENDING_APPROVAL", risk_flags=[])
    return {}


def route_after_propose(state: dict) -> str:
    return "approval" if state.get("change_proposal") else "end"


def route_after_spec(state: dict) -> str:
    """require_spec_confirmation=true 时先停人工确认,否则直进抽证(旧行为兼容)。"""
    return "confirm" if state.get("require_spec_confirmation") and not state.get("spec_confirmation") \
        else "extract"


def human_approval(state: dict) -> dict:
    """人工审批:Accept / Edit / Reject。interrupt 暂停,API 用 Command(resume=...) 恢复。"""
    from langgraph.types import interrupt

    job_id = state.get("job_id")
    version = int(state.get("spec_version", 1))
    proposal = state.get("change_proposal") or {}
    decision = interrupt({
        "job_id": job_id, "spec_version": version, "proposal": proposal,
        "criteria": state.get("criteria", []),
        "question": "是否批准该招聘标准变更?",
    })
    if not isinstance(decision, dict):
        decision = {"approved": bool(decision)}
    action = str(decision.get("action") or ("accept" if decision.get("approved") else "reject"))

    if action == "reject":
        store.decide_proposal(job_id, decision, status="REJECTED")
        return {"approval": decision, "change_proposal": None, "reeval_candidates": [],
                "reeval_criteria": None,
                "feedback_notes": list(state.get("feedback_notes", [])) + ["变更被驳回"]}

    edited = decision.get("criteria")
    is_edit = action == "edit" and bool(edited)
    if is_edit:  # 审批人直接改写标准全文,不走 add/remove
        new_criteria: list[Criterion] = [
            {"id": str(c.get("id") or f"c{i+1}"), "type": _norm_type(c.get("type")),
             "text": str(c["text"]), "weight": int(c.get("weight", 2))}
            for i, c in enumerate(edited) if isinstance(c, dict) and c.get("text")]
        if not new_criteria:
            is_edit = False

    if not is_edit:
        remove_ids = {str(x) for x in proposal.get("remove", [])}
        old_ids = {c["id"] for c in state.get("criteria", [])}
        existing = [dict(c) for c in state.get("criteria", []) if c["id"] not in remove_ids]
        # 新标准 ID 不得复用刚删除的 ID:否则旧证据会被同 ID 新标准误认领。
        taken = old_ids | {c["id"] for c in existing}
        nxt = max([int(i[1:]) for i in taken if i[1:].isdigit()] or [0])
        for add in proposal.get("add", []):
            nxt += 1
            new_id = f"c{nxt}"
            while new_id in taken:
                nxt += 1
                new_id = f"c{nxt}"
            existing.append({"id": new_id, "type": _norm_type(add.get("type")),
                             "text": str(add["text"]), "weight": int(add.get("weight", 2))})
            taken.add(new_id)
        new_criteria = existing

    old_by_id = {c["id"]: c for c in state.get("criteria", [])}
    new_by_id = {c["id"]: c for c in new_criteria}
    removed = set(old_by_id) - set(new_by_id)
    added = set(new_by_id) - set(old_by_id)
    # 同 ID 文本变动 → 旧证据不再对应新措辞,必须重抽;仅改类型/权重 → 只重新分档聚合。
    reworded = {cid for cid in set(new_by_id) & set(old_by_id)
                if old_by_id[cid]["text"] != new_by_id[cid]["text"]}
    extract_ids = added | reworded

    # 局部重评:新增/改写的标准全员重抽;删除标准只影响持有其证据的候选人。
    if extract_ids:
        affected = {c["id"] for c in state.get("candidates", [])}
        changed = sorted(extract_ids)
    else:
        changed = []
        affected = {e["candidate_id"] for e in state.get("evidence", [])
                    if e["criterion_id"] in removed}
        if not affected:  # 仅改类型/权重:全员重排,不重新抽证
            affected = {c["id"] for c in state.get("candidates", [])}

    nxt_version = version + 1
    store.decide_proposal(job_id, decision, status="APPROVED")
    store.save_spec(job_id, nxt_version, new_criteria, source_proposal=True)
    store.audit(job_id, "system", "SPEC_UPGRADED",
                {"from": version, "to": nxt_version, "added": sorted(added),
                 "reworded": sorted(reworded), "removed": sorted(removed),
                 "edited": is_edit})

    return {
        "criteria": new_criteria, "spec_version": nxt_version,
        "approval": decision, "change_proposal": None,
        "reeval_candidates": sorted(affected), "reeval_criteria": changed,
        "feedback_notes": list(state.get("feedback_notes", [])) + [
            f"变更已批准({'改写' if is_edit else '采纳'},标准升级到 v{nxt_version}"],
    }


def route_after_approval(state: dict) -> str:
    return "reeval" if state.get("reeval_candidates") else "end"


def entry_route(state: dict) -> str:
    """图入口:带 feedback 且已有标准 → 走反馈分支;否则从澄清开始。"""
    if state.get("feedback") and state.get("criteria"):
        return "interpret"
    return "clarify"
