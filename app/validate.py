"""结构化 statements 校验与 tier 计算。

新校验器(主路径):
- 输入模型输出的结构化 statements [{text, claim_ids}]
- 编造的 claim_id 丢弃,并把该条 tier 降一级(FACT->THEORY->FICTION->MODEL_GUESS)
- tier = 有效引用中最差的一档;含 THEORY 时即显示 THEORY
- 无任何有效引用 -> MODEL_GUESS

旧正则抽取(降级路径,新校验器稳定前保留):
- validate_answer_text(): 模型偶尔不按 JSON 输出时,从纯文本里抽 [claim-id]
"""

import re

from app.kb import KnowledgeBase
from app.schemas import TIER_RANK, StatementOut

CLAIM_REF_RE = re.compile(r"\[([A-Za-z][\w-]*)\]")

_RANK_TO_TIER = {v: k for k, v in TIER_RANK.items()}
_MAX_RANK = max(TIER_RANK.values())


def _worst_tier(tiers: list[str]) -> str:
    return _RANK_TO_TIER[max(TIER_RANK.get(t, _MAX_RANK) for t in tiers)]


def _downgrade_tier(tier: str) -> str:
    return _RANK_TO_TIER[min(TIER_RANK.get(tier, _MAX_RANK) + 1, _MAX_RANK)]


def validate_statements(statements: list[dict], kb: KnowledgeBase) -> list[dict]:
    """校验并计算 tier,返回 StatementOut 形态的 dict 列表。"""
    out: list[dict] = []
    for st in statements:
        text = str(st.get("text", "")).strip()
        if not text:
            continue
        cited = [str(c) for c in st.get("claim_ids", [])]
        valid: list[dict] = []
        invalid: list[str] = []
        for cid in cited:
            claim = kb.get_claim(cid)
            if claim is None:
                invalid.append(cid)
            elif claim not in valid:
                valid.append(claim)

        downgraded = False
        if not valid:
            tier = "MODEL_GUESS"
            downgraded = bool(cited)  # 有引用但全是编造
        else:
            tier = _worst_tier([c["tier"] for c in valid])
            if invalid:
                tier = _downgrade_tier(tier)
                downgraded = True

        dispute_notes = [c["dispute_note"] for c in valid if c.get("dispute_note")]
        out.append(
            StatementOut(
                text=text,
                claim_ids=[c["id"] for c in valid],
                invalid_claim_ids=invalid,
                tier=tier,
                downgraded=downgraded,
                dispute_note=" / ".join(dispute_notes) or None,
            ).model_dump()
        )
    return out


# ---------------- 旧正则降级路径 ----------------

def extract_claim_ids(text: str) -> list[str]:
    seen: list[str] = []
    for cid in CLAIM_REF_RE.findall(text):
        if cid not in seen:
            seen.append(cid)
    return seen


def validate_answer_text(text: str, kb: KnowledgeBase) -> dict:
    """兼容模型输出纯文本 + [claim-id] 引用的情况。"""
    ids = extract_claim_ids(text)
    [st] = validate_statements([{"text": text, "claim_ids": ids}], kb)
    return st
