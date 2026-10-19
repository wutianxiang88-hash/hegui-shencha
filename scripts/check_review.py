#!/usr/bin/env python3
"""check_review.py — 校验 9B 复核输出，执行降级规则。

用法:
    python3 check_review.py <review_result.json> [--index evidence_index.json] [--out 校验报告.md]

背景（实测踩坑，2026-10-18）:
   qwen3.5:9b-q8_0 算术不可靠——问"应收增速比收入增速高多少个百分点"答 4.5（正确 55）。
   它一旦在复核理由里自己算数，结论就不可信。所以本脚本对每条复核做三道校验：

   降级规则（命中任一即把该条降级为「无法判断」）：
     R1 缺证据摘录：证据摘录 为空，或长度 < 10 字符（防 9B 凭空断言）
     R2 证据摘录非原文：摘录内容对不上输入证据段落里的任何连续子串（防 9B 自行造句）
     R3 复核理由含算术：出现 "= 数字" / "高出 X 个百分点" / "增长 X%" / "是…的 X 倍" 等算式痕迹

   另有两个 INFO 级提示（不降级，仅记录）：
     I1 态度为「无法判断」（可能是定位失败，属正常行为，登记册应提升关注度）
     I2 输入中存在定位失败的疑点但复核未提（复查 extract_evidence 的 --index）

设计原则：宁可把可疑结论降级成"无法判断"让人工回原文，也不要把一个
9B 编出来的"支持"送进登记册——后者会让复核环节看起来在工作，实际上放了水。
"""
import argparse
import json
import os
import re
import sys

ARITH_PATTERNS = [
    # 只匹配"模型自己在算"的痕迹：等式、百分点差、倍数、金额加减乘除
    (r"(高|低|超|差|少|多)\s*(出|了|于)?\s*[\d,.]+\s*(个)?百分点", "百分点计算"),
    (r"(增长|下降|上升|减少|增加|提高|降低)\s*(了)?\s*[\d,.]+\s*(个)?百分点", "百分点计算"),
    (r"=\s*[\d,.]+\s*(元|万元|亿元|%|个百分点|倍)", "等式痕迹"),
    (r"[\d,.]+\s*(万元|亿元|元)\s*[-+×x*/]\s*[\d,.]+\s*(万元|亿元|元)?", "金额加减乘除"),
    (r"是.{0,12}的\s*[\d,.]+\s*倍", "倍数计算"),
    (r"(合计|共计|总计|总额)\s*(为|是|达)?\s*[\d,.]+\s*(万元|亿元|元)", "自行汇总"),
]
ARITH_RE = [(re.compile(p), label) for p, label in ARITH_PATTERNS]

# 这些是"引用原文数字"的正常句式，不算算术。命中则跳过 R3。
CITE_SAFE_RE = re.compile(
    r"(原文|材料|证据|段落|披露|显示|列示|注明|记载|表述|摘录|写道|称|为)"
    r"[^。；\n]{0,24}?[\d,.]+\s*(万元|亿元|元|%|个百分点)"
)

MIN_EXCERPT_LEN = 10
VALID_ATTITUDES = ("支持", "反驳", "无法判断")


def norm(s):
    return re.sub(r"\s+", "", (s or ""))


def excerpt_in_evidence(excerpt, ev_text):
    """摘录是否为证据原文的连续子串（忽略空白，允许省略号分段）。"""
    e = norm(excerpt)
    if not e:
        return False, "摘录为空"
    parts = [p for p in re.split(r"[…\.]{2,}|\.{3,}", e) if p]
    if not parts:
        parts = [e]
    ev = norm(ev_text)
    missing = [p for p in parts if p not in ev]
    if missing:
        return False, f"摘录有 {len(missing)} 段在证据原文中找不到"
    return True, ""


def arith_hits(text):
    """检测复核理由里的算术痕迹。

    只看 复核理由，不看证据摘录——摘录是原文引用，里面本来就有百分比
    （"增长60.0%"），把它算进去会把正常引用误判成模型在算数（实测踩过）。
    命中"原文/材料/证据…显示…XX万元"这类引用句式时豁免。
    """
    if not text:
        return []
    hits = []
    for rx, label in ARITH_RE:
        m = rx.search(text)
        if not m:
            continue
        # 引用豁免：该匹配点前后 24 字符内出现"原文/材料/证据/披露/显示"等词，
        # 说明是在复述材料数字，不是自己在算
        lo = max(0, m.start() - 24)
        hi = min(len(text), m.end() + 24)
        ctx = text[lo:hi]
        if CITE_SAFE_RE.search(ctx):
            continue
        hits.append(label)
    return hits


