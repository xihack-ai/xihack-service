"""谜境(mystery)会话引擎(状态机)。

state: investigating --hypothesize--> reasoning --close--> closed

actions:
    plan         从前置条件已满足的线索里让 LLM 挑下一条;代码校验 id,失败回退 order 最小
    investigate  解锁线索 -> 基于 claims 生成 statements -> 校验/降级 -> 证据池
    opinions     对已解锁的 THEORY claims 输出多方观点(含 dispute_note)
    hypothesize  假设与证据池对照评判
    close        生成结案报告

剧场(theater)/探索(explore)模式先留接口,创建会话时返回 UnsupportedMode。
会话保存在内存(演示用)。
"""

import uuid

from app import llm, validate
from app.kb import KnowledgeBase, get_kb
from app.schemas import Clue, ClueNode, PoolItem, SessionOut

_SESSIONS: dict[str, dict] = {}

JSON_INSTRUCTION = (
    "只输出一个 JSON 对象,不要任何解释或 markdown 围栏。"
    "引用只能使用资料中列出的 claim id,禁止编造。"
)


class SessionNotFound(Exception):
    pass


class ActionError(Exception):
    """非法动作 / 闸门未通过,HTTP 400。"""


class UnsupportedMode(Exception):
    """剧场/探索模式尚未实现,HTTP 501。"""


# ---------------- 会话生命周期 ----------------

def create_session(scene_id: str, kb: KnowledgeBase | None = None) -> str:
    kb = kb or get_kb()
    scene = kb.get_scene(scene_id)
    if scene is None:
        raise SessionNotFound(scene_id)
    if scene.get("mode") != "mystery":
        raise UnsupportedMode(scene.get("mode", "?"))
    session_id = uuid.uuid4().hex[:12]
    _SESSIONS[session_id] = {
        "scene_id": scene_id,
        "state": "investigating",
        "unlocked": [],
        "current_clue_id": None,
        "pool": [],           # 证据池: validated statement dicts
        "reasoning": [],      # hypothesize 产出
        "report": None,
    }
    return session_id


def get_session(session_id: str) -> dict:
    session = _SESSIONS.get(session_id)
    if session is None:
        raise SessionNotFound(session_id)
    return session


# ---------------- 线索与闸门 ----------------

def _scene_clues(kb: KnowledgeBase, scene_id: str) -> list[Clue]:
    scene = kb.get_scene(scene_id) or {}
    return [Clue.model_validate(c) for c in scene.get("clues", [])]


def available_clues(session: dict, kb: KnowledgeBase) -> list[Clue]:
    """迷雾已散开的线索:未解锁且 requires 全部已解锁。"""
    unlocked = set(session["unlocked"])
    return [
        c
        for c in _scene_clues(kb, session["scene_id"])
        if c.id not in unlocked and all(r in unlocked for r in c.requires)
    ]


def _find_clue(session: dict, kb: KnowledgeBase, clue_id: str) -> Clue:
    for c in _scene_clues(kb, session["scene_id"]):
        if c.id == clue_id:
            return c
    raise ActionError(f"线索不存在: {clue_id}")


def _format_claims(claims: list[dict], kb: KnowledgeBase) -> str:
    lines = []
    for c in claims:
        src = kb.source_of(c)
        src_name = src["title"] if src else "未知来源"
        note = f" (争议: {c['dispute_note']})" if c.get("dispute_note") else ""
        lines.append(f"- [{c['id']}] ({c['tier']}, 来源: {src_name}) {c['text']}{note}")
    return "\n".join(lines) or "(无资料)"


# ---------------- LLM 调用与回退 ----------------

def _fallback_statements(clue: Clue, claims: list[dict]) -> list[dict]:
    if not claims:
        return [{"text": "知识库中没有支持该方向的可靠资料,暂无法下结论。", "claim_ids": []}]
    return [{"text": c["text"], "claim_ids": [c["id"]]} for c in claims]


def _ask_statements(question: str, claims: list[dict], kb: KnowledgeBase, fallback: list[dict]) -> dict:
    return llm.chat_json(
        [
            {"role": "system", "content": JSON_INSTRUCTION + ' 格式: {"statements": [{"text": "...", "claim_ids": ["..."]}]}'},
            {"role": "user", "content": f"资料:\n{_format_claims(claims, kb)}\n\n问题:{question}\n\n基于资料回答,每条结论标注引用的 claim id;资料不足则给一条 claim_ids 为空的说明。"},
        ],
        fallback={"statements": fallback},
    )


# ---------------- 各动作处理 ----------------

