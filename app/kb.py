"""知识库加载、启动校验与检索(中文二字词分词)。

- 数据格式: sources / claims / entities / relations / scenes
- 启动时校验,不合法直接抛 KbValidationError(服务拒绝启动)
"""

import json
import os
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from app.schemas import Claim, Entity, Relation, Scene, Source

DEFAULT_KB_PATH = "data/sample_kb.json"

_EN_RE = re.compile(r"[a-zA-Z0-9]+")
_CN_RE = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str) -> set[str]:
    """英文按单词,中文按连续汉字的二字词(比单字噪声小)。"""
    tokens: set[str] = set()
    for w in _EN_RE.findall(text):
        tokens.add(w.lower())
    for run in _CN_RE.findall(text):
        if len(run) == 1:
            tokens.add(run)
        for i in range(len(run) - 1):
            tokens.add(run[i : i + 2])
    return tokens


class KbValidationError(Exception):
    """知识库数据不合法,messages 为全部问题。"""

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        super().__init__("知识库校验失败:\n" + "\n".join(f"- {m}" for m in messages))


def _parse_section(items: list[dict], model, what: str, errors: list[str]) -> list:
    parsed = []
    for i, item in enumerate(items):
        try:
            parsed.append(model.model_validate(item))
        except ValidationError as e:
            first = e.errors()[0] if e.errors() else {}
            errors.append(
                f"{what}[{i}] (id={item.get('id', '?')}) 字段 {first.get('loc')} 不合法: {first.get('msg')}"
            )
    return parsed


def _dup_errors(objs: list, what: str, errors: list[str]) -> None:
    counter = Counter(getattr(o, "id", None) for o in objs)
    for oid, n in counter.items():
        if oid is not None and n > 1:
            errors.append(f"{what} id 重复: {oid} x{n}")


def validate_kb(raw: dict) -> list[str]:
    """返回全部错误信息(空列表 = 合法)。"""
    errors: list[str] = []

    sources = _parse_section(raw.get("sources", []), Source, "source", errors)
    claims = _parse_section(raw.get("claims", []), Claim, "claim", errors)
    entities = _parse_section(raw.get("entities", []), Entity, "entity", errors)
    relations = _parse_section(raw.get("relations", []), Relation, "relation", errors)
    scenes = _parse_section(raw.get("scenes", []), Scene, "scene", errors)

    _dup_errors(sources, "source", errors)
    _dup_errors(claims, "claim", errors)
    _dup_errors(entities, "entity", errors)
    _dup_errors(scenes, "scene", errors)

    source_ids = {s.id for s in sources}
    claim_ids = {c.id for c in claims}
    entity_ids = {e.id for e in entities}

    for c in claims:
        if c.source_id not in source_ids:
            errors.append(f"claim {c.id} 引用了不存在的 source: {c.source_id}")
        for e in c.entity_ids:
            if e not in entity_ids:
                errors.append(f"claim {c.id} 引用了不存在的 entity: {e}")
        if c.tier == "THEORY" and not c.dispute_note:
            errors.append(f"THEORY claim {c.id} 缺少 dispute_note")

    for e in entities:
        for cid in e.claim_ids:
            if cid not in claim_ids:
                errors.append(f"entity {e.id} 引用了不存在的 claim: {cid}")

    for i, r in enumerate(relations):
        if r.src not in entity_ids:
            errors.append(f"relation[{i}] src 不存在: {r.src}")
        if r.dst not in entity_ids:
            errors.append(f"relation[{i}] dst 不存在: {r.dst}")
        if not r.claim_ids:
            errors.append(f"relation[{i}] {r.src}-{r.label}->{r.dst} 没有任何依据 claim")
        for cid in r.claim_ids:
            if cid not in claim_ids:
                errors.append(f"relation[{i}] 引用了不存在的 claim: {cid}")

    for sc in scenes:
        for e in sc.entity_ids:
            if e not in entity_ids:
                errors.append(f"scene {sc.id} 引用了不存在的 entity: {e}")
        clue_ids = {cl.id for cl in sc.clues}
        if len(clue_ids) != len(sc.clues):
            errors.append(f"scene {sc.id} 存在重复 clue id")
        for cl in sc.clues:
            for cid in cl.claim_ids:
                if cid not in claim_ids:
                    errors.append(f"clue {cl.id} 引用了不存在的 claim: {cid}")
            for req in cl.requires:
                if req not in clue_ids:
                    errors.append(f"clue {cl.id} 的 requires 引用了不存在的 clue: {req}")
                if req == cl.id:
                    errors.append(f"clue {cl.id} 不能依赖自身")

        # requires 依赖图:必须无环,且至少有一条无前置线索可作为起点
        clue_map = {cl.id: cl for cl in sc.clues}
        cycle = _find_requires_cycle(clue_map)
        if cycle:
            errors.append(f"scene {sc.id} 的 requires 存在循环依赖: {' -> '.join(cycle)}")
        elif sc.clues and not any(not cl.requires for cl in sc.clues):
            errors.append(f"scene {sc.id} 所有线索都有前置依赖,没有可入手的起点")

        hotspot_ids: set[str] = set()
        for hs in sc.hotspots:
            if hs.id in hotspot_ids:
                errors.append(f"scene {sc.id} hotspot id 重复: {hs.id}")
            hotspot_ids.add(hs.id)
            for eid in hs.entity_ids:
                if eid not in entity_ids:
                    errors.append(f"hotspot {hs.id} 引用了不存在的 entity: {eid}")
            for cid in hs.claim_ids:
                if cid not in claim_ids:
                    errors.append(f"hotspot {hs.id} 引用了不存在的 claim: {cid}")

        if sc.start_entity is not None and sc.start_entity not in entity_ids:
            errors.append(f"scene {sc.id} 的 start_entity 不存在: {sc.start_entity}")

    return errors


