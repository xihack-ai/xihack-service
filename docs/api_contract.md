# xihack 接口契约 v0.2(给前端同学,可先按此做 mock)

Base URL: `http://127.0.0.1:8000` ;交互示例见 `web/index.html` ;在线文档 `/docs`。

## 通用约定

- tier 四级:`FACT`(史实) > `THEORY`(学说,带争议说明) > `FICTION`(虚构) > `MODEL_GUESS`(无据猜测)。
  tier 由后端根据引用计算,前端直接展示即可;`downgraded: true` 建议加"⚠引用被降级"标记。
- 错误码:
  - `400` 非法动作/闸门未过,`detail` 为人话(如缺少哪条前置线索);
  - `404` 场景或会话不存在;
  - `422` 动作名/字段不合法(请求本身格式错);
  - `501` 模式尚未实现(theater / sandbox 阶段 2 实装)。
- 场景三模式:`mystery`(谜境,已实现)/ `theater`(剧场)/ `sandbox`(沙盒探索)。

## GET /scenes

```json
[
  {"id": "myst_s01", "mode": "mystery", "title": "【占位】这件器物是做什么用的?", "intro": "……"}
]
```

## GET /scenes/{scene_id}

返回场景原文。谜境含 `anomaly / root_question / puzzle / clues[]`;
剧场含 `genres[] / hotspots[]`(hotspot: `{id, label, entity_ids, claim_ids}`);
沙盒含 `start_entity`。`anomaly / root_question` 可能为 `null`,请做缺省展示。

## POST /sessions

```json
// 请求
{ "scene_id": "myst_s01" }
// 200 响应 = SessionOut(见下)
// thea_s01 / sand_s01 → 501
```

## POST /sessions/{session_id}/actions

```json
// 请求(五选一动作;clue_id/text 按动作给)
{ "action": "investigate", "clue_id": "myst_cl_env" }
```

| action | 参数 | 含义 | result 关键字段 |
|---|---|---|---|
| `plan` | - | 推荐下一条可及线索 | `clue_id, question` |
| `investigate` | `clue_id` | 解锁线索、产出证据 | `statements[]` |
| `opinions` | - | 多方观点(需已解锁 theory 线索) | `statements[], disputes[]` |
| `hypothesize` | `text` | 假设与证据池对照 | `verdict: supported\|refuted\|undetermined, statements[]` |
| `close` | - | 结案(证据池须有 FACT/THEORY) | `report`(markdown) |

```json
// 200 响应 = SessionOut
{
  "session_id": "ab12cd34ef56",
  "scene_id": "myst_s01",
  "state": "investigating",              // investigating | reasoning | closed
  "current_clue_id": "myst_cl_env",
  "clues": [                             // 迷雾地图:unlocked=false 显示"???"
    {"id": "myst_cl_env", "tag": "出土环境", "title": "它出土在哪?",
     "question": "……", "kind": "evidence", "unlocked": true, "requires": []},
    {"id": "myst_cl_theory", "tag": "学界分歧", "title": "专家怎么说?",
     "question": "……", "kind": "theory", "unlocked": false, "requires": ["myst_cl_material"]}
  ],
  "pool": [                              // 证据池,追加式
    {"text": "【占位】该器物出土于某遗址第三层……", "tier": "FACT",
     "claim_ids": ["myst_c01"], "dispute_note": null}
  ],
  "result": {                            // 仅本次动作有值
    "clue_id": "myst_cl_env",
    "statements": [
      {"text": "……", "claim_ids": ["myst_c01"], "invalid_claim_ids": [],
       "tier": "FACT", "downgraded": false, "dispute_note": null}
    ]
  },
  "report": null                         // close 后为 markdown 结案报告
}
```

`result.statements[].invalid_claim_ids` 非空 = 模型编造了引用且已被拦截,可展示拦截提示。

## GET /sessions/{session_id}

返回当前 `SessionOut`(`result` 为 null)。会话存内存,服务重启即失效,前端刷新请重新建会话。

## GET /api/kb/search

`?q=出土&scene_id=myst_s01&top_k=5` → claim 列表(含 `id/text/tier/source_id`),检索范围为该场景可见证据。

## GET /health

```json
{"status": "ok", "llm_configured": false, "replay_only": true}
```