def _act_plan(session: dict, kb: KnowledgeBase, result_ctx: dict) -> dict:
    cands = available_clues(session, kb)
    if not cands:
        return {"clue_id": None, "message": "所有可及线索已调查完毕,可以提出假设或结案。"}
    fallback = sorted(cands, key=lambda c: c.order)[0]
    cand_lines = "\n".join(f"- {c.id}: [{c.tag}] {c.title}" for c in cands)
    scene = kb.get_scene(session["scene_id"]) or {}
    pick = llm.chat_json(
        [
            {"role": "system", "content": JSON_INSTRUCTION + ' 格式: {"clue_id": "..."}'},
            {
                "role": "user",
                "content": (
                    f"悬案:{scene.get('root_question') or scene.get('puzzle') or ''}\n"
                    f"已解锁线索:{', '.join(session['unlocked']) or '无'}\n"
                    f"候选线索:\n{cand_lines}\n\n选择最值得优先调查的一条,输出其 id。"
                ),
            },
        ],
        fallback={"clue_id": fallback.id},
    )
    chosen_id = pick.get("clue_id")
    chosen = next((c for c in cands if c.id == chosen_id), fallback)
    session["current_clue_id"] = chosen.id
    return {
        "clue_id": chosen.id,
        "tag": chosen.tag,
        "title": chosen.title,
        "question": chosen.question,
    }


def _act_investigate(session: dict, kb: KnowledgeBase, clue_id: str | None, result_ctx: dict) -> dict:
    if not clue_id:
        raise ActionError("investigate 需要 clue_id")
    if session["state"] == "closed":
        raise ActionError("会话已结案")
    clue = _find_clue(session, kb, clue_id)
    if clue.id in session["unlocked"]:
        raise ActionError(f"线索已解锁: {clue_id}")
    locked = [r for r in clue.requires if r not in session["unlocked"]]
    if locked:
        raise ActionError(f"线索 {clue_id} 尚未解锁,需要先调查: {', '.join(locked)}")

    claims = kb.scope_claims(clue.claim_ids)
    fallback = _fallback_statements(clue, claims)
    out = _ask_statements(clue.question, claims, kb, fallback)
    statements = validate.validate_statements(out.get("statements", fallback), kb)
    if not statements:
        statements = validate.validate_statements(fallback, kb)

    session["unlocked"].append(clue.id)
    session["pool"].extend(statements)

    result = {
        "clue_id": clue.id,
        "statements": statements,
    }
    if clue.kind == "trap":
        # 陷阱线索:没有任何 FACT/THEORY 级别的有效支撑即视为成功识别
        result["trap"] = True
        result["refused"] = all(st["tier"] in ("MODEL_GUESS", "FICTION") for st in statements)
    return result


def _act_opinions(session: dict, kb: KnowledgeBase, result_ctx: dict) -> dict:
    unlocked = set(session["unlocked"])
    claims: dict[str, dict] = {}
    for clue in _scene_clues(kb, session["scene_id"]):
        if clue.id in unlocked:
            for c in kb.scope_claims(clue.claim_ids):
                if c["tier"] == "THEORY":
                    claims[c["id"]] = c
    if not claims:
        raise ActionError("尚未解锁任何包含学界分歧(THEORY)的线索")
    theory_list = list(claims.values())
    fallback = [
        {"text": c["text"], "claim_ids": [c["id"]]} for c in theory_list
    ]
    out = _ask_statements(
        "关于此问题,学界有哪些不同观点?逐一陈述,并注明争议点。",
        theory_list,
        kb,
        fallback,
    )
    statements = validate.validate_statements(out.get("statements", fallback), kb)
    if not statements:
        statements = validate.validate_statements(fallback, kb)
    session["pool"].extend(statements)
    return {
        "statements": statements,
        "disputes": [st["dispute_note"] for st in statements if st.get("dispute_note")],
    }


def _act_hypothesize(session: dict, kb: KnowledgeBase, text: str | None, result_ctx: dict) -> dict:
    if not text or not text.strip():
        raise ActionError("hypothesize 需要 text(假设内容)")
    if not session["pool"]:
        raise ActionError("证据池为空,先调查至少一条线索")
    pool_lines = "\n".join(
        f"- ({st['tier']}) {st['text']} {st['claim_ids']}" for st in session["pool"]
    )
    fallback = {
        "verdict": "undetermined",
        "statements": [
            {"text": f"关于假设「{text.strip()}」:当前证据池不足以判定,需继续调查。", "claim_ids": []}
        ],
    }
    out = llm.chat_json(
        [
            {"role": "system", "content": JSON_INSTRUCTION + ' 格式: {"verdict": "supported|refuted|undetermined", "statements": [{"text": "...", "claim_ids": ["..."]}]}'},
            {"role": "user", "content": f"证据池:\n{pool_lines}\n\n假设:{text}\n\n评判该假设,每条理由标注引用的 claim id。"},
        ],
        fallback=fallback,
    )
    statements = validate.validate_statements(out.get("statements", fallback["statements"]), kb)
    session["reasoning"].extend(statements)
    session["pool"].extend(statements)
    session["state"] = "reasoning"
    return {"verdict": out.get("verdict", "undetermined"), "statements": statements}