def load_evidence_map(index_path):
    """从 extract_evidence.py 的 --index 读出每条疑点的证据段落，用于核对摘录。"""
    if not index_path or not os.path.isfile(index_path):
        return {}
    with open(index_path, "r", encoding="utf-8") as f:
        idx = json.load(f)
    return {str(it.get("编号")): it for it in idx.get("items", [])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("review_json")
    ap.add_argument("--index", help="extract_evidence.py 生成的 evidence_index.json")
    ap.add_argument("--evidence-dir", help="原文 text 目录，用于核对摘录是否逐字来自原文")
    ap.add_argument("--out", help="Markdown 校验报告输出路径")
    args = ap.parse_args()

    if not os.path.isfile(args.review_json):
        sys.exit(f"[拒绝] 文件不存在：{args.review_json}")
    with open(args.review_json, "r", encoding="utf-8") as f:
        obj = json.load(f)
    reviews = obj.get("reviews")
    if not isinstance(reviews, list):
        sys.exit("[拒绝] review_result.json 缺少 reviews 数组。")

    ev_map = load_evidence_map(args.index)
    text_cache = {}

    def evidence_text(fid):
        """该疑点可核对的证据全文：优先 evidence_index 记录的文件区间，其次整个文件。"""
        it = ev_map.get(str(fid)) or {}
        if not args.evidence_dir:
            return ""
        name = it.get("文件")
        if not name:
            return ""
        if name not in text_cache:
            p = os.path.join(args.evidence_dir, name)
            if not os.path.isfile(p):
                text_cache[name] = None
            else:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    text_cache[name] = f.read()
        t = text_cache.get(name)
        if t is None:
            return ""
        rng = it.get("字符区间")
        if isinstance(rng, list) and len(rng) == 2:
            lo, hi = rng
            # 放宽到 ±200 字符：evidence_index 记的是定位词区间，摘录可能来自窗口内更宽范围
            return t[max(0, lo - 200):min(len(t), hi + 200)]
        return t

    rows, downgraded, notes = [], [], []
    for i, rv in enumerate(reviews, 1):
        fid = rv.get("编号", i)
        att = (rv.get("态度") or "").strip()
        excerpt = rv.get("证据摘录") or ""
        reason = rv.get("复核理由") or ""
        reasons, final = [], att

        # R1 缺摘录
        if len(excerpt.strip()) < MIN_EXCERPT_LEN:
            reasons.append(f"R1 缺证据摘录（长度 {len(excerpt.strip())} < {MIN_EXCERPT_LEN}）")
        # R2 摘录非原文
        ev = evidence_text(fid)
        if len(excerpt.strip()) >= MIN_EXCERPT_LEN and ev:
            ok, why = excerpt_in_evidence(excerpt, ev)
            if not ok:
                reasons.append(f"R2 {why}")
        # R3 理由含算术（只看复核理由；摘录是原文引用，不算）
        hits = arith_hits(reason)
        if hits:
            reasons.append("R3 复核理由含算术痕迹：" + "、".join(sorted(set(hits))))

        if reasons and final != "无法判断":
            final = "无法判断"
            downgraded.append({"编号": fid, "原态度": att, "原因": reasons})
        if att not in VALID_ATTITUDES:
            reasons.append(f"I0 态度取值非法：{att!r}（应为 支持/反驳/无法判断）")
        if att == "无法判断":
            notes.append(f"I1 疑点 {fid} 复核为「无法判断」，登记册应提升一级关注度")
        if str(fid) in ev_map and ev_map[str(fid)].get("定位状态") == "失败":
            notes.append(f"I2 疑点 {fid} 原文定位失败，复核只能给「无法判断」；已如实传达则为正常")

        rows.append({"编号": fid, "原态度": att or "(空)", "终态": final,
                     "降级原因": reasons, "证据摘录长度": len(excerpt.strip())})

    counts = {}
    for r in rows:
        counts[r["终态"]] = counts.get(r["终态"], 0) + 1

    print(f"[完成] 复核 {len(rows)} 条：终态分布 {counts}")
    print(f"       降级 {len(downgraded)} 条")
    for d in downgraded:
        print(f"       [降级] 疑点 {d['编号']}：{d['原态度']} → 无法判断")
        for r in d["原因"]:
            print(f"                └ {r}")
    for n in notes:
        print(f"       [{n[:2]}] {n[3:].strip()}")
    if downgraded:
        print("       被降级的条目在登记册复核列标注「已降级」，并按其原疑点复算结果定关注度。")

    if args.out:
        L = ["# 复核输出校验报告", "",
             f"- 输入：`{args.review_json}`",
             f"- 复核条数：{len(rows)}；终态分布：{counts}；降级 {len(downgraded)} 条", "",
             "| 编号 | 原态度 | 终态 | 摘录长度 | 降级原因 |",
             "|---|---|---|---|---|"]
        for r in rows:
            L.append(f"| {r['编号']} | {r['原态度']} | **{r['终态']}** | {r['证据摘录长度']} | "
                     f"{'；'.join(r['降级原因']) or '—'} |")
        if notes:
            L += ["", "## 提示", ""] + [f"- {n[3:].strip()}" for n in notes]
        L += ["", "> 降级规则：R1 缺证据摘录 / R2 摘录非证据原文 / R3 复核理由含算术痕迹。",
              "> 原则：宁可降级为「无法判断」让人工回原文，也不要把模型编出来的「支持」送进登记册。"]
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
        print(f"       报告：{args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
