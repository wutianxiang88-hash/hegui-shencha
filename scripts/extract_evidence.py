#!/usr/bin/env python3
"""extract_evidence.py — 按初筛 flag 的证据定位，从原文截取复核输入。

用法:
    python3 extract_evidence.py <screen_result.json> <text目录> <输出review_input.md> [选项]

选项:
    --window N    证据定位词前后各截取 N 字符（默认 300，对应 SKILL.md 的"前后各 300 字"）
    --all-files   未提供定位词时搜索目录下全部 .txt（默认只搜 flag 指定的文件）
    --index       附一份可核对的索引 JSON，供 Phase 4 登记册填"证据定位"列

设计约束:
  1. 定位失败必须明确标注"定位失败"，绝不拿相邻段落或整篇材料冒充证据。
     复核员看不到错误的上下文，比说"没找到"危险得多。
  2. 输出文件同时可能包含材料原文（证据段落）——按 dd-triage 硬规则，
     该文件只进 14B 的请求，不进指挥模型上下文。
  3. 定位词优先按"原文中最长且真实存在的连续字符串"匹配；支持一个 flag 带多个候选定位词。
"""
import argparse
import json
import os
import re
import sys

DEFAULT_WINDOW = 300


def load_texts(text_dir):
    files = {}
    for n in sorted(os.listdir(text_dir)):
        if n.lower().endswith(".txt"):
            with open(os.path.join(text_dir, n), "r", encoding="utf-8", errors="replace") as f:
                files[n] = f.read()
    if not files:
        sys.exit(f"[拒绝] {text_dir} 下没有 .txt 文件。先完成 Phase 0 材料入库。")
    return files


def normalize(s):
    return re.sub(r"\s+", "", (s or ""))


def locate(term, files, restrict=None):
    """在原文中定位 term，返回 (文件名, 起点, 终点)；找不到返回 None。

    匹配在去空白后的文本上进行（换行/空格不影响匹配），
    但返回的坐标会映射回原文索引，保证截取的是原文原样。
    """
    t = normalize(term)
    if not t:
        return None
    for name, text in files.items():
        if restrict and restrict not in name:
            continue
        stripped, mapping = [], []
        for i, ch in enumerate(text):
            if not ch.isspace():
                stripped.append(ch)
                mapping.append(i)
        flat = "".join(stripped)
        pos = flat.find(t)
        if pos >= 0:
            return name, mapping[pos], mapping[min(pos + len(t) - 1, len(mapping) - 1)]
    return None


def resolve_restrict(ev, files):
    """把 flag 的"文件"字段解析成 text 目录里真实存在的文件名。

    解析顺序：① 精确匹配；② 去掉目录前缀后的 basename 匹配；
    ③ 去掉扩展名的 stem 模糊匹配。④ 都解析不出来 → None，即不限制文件、全文搜索。
    宁可全文搜索，也不要因为模型写了"待审材料"这种占位词就丢掉一条红旗。
    """
    if not isinstance(ev, dict):
        return None
    f = ev.get("文件") or ev.get("file")
    if not isinstance(f, str) or not f.strip():
        return None
    name = f.strip()
    if name in files:
        return name
    base = os.path.basename(name)
    if base in files:
        return base
    stem = os.path.splitext(base)[0]
    hits = [n for n in files if stem and stem in n]
    if len(hits) == 1:
        return hits[0]
    if hits:
        return max(hits, key=len)  # 多个候选时取最具体的
    return None


def excerpt(text, start, end, window):
    a = max(0, start - window)
    b = min(len(text), end + 1 + window)
    return text[a:b], a, b