def _deterministic_report(session: dict, kb: KnowledgeBase) -> str:
    scene = kb.get_scene(session["scene_id"]) or {}
    lines = [f"# 结案报告:{scene.get('title', '')}", ""]
    lines.append(f"**异常现象**:{scene.get('anomaly') or '(未设定)'}")
    lines.append(f"**根问题**:{scene.get('root_question') or scene.get('puzzle') or '(未设定)'}")
    lines.append("")
    lines.append("## 证据链(按可信级别)")
    for tier in ("FACT", "THEORY", "FICTION", "MODEL_GUESS"):
        items = [st for st in session["pool"] if st["tier"] == tier]
        if not items:
            continue
        lines.append(f"### {tier}")
        for st in items:
            cite = " ".join(f"[{c}]" for c in st["claim_ids"])
            mark = " ⚠已降级" if st.get("downgraded") else ""
            lines.append(f"- {st['text']} {cite}{mark}")
            if st.get("dispute_note"):
                lines.append(f"  - 争议:{st['dispute_note']}")
    if session["reasoning"]:
        lines.append("")
        lines.append("## 推理评判")
        for st in session["reasoning"]:
            lines.append(f"- ({st['tier']}) {st['text']}")
    lines.append("")
    lines.append("## 结论边界")
    facts = sum(1 for st in session["pool"] if st["tier"] == "FACT")
    theories = sum(1 for st in session["pool"] if st["tier"] == "THEORY")
    lines.append(f"本次调查共获得史实 {facts} 条、学说 {theories} 条。学说部分存在学界分歧,请将其视为一种解释而非定论。")
    return "\n".join(lines)


def _act_close(session: dict, kb: KnowledgeBase, result_ctx: dict) -> dict:
    if not any(st["tier"] in ("FACT", "THEORY") for st in session["pool"]):
        raise ActionError("证据池中没有任何 FACT/THEORY 级别的证据,无法结案")
    fallback_report = _deterministic_report(session, kb)
    out = llm.chat_json(
        [
            {"role": "system", "content": JSON_INSTRUCTION + ' 格式: {"report": "markdown 字符串"}'},
            {"role": "user", "content": "基于以下草稿润色结案报告,不得增删证据:\n" + fallback_report},
        ],
        fallback={"report": fallback_report},
    )
    report = out.get("report") or fallback_report
    session["state"] = "closed"
    session["report"] = report
    return {"report": report}


# ---------------- 对外入口 ----------------

_HANDLERS = {
    "plan": lambda s, kb, a: _act_plan(s, kb, a),
    "investigate": lambda s, kb, a: _act_investigate(s, kb, a.get("clue_id"), a),
    "opinions": lambda s, kb, a: _act_opinions(s, kb, a),
    "hypothesize": lambda s, kb, a: _act_hypothesize(s, kb, a.get("text"), a),
    "close": lambda s, kb, a: _act_close(s, kb, a),
}


def do_action(session_id: str, action: str, args: dict, kb: KnowledgeBase | None = None) -> SessionOut:
    kb = kb or get_kb()
    session = get_session(session_id)
    handler = _HANDLERS.get(action)
    if handler is None:
        raise ActionError(f"未知动作: {action}")
    result = handler(session, kb, args)
    return to_out(session_id, session, kb, result=result)


def to_out(session_id: str, session: dict, kb: KnowledgeBase, result: dict | None = None) -> SessionOut:
    unlocked = set(session["unlocked"])
    clues = [
        ClueNode(
            id=c.id,
            tag=c.tag,
            title=c.title,
            question=c.question,
            kind=c.kind,
            unlocked=c.id in unlocked,
            requires=c.requires,
        )
        for c in _scene_clues(kb, session["scene_id"])
    ]
    pool = [
        PoolItem(
            text=st["text"],
            tier=st["tier"],
            claim_ids=st["claim_ids"],
            dispute_note=st.get("dispute_note"),
        )
        for st in session["pool"]
    ]
    return SessionOut(
        session_id=session_id,
        scene_id=session["scene_id"],
        state=session["state"],
        current_clue_id=session["current_clue_id"],
        clues=clues,
        pool=pool,
        result=result,
        report=session["report"],
    )
