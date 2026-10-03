"""Run: python scripts/evaluate_judgments.py labeled_results.jsonl --output report.json"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.evaluation_metrics import evaluate_rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.input.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    report = evaluate_rows(rows)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已计算 {len(rows)} 个判定；来源与实测声明需依据输入记录核实。")


if __name__ == "__main__":
    main()