def _find_requires_cycle(clue_map: dict) -> list[str] | None:
    """DFS 三色标记找环;找到则返回环路径(首尾同一 id)。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {cid: WHITE for cid in clue_map}

    def dfs(cid: str, stack: list[str]) -> list[str] | None:
        color[cid] = GRAY
        for req in clue_map[cid].requires:
            if req not in clue_map:  # 悬空引用另有报错
                continue
            if color[req] == GRAY:
                return stack + [cid, req]
            if color[req] == WHITE:
                found = dfs(req, stack + [cid])
                if found:
                    return found
        color[cid] = BLACK
        return None

    for cid in clue_map:
        if color[cid] == WHITE:
            found = dfs(cid, [])
            if found:
                return found
    return None


class KnowledgeBase:
    def __init__(self, raw: dict) -> None:
        self.sources = {s["id"]: s for s in raw.get("sources", [])}
        self.claims = {c["id"]: c for c in raw.get("claims", [])}
        self.entities = {e["id"]: e for e in raw.get("entities", [])}
        self.relations = raw.get("relations", [])
        self.scenes = {s["id"]: s for s in raw.get("scenes", [])}
        self._tokens = {
            cid: tokenize(c["text"] + " " + " ".join(c.get("tags", [])))
            for cid, c in self.claims.items()
        }

    # ---------- 基础访问 ----------

    def get_claim(self, claim_id: str) -> dict | None:
        return self.claims.get(claim_id)

    def get_scene(self, scene_id: str) -> dict | None:
        return self.scenes.get(scene_id)

    def list_scenes(self) -> list[dict]:
        return list(self.scenes.values())

    def source_of(self, claim: dict) -> dict | None:
        return self.sources.get(claim.get("source_id", ""))

    # ---------- 范围与检索 ----------

    def scene_claims(self, scene_id: str) -> list[dict]:
        """场景可见的全部 claims(场景实体关联 + 线索引用)。"""
        scene = self.scenes.get(scene_id, {})
        out: dict[str, dict] = {}
        for eid in scene.get("entity_ids", []):
            for cid in self.entities.get(eid, {}).get("claim_ids", []):
                if cid in self.claims:
                    out[cid] = self.claims[cid]
        for clue in scene.get("clues", []):
            for cid in clue.get("claim_ids", []):
                if cid in self.claims:
                    out[cid] = self.claims[cid]
        return list(out.values())

    def scope_claims(self, claim_ids: list[str]) -> list[dict]:
        """按 id 列表取 claims,忽略不存在的(存在性由校验层负责报错)。"""
        return [self.claims[cid] for cid in claim_ids if cid in self.claims]

    def search_claims(
        self, query: str, claim_ids: list[str] | None = None, top_k: int = 5
    ) -> list[dict]:
        """二字词重合度检索。claim_ids 给定时限定在该范围内(场景内检索用)。"""
        pool = self.scope_claims(claim_ids) if claim_ids is not None else list(self.claims.values())
        q = tokenize(query)
        if not q:
            return []
        scored: list[tuple[float, dict]] = []
        for c in pool:
            doc = self._tokens.get(c["id"], set())
            overlap = len(q & doc)
            if overlap > 0:
                scored.append((overlap / (len(q) ** 0.5), c))
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:top_k]]


def load_kb_dict(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)
    errors = validate_kb(raw)
    if errors:
        raise KbValidationError(errors)
    return raw


@lru_cache
def get_kb() -> KnowledgeBase:
    path = Path(os.getenv("KB_PATH", DEFAULT_KB_PATH))
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return KnowledgeBase(load_kb_dict(path))