def candidates(flag, files):
    """收集一个 flag 的所有候选定位词，并把"文件"字段解析成真实文件名。

    模型经常不照约定写文件名（实测 Saluki 写成"待审材料"这类占位词）。
    此时必须放开文件限制改为全文搜索，而不是让整条红旗降级成"定位失败"——
    那会静默丢掉一条红旗。
    """
    ev = flag.get("证据定位")
    cands, restrict = [], None
    if isinstance(ev, dict):
        for k in ("定位词", "关键词", "keyword", "引文", "原文引用"):
            v = ev.get(k)
            if isinstance(v, str) and v.strip():
                cands.append(v.strip())
        restrict = resolve_restrict(ev, files)
    elif isinstance(ev, str):
        cands.append(ev.strip())
    if not cands and isinstance(flag.get("原文引用"), str):
        cands.append(flag["原文引用"].strip())
    # 长的优先：定位词越具体越不容易撞到别处
    cands.sort(key=len, reverse=True)
    return cands, restrict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("screen_json")
    ap.add_argument("text_dir")
    ap.add_argument("output_md")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--all-files", action="store_true")
    ap.add_argument("--index")
    args = ap.parse_args()

    if not os.path.isfile(args.screen_json):
        sys.exit(f"[拒绝] 输入文件不存在：{args.screen_json}")
    with open(args.screen_json, "r", encoding="utf-8") as f:
        obj = json.load(f)
    flags = obj.get("flags")
    if not isinstance(flags, list):
        sys.exit("[拒绝] screen_result.json 缺少 flags 数组（结构不完整），按 SKILL.md 停止。")

    files = load_texts(args.text_dir)
    L, index, ok_n, fail_n = [], [], 0, 0

    for i, flag in enumerate(flags, 1):
        fid = flag.get("编号", i)
        subj = flag.get("科目", "未命名科目")
        susp = flag.get("疑点", "")
        cands, restrict = candidates(flag, files)
        if args.all_files:
            restrict = None

        hit = None
        used_term = None
        for c in cands:
            hit = locate(c, files, restrict)
            if hit:
                used_term = c
                break

        L.append(f"## 疑点 {fid}：{subj}\n")
        L.append(f"**初筛疑点**：{susp}\n")
        if hit:
            name, s, e = hit
            text = files[name]
            snippet, a, b = excerpt(text, s, e, args.window)
            L.append(f"**证据定位**：文件 `{name}`，定位词 `{used_term}`，字符区间 {a}-{b}\n")
            L.append("**证据段落**：\n\n> " + snippet.replace("\n", "\n> ") + "\n")
            ok_n += 1
            index.append({"编号": fid, "科目": subj, "文件": name, "定位词": used_term,
                          "字符区间": [a, b], "定位状态": "成功", "疑点": susp})
        else:
            L.append("**证据定位**：定位失败（未在原文中找到可检索的定位词）。\n")
            L.append("> 复核员注意：此条无证据上下文，态度只能填 `无法判断`，不得根据疑点描述自行推测。\n")
            fail_n += 1
            index.append({"编号": fid, "科目": subj, "文件": restrict, "定位词": cands,
                          "定位状态": "失败", "疑点": susp})

    header = [
        "# 复核输入（对抗性复核专用）\n",
        f"- 疑点总数：{len(flags)}；定位成功 {ok_n}，定位失败 {fail_n}。",
        f"- 证据窗口：定位词前后各 {args.window} 字符。",
        "- 本文件含尽调材料原文片段：只进本地 14B 请求，**不进指挥模型上下文**。\n",
        "---\n",
    ]
    os.makedirs(os.path.dirname(os.path.abspath(args.output_md)) or ".", exist_ok=True)
    with open(args.output_md, "w", encoding="utf-8") as f:
        f.write("\n".join(header + L))

    if args.index:
        with open(args.index, "w", encoding="utf-8") as f:
            json.dump({"total": len(flags), "ok": ok_n, "failed": fail_n, "items": index},
                      f, ensure_ascii=False, indent=2)

    print(f"[完成] {args.output_md}")
    print(f"       疑点 {len(flags)} 条：定位成功 {ok_n}，定位失败 {fail_n}")
    for it in index:
        if it["定位状态"] == "失败":
            print(f"       [定位失败] 疑点 {it['编号']} {it['科目']}：候选定位词={it['定位词']}")
    if fail_n:
        print("       定位失败的条目已标注，14B 复核时态度应为「无法判断」，"
              "不得编造上下文。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
