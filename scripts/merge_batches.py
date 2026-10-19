#!/usr/bin/env python3
"""merge_batches.py — 合并分批初筛结果为单个 screen_result.json。

用法:
    python3 merge_batches.py <批次json...> <输出screen_result.json>

合并规则（只动结构，不动内容）:
  - numbers: 原样拼接（各批的文件名与原文引用不变，numcheck 按科目/期间归并）。
  - flags: 原样拼接，编号 按拼接顺序重排为 1..N（各批内部编号会重复，必须重排）。
  - 任一批次缺 numbers/flags 键或不是 dict：明确报错退出，不静默兜底。
"""
import json
import sys


def main():
    if len(sys.argv) < 3:
        sys.exit("用法: merge_batches.py <批次json...> <输出screen_result.json>")
    *inputs, out = sys.argv[1:]
    numbers, flags = [], []
    for path in inputs:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "numbers" not in data or "flags" not in data:
            sys.exit(f"[拒绝] {path} 结构不完整（缺 numbers/flags 或顶层不是对象）。按 SKILL.md 记录失败，不做兜底。")
        if not isinstance(data["numbers"], list) or not isinstance(data["flags"], list):
            sys.exit(f"[拒绝] {path} 的 numbers/flags 不是数组。")
        numbers.extend(data["numbers"])
        flags.extend(data["flags"])
        print(f"  并入 {path}: numbers+{len(data['numbers'])} flags+{len(data['flags'])}")
    for i, fl in enumerate(flags, 1):
        if isinstance(fl, dict):
            fl["编号"] = i
    merged = {"numbers": numbers, "flags": flags}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    print(f"[成功] 合并 {len(inputs)} 批 → {out}: numbers={len(numbers)} flags={len(flags)}")


if __name__ == "__main__":
    main()
