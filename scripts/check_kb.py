"""知识库检查脚本(给数据同学)。

用法:
    python scripts/check_kb.py data/sample_kb.json

校验通过退出码 0,失败列出全部问题并退出码 1。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.kb import validate_kb  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python scripts/check_kb.py <kb.json>")
        return 2
    path = Path(sys.argv[1])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"无法读取 {path}: {e}")
        return 1

    errors = validate_kb(raw)
    if errors:
        print(f"✗ {path} 共 {len(errors)} 个问题:")
        for e in errors:
            print(f"  - {e}")
        return 1

    counts = {k: len(raw.get(k, [])) for k in ("sources", "claims", "entities", "relations", "scenes")}
    print(f"✓ {path} 校验通过", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
