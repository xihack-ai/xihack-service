"""表格转 JSON 脚本(给数据同学)。

把 7 张表导出的 CSV 合并成 kb.json,合并后自动跑校验,通过才写回。

约定:
- CSV 一律用 utf-8-sig 读取(兼容 Excel 导出的 BOM);
- 多值列用英文分号 `;` 分隔,如 `myst_c01;myst_c02`;
- 文件名为 <表名>.csv,放在同一目录:
    sources.csv  claims.csv  entities.csv  relations.csv
    scenes.csv   clues.csv   hotspots.csv
  (允许只出现部分表;clues/hotspots 通过 scene_id 列挂到对应场景)
- 空单元格 = 空字符串/空列表;tier 用 FACT/THEORY/FICTION/MODEL_GUESS。

用法:
    python scripts/csv_to_kb.py csv_dir/ -o data/kb.json
"""

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.kb import validate_kb  # noqa: E402

TABLES = ["sources", "claims", "entities", "relations", "scenes", "clues", "hotspots"]

# 多值列(按 `;` 拆分)
MULTI = {
    "claims": {"entity_ids", "tags"},
    "entities": {"claim_ids"},
    "relations": {"claim_ids"},
    "scenes": {"entity_ids", "genres"},
    "clues": {"claim_ids", "requires", "topics"},
    "hotspots": {"entity_ids", "claim_ids"},
}
# 整数列
INT = {"clues": {"order"}}
# 空串 -> None 的列
NULLABLE = {"claims": {"locator", "dispute_note"}, "scenes": {"anomaly", "root_question", "puzzle", "start_entity"}}


def _rows(csv_dir: Path, table: str) -> list[dict]:
    path = csv_dir / f"{table}.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if any((v or "").strip() for v in r.values())]


def _convert(table: str, row: dict) -> dict:
    out: dict = {}
    for key, value in row.items():
        if key is None or value is None:
            continue
        key = key.strip()
        value = value.strip()
        if not value:
            out[key] = None if key in NULLABLE.get(table, set()) else ([] if key in MULTI.get(table, set()) else "")
        elif key in MULTI.get(table, set()):
            out[key] = [v.strip() for v in value.split(";") if v.strip()]
        elif key in INT.get(table, set()):
            out[key] = int(value)
        else:
            out[key] = value
    return out


def main() -> int:
    args = sys.argv[1:]
    if not args or "-o" not in args:
        print(__doc__)
        return 2
    out_path = Path(args[args.index("-o") + 1])
    csv_dir = Path(args[0])

    kb: dict = {t: [] for t in TABLES}
    counts: dict[str, int] = {}
    errors: list[str] = []

    for table in TABLES:
        rows = _rows(csv_dir, table)
        items = []
        for i, row in enumerate(rows):
            try:
                items.append(_convert(table, row))
            except ValueError as e:
                errors.append(f"{table}.csv 第 {i + 2} 行(含表头): {e}")
        kb[table] = items
        counts[table] = len(items)

    # 子表挂载:clues/hotspots 按 scene_id 归入场景,其余字段原样保留
    if kb["clues"] or kb["hotspots"]:
        scene_idx = {s.get("id"): s for s in kb["scenes"]}
        for sub, field in (("clues", "clues"), ("hotspots", "hotspots")):
            rest = []
            for item in kb[sub]:
                sid = item.pop("scene_id", "")
                scene = scene_idx.get(sid)
                if scene is None:
                    errors.append(f"{sub}.csv: {item.get('id', '?')} 的 scene_id 不存在: {sid!r}")
                    continue
                scene.setdefault(field, []).append(item)
            kb[sub] = rest

    if errors:
        print("✗ CSV 解析/挂载问题,未写回:")
        for e in errors:
            print(f"  - {e}")
        return 1

    kb = {k: v for k, v in kb.items() if v}  # 去掉空表
    errors = validate_kb(kb)
    if errors:
        print(f"✗ 合并后校验失败({len(errors)} 个问题),未写回:")
        for e in errors:
            print(f"  - {e}")
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(kb, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"✓ 已生成 {out_path}", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
