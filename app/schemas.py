"""数据模型与 API 契约(队友并行开发的依据,字段先定死)。

数据侧: sources / claims / entities / relations / scenes (data/*.json)
tier 四级: FACT(史实) > THEORY(学说) > FICTION(虚构) > MODEL_GUESS(模型猜测)
"""

from typing import Literal

from pydantic import BaseModel, Field

Tier = Literal["FACT", "THEORY", "FICTION", "MODEL_GUESS"]
Mode = Literal["mystery", "theater", "sandbox"]
Action = Literal["plan", "investigate", "opinions", "hypothesize", "close"]

TIER_RANK: dict[str, int] = {"FACT": 0, "THEORY": 1, "FICTION": 2, "MODEL_GUESS": 3}


# ---------------- 知识库数据模型 ----------------

class Source(BaseModel):
    id: str
    title: str
    type: str
    url: str | None = None
    license: str | None = None


class Claim(BaseModel):
    id: str
    text: str
    tier: Tier
    source_id: str
    locator: str | None = None
    entity_ids: list[str] = []
    tags: list[str] = []
    dispute_note: str | None = None  # THEORY 必填


class Entity(BaseModel):
    id: str
    type: str
    name: str
    summary: str = ""
    claim_ids: list[str] = []


class Relation(BaseModel):
    src: str
    dst: str
    label: str
    claim_ids: list[str] = Field(default_factory=list)


class Clue(BaseModel):
    id: str
    tag: str
    title: str
    question: str                      # 展示给用户的探索问题
    claim_ids: list[str] = []
    requires: list[str] = []           # 前置线索 id(迷雾闸门)
    kind: Literal["evidence", "theory", "trap"] = "evidence"
    topics: list[str] = []
    order: int = 0                     # LLM 选择失败时的回退顺序


class Hotspot(BaseModel):
    """迷雾地图上的兴趣点(剧场/沙盒模式用)。"""

    id: str
    label: str
    entity_ids: list[str] = []
    claim_ids: list[str] = []


class Scene(BaseModel):
    id: str
    mode: Mode
    title: str
    intro: str
    anomaly: str | None = None         # 开场异常现象(可选)
    root_question: str | None = None   # 根问题(可选)
    entity_ids: list[str] = []
    puzzle: str | None = None
    clues: list[Clue] = []
    hotspots: list[Hotspot] = []
    genres: list[str] = []             # 剧场模式的故事类型选项
    start_entity: str | None = None    # 沙盒模式入口实体


# ---------------- 校验结果 ----------------

class StatementIn(BaseModel):
    text: str
    claim_ids: list[str] = []


class StatementOut(BaseModel):
    text: str
    claim_ids: list[str] = []          # 仅保留真实存在的
    invalid_claim_ids: list[str] = []  # 被丢弃的编造 id
    tier: Tier                         # 后端根据引用计算
    downgraded: bool = False
    dispute_note: str | None = None    # 含 THEORY 时透出争议说明


# ---------------- 会话 / API ----------------

class SceneSummary(BaseModel):
    id: str
    mode: Mode
    title: str
    intro: str


class CreateSessionIn(BaseModel):
    scene_id: str


class ActionIn(BaseModel):
    action: Action
    clue_id: str | None = None
    text: str | None = None            # hypothesize 的假设内容


class ClueNode(BaseModel):
    id: str
    tag: str
    title: str
    question: str
    kind: Literal["evidence", "theory", "trap"]
    unlocked: bool
    requires: list[str] = []


class PoolItem(BaseModel):
    text: str
    tier: Tier
    claim_ids: list[str] = []
    dispute_note: str | None = None


class SessionOut(BaseModel):
    session_id: str
    scene_id: str
    state: str                         # investigating | reasoning | closed
    current_clue_id: str | None = None
    clues: list[ClueNode] = []         # 迷雾地图:已解锁/未解锁
    pool: list[PoolItem] = []          # 证据池
    result: dict | None = None         # 本次动作的产出(LLM 内容+校验)
    report: str | None = None          # close 后的结案报告
