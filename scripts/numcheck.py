#!/usr/bin/env python3
"""numcheck.py — 尽调数字确定性复算（python3 scripts/numcheck.py <screen_result.json> <报告输出.md>）

四项检查，全部为纯确定性计算，不调用任何模型：
  1. 同比变动（yoy）        —— 同一科目跨期增长率
  2. 应收/收入增速背离      —— 应收账款增速 - 营业收入增速 > 阈值
  3. 经营现金流/净利润背离   —— 比值落在 [下限, 上限] 区间外
  4. 资产负债表勾稽          —— 资产 ≈ 负债 + 所有者权益

输入为 Phase 1 的 screen_result.json（含 numbers 数字抽取表与 flags 红旗清单）。
输出：Markdown 复算报告（PASS/FAIL 逐项）+ 同名 .summary.json（供 Phase 3/4 汇总用）。

阈值全部集中在下面的【可调参数】区，每项有中文注释，按你的口径自行修改。
"""
import json
import os
import re
import sys

# ===================== 可调参数（按你的尽调口径自行修改） =====================
AR_REV_GAP_PCT = 20.0        # 背离判定：应收账款增速 - 营业收入增速，超过该百分点数判 FAIL
OCF_NP_LOW = 0.5             # 现金流/净利润比值下界，低于判 FAIL
OCF_NP_HIGH = 2.0            # 现金流/净利润比值上界，高于判 FAIL
BALANCE_TOL_PCT = 0.5        # 勾稽容差：|资产-负债-权益|/资产 的百分比，超过判 FAIL
YOY_SPIKE_PCT = 50.0         # 同比变动超过该百分比判 WARN（提示关注，不直接判 FAIL）
# ============================================================================

UNIT_TO_YUAN = {
    "元": 1.0, "千元": 1e3, "万元": 1e4, "十万": 1e5, "百万": 1e6, "亿元": 1e8,
}

# 数字 + 常见千分位/单位写法。用于从模型漏抽的原文引用里兜底补出基期数字。
NUM_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\d])")

# 科目别名归并：模型抽取口径不一（"营业收入" vs "营业总收入"），归到同一口径再参与勾稽
SUBJECT_ALIASES = {
    "营业收入": "revenue", "营业总收入": "revenue", "主营业务收入": "revenue", "收入": "revenue",
    "净利润": "net_profit", "归母净利润": "net_profit", "净利润(归母)": "net_profit",
    "归属于母公司股东的净利润": "net_profit", "利润总额": "net_profit",
    "经营活动产生的现金流量净额": "ocf", "经营活动现金流量净额": "ocf", "经营现金流净额": "ocf",
    "应收账款": "ar", "应收帐款": "ar",
    "资产总计": "assets", "资产总额": "assets", "总资产": "assets",
    "负债合计": "liabilities", "负债总额": "liabilities", "总负债": "liabilities",
    "所有者权益合计": "equity", "股东权益合计": "equity", "净资产": "equity",
    "归属于母公司所有者权益合计": "equity",
}

YEAR_RE = re.compile(r"(20\d{2})")


def canon_subject(s):
    s = (s or "").strip()
    for k, v in SUBJECT_ALIASES.items():
        if k in s:
            return v
    return s or "未命名科目"


def parse_period(s):
    """从期间字符串里取四位年份；取不到返回 None。"""
    if not s:
        return None
    m = YEAR_RE.findall(str(s))
    return int(m[-1]) if m else None


def to_yuan(value, unit):
    f = UNIT_TO_YUAN.get((unit or "").strip(), None)
    if f is None:
        return None
    return float(value) * f


def fmt(v):
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e8:
        return f"{v/1e8:,.4f} 亿元"
    if a >= 1e4:
        return f"{v/1e4:,.2f} 万元"
    return f"{v:,.2f} 元"


def pct(cur, base):
    if base in (None, 0) or cur is None:
        return None
    return (cur / base - 1.0) * 100.0


class Report:
    def __init__(self):
        self.items = []   # {检查项, 科目, 期间, 结果(PASS/FAIL/WARN/SKIP), 说明, 红旗候选(bool)}

    def add(self, name, subject, period, verdict, detail, is_flag=False):
        self.items.append({
            "检查项": name, "科目": subject, "期间": str(period),
            "结果": verdict, "说明": detail, "红旗候选": bool(is_flag),
        })


def check_yoy(data, rep):
    for subj in sorted(data):
        pts = sorted(data[subj], key=lambda x: x["year"])
        for a, b in zip(pts, pts[1:]):
            g = pct(b["yuan"], a["yuan"])
            if g is None:
                rep.add("同比变动", subj, f"{a['year']}→{b['year']}", "SKIP", "基期缺失或为 0，无法计算")
                continue
            unit_note = "" if a["unit"] == b["unit"] else f"（注意：两期单位不同 {a['unit']}/{b['unit']}，已归并到元计算）"
            if abs(g) >= YOY_SPIKE_PCT:
                rep.add("同比变动", subj, f"{a['year']}→{b['year']}", "WARN",
                        f"{fmt(a['yuan'])} → {fmt(b['yuan'])}，同比 {g:+.1f}%（超过 {YOY_SPIKE_PCT:.0f}% 关注线）{unit_note}",
                        is_flag=True)
            else:
                rep.add("同比变动", subj, f"{a['year']}→{b['year']}", "PASS",
                        f"{fmt(a['yuan'])} → {fmt(b['yuan'])}，同比 {g:+.1f}%{unit_note}")


def check_ar_vs_revenue(data, rep):
    if "revenue" not in data or "ar" not in data or not data["revenue"] or not data["ar"]:
        rep.add("应收/收入增速背离", "应收账款 vs 营业收入", "—", "SKIP", "缺收入或应收账款数据")
        return
    rev = sorted(data["revenue"], key=lambda x: x["year"])
    ar = sorted(data["ar"], key=lambda x: x["year"])
    if len(rev) < 2 or len(ar) < 2:
        rep.add("应收/收入增速背离", "应收账款 vs 营业收入", f"{rev[-1]['year']}", "SKIP",
                "需两期及以上的收入与应收账款数据")
        return
    r = pct(rev[-1]["yuan"], rev[0]["yuan"])
    a = pct(ar[-1]["yuan"], ar[0]["yuan"])
    if r is None or a is None:
        rep.add("应收/收入增速背离", "应收账款 vs 营业收入", "—", "SKIP", "基期数据缺失或为 0")
        return
    gap = a - r
    span = f"{rev[0]['year']}→{rev[-1]['year']}"
    detail = (f"收入增速 {r:+.1f}%（{fmt(rev[0]['yuan'])}→{fmt(rev[-1]['yuan'])}），"
              f"应收账款增速 {a:+.1f}%（{fmt(ar[0]['yuan'])}→{fmt(ar[-1]['yuan'])}），"
              f"差额 {gap:+.1f} 个百分点（阈值 {AR_REV_GAP_PCT:.0f}）")
    if gap > AR_REV_GAP_PCT:
        rep.add("应收/收入增速背离", "应收账款 vs 营业收入", span, "FAIL", detail + " → 背离", is_flag=True)
    else:
        rep.add("应收/收入增速背离", "应收账款 vs 营业收入", span, "PASS", detail)


def check_ocf_vs_np(data, rep):
    for subj, label in (("ocf", "经营现金流/净利润"),):
        need = ("ocf", "net_profit")
        if any(s not in data or not data[s] for s in need):
            rep.add(label, "经营现金流 vs 净利润", "—", "SKIP", "缺经营现金流或净利润数据")
            continue
        ocf = sorted(data["ocf"], key=lambda x: x["year"])
        np_ = sorted(data["net_profit"], key=lambda x: x["year"])
        latest = max(ocf[-1]["year"], np_[-1]["year"])
        o, n = ocf[-1]["yuan"], np_[-1]["yuan"]
        if n == 0:
            rep.add(label, "经营现金流 vs 净利润", f"{latest}", "SKIP", "净利润为 0，比值无意义")
            continue
        ratio = o / n
        detail = (f"{latest} 年经营现金流净额 {fmt(o)}，净利润 {fmt(n)}，比值 {ratio:.2f}"
                  f"（合理区间 [{OCF_NP_LOW}, {OCF_NP_HIGH}]）")
        if ratio < OCF_NP_LOW or ratio > OCF_NP_HIGH:
            rep.add(label, "经营现金流 vs 净利润", f"{latest}", "FAIL", detail + " → 背离", is_flag=True)
        else:
            rep.add(label, "经营现金流 vs 净利润", f"{latest}", "PASS", detail)


def check_balance(data, rep):
    need = ("assets", "liabilities", "equity")
    missing = [s for s in need if s not in data or not data[s]]
    if missing:
        rep.add("资产负债表勾稽", "资产/负债/权益", "—", "SKIP", "缺：" + "、".join(missing))
        return
    periods = []
    for s in need:
        periods.append({p["year"] for p in data[s]})
    common = set.intersection(*periods)
    if not common:
        rep.add("资产负债表勾稽", "资产/负债/权益", "—", "SKIP", "资产/负债/权益没有共同会计期间，无法勾稽")
        return
    for y in sorted(common):
        a = next(p["yuan"] for p in data["assets"] if p["year"] == y)
        l = next(p["yuan"] for p in data["liabilities"] if p["year"] == y)
        e = next(p["yuan"] for p in data["equity"] if p["year"] == y)
        if a == 0:
            rep.add("资产负债表勾稽", "资产/负债/权益", f"{y}", "SKIP", "资产总计为 0，无法计算容差")
            continue
        diff = a - l - e
        tol = abs(a) * BALANCE_TOL_PCT / 100.0
        detail = (f"{y} 年：资产 {fmt(a)}，负债 {fmt(l)}，权益 {fmt(e)}，"
                  f"差额 {fmt(diff)}（容差 {fmt(tol)}，即 {BALANCE_TOL_PCT}%）")
        if abs(diff) > tol:
            rep.add("资产负债表勾稽", "资产/负债/权益", f"{y}", "FAIL", detail + " → 勾稽不平", is_flag=True)
        else:
            rep.add("资产负债表勾稽", "资产/负债/权益", f"{y}", "PASS", detail)


def build(numbers):
    """按 科目→[期间点] 归并。同一科目同一期间多条时保留第一条并记录冲突。"""
    data, dupes = {}, []
    for i, n in enumerate(numbers):
        subj = canon_subject(n.get("科目"))
        year = parse_period(n.get("期间"))
        yuan = to_yuan(n.get("值"), n.get("单位"))
        if year is None or yuan is None:
            dupes.append(f"第 {i+1} 条（{n.get('科目')}）：期间或单位无法解析，已跳过（期间={n.get('期间')!r} 单位={n.get('单位')!r}）")
            continue
        pts = data.setdefault(subj, [])
        if any(p["year"] == year for p in pts):
            first = next(p for p in pts if p["year"] == year)
            same = abs(first["yuan"] - yuan) < 1e-9
            dupes.append(
                f"「{subj}」{year} 年出现多条抽取值：{fmt(first['yuan'])} vs {fmt(yuan)}"
                + ("（一致，已去重）" if same else "（不一致！以原文字面靠前者为准，此冲突需人工核对）")
            )
            if same:
                continue
        pts.append({"year": year, "yuan": yuan, "unit": n.get("单位"), "raw": n.get("值")})
    return data, dupes


# 基期补抽：模型只抽了本期、漏掉上期时，增速类检查会整项 SKIP。
# 那是静默漏报——材料里明明写了"较期初20,000万元"，报告却说"数据不足"。
BASE_YEAR_HINTS = ("较期初", "期初", "上年同期", "同比", "较年初", "上期", "期初余额", "上年末")


def backfill_base_years(numbers, data, dupes):
    """从漏抽条目的原文引用里，把"较期初X万元"这类基期数字补回 data。

    只补"科目已有本期、缺唯一基期"的情形；补入的一律在 dupes 里留痕，
    供 Phase 4 人工核对——补抽值没有独立的期间标注，可信度低于正式抽取值。
    """
    added = []
    for n in numbers:
        subj = canon_subject(n.get("科目"))
        if subj not in data or len(data[subj]) != 1:
            continue  # 只处理"该科目只有一期"的情况
        quote = n.get("原文引用")
        if not isinstance(quote, str) or not any(h in quote for h in BASE_YEAR_HINTS):
            continue
        cur = data[subj][0]
        base_year = cur["year"] - 1
        if not (2000 <= base_year <= 2100):
            continue
        for m in NUM_RE.finditer(quote):
            token = m.group(1)
            if _same_number(token, cur["raw"]):
                continue  # 跳过本期值本身
            if _is_year(quote, m):
                continue  # 跳过年份："2024年度"的 2024 不是基期金额（实测踩过这个坑）
            if not _follows_base_hint(quote, m):
                continue  # 只认紧跟"较期初/期初"等基期提示词的数字，
                          # 否则"同比增长5.0%"里的 5.0 会被误当基期收入（实测踩过）
            yuan = to_yuan(float(token.replace(",", "")), n.get("单位"))
            if yuan is None:
                continue
            if not _plausible_base(cur["yuan"], yuan):
                continue  # 与本期不同量级 → 抓到的是百分比/增长率，不是基期金额
            data[subj].append({"year": base_year, "yuan": yuan, "unit": n.get("单位"),
                               "raw": token, "backfilled": True})
            added.append((subj, base_year, yuan, quote))
            break
    for subj, y, v, q in added:
        dupes.append(f"「{subj}」{y} 年基期由原文引用兜底补抽：{fmt(v)}"
                     f"（模型只抽了本期；基期取自引用片段：…{q[:40]}…）"
                     "——该值未经模型独立标注期间，已参与计算，请人工核对")
    return added


def _same_number(token, raw):
    try:
        return abs(float(token.replace(",", "")) - float(raw)) < 1e-9
    except (TypeError, ValueError):
        return False


def _is_year(quote, match):
    """判断匹配到的是年份而不是金额。

    判据：四位数字落在合理会计年度区间（1990-2100），且紧邻"年"字
    （"2024年度" / "较2023年"）。误把年份当金额会算出 +5087.7% 这类荒谬增速。
    """
    token = match.group(1)
    if not re.fullmatch(r"\d{4}", token) or not (1990 <= int(token) <= 2100):
        return False
    before = quote[max(0, match.start(1) - 1):match.start(1)]
    after = quote[match.end(1):match.end(1) + 1]
    return "年" in (before + after)


def _follows_base_hint(quote, match, span=6):
    """判断数字前 span 个字符内是否出现基期提示词（较期初/期初/上年同期…）。

    span 取 6 是因为"上年同期为100,000万元"这类句式中，提示词与数字之间
    还隔着"为"这样的动词，span=4 会漏掉（实测踩过）。
    """
    window = quote[max(0, match.start(1) - span):match.start(1)]
    return any(h in window for h in BASE_YEAR_HINTS)


def _plausible_base(cur_yuan, base_yuan):
    """基期金额与本期是否同量级（0.2x ~ 5x）。

    "同比增长5.0%"里的 5.0 与 10.5 亿元的营收差 7 个数量级，一眼可辨。
    没有这道校验，报告会输出一个 "+2099900%" 的假背离 FAIL——
    那比 SKIP 危险得多，因为人工复核会以为自己真的算出了背离。
    """
    if cur_yuan == 0:
        return base_yuan == 0
    r = base_yuan / cur_yuan
    return 0.2 <= r <= 5.0


def render(rep, data, dupes, src, backfilled=None):
    backfilled = backfilled or []
    counts = {}
    for it in rep.items:
        counts[it["结果"]] = counts.get(it["结果"], 0) + 1
    L = []
    L.append("# 数字确定性复算报告\n")
    L.append(f"- 输入：`{src}`")
    L.append(f"- 口径：应收/收入增速背离阈值 {AR_REV_GAP_PCT:.0f} 个百分点；"
             f"现金流/净利润区间 [{OCF_NP_LOW}, {OCF_NP_HIGH}]；勾稽容差 {BALANCE_TOL_PCT}%")
    L.append(f"- 结论：PASS {counts.get('PASS',0)} / FAIL {counts.get('FAIL',0)} / "
             f"WARN {counts.get('WARN',0)} / SKIP {counts.get('SKIP',0)}")
    if backfilled:
        L.append(f"- 基期兜底补抽 {len(backfilled)} 项（标记为 ⚠补抽）："
                 "模型只抽了本期，基期数字从原文引用片段提取，请优先人工核对。")
    L.append(f"- FAIL 与 WARN 项已列为红旗候选，由 Phase 4 并入红旗登记册。\n")
    L.append("| 检查项 | 科目 | 期间 | 结果 | 说明 |")
    L.append("|---|---|---|---|---|")
    for it in sorted(rep.items, key=lambda x: ({"FAIL": 0, "WARN": 1, "PASS": 2, "SKIP": 3}[x["结果"]], x["检查项"])):
        L.append(f"| {it['检查项']} | {it['科目']} | {it['期间']} | **{it['结果']}** | {it['说明']} |")
    if dupes:
        L.append("\n## 抽取层面的数据问题（非计算，但需人工核对）\n")
        for d in dupes:
            L.append(f"- {d}")
    L.append("\n## 参与计算的抽取值（归并后）\n")
    L.append("| 科目 | 年度 | 抽取原值 | 单位 | 归并为元 |")
    L.append("|---|---|---|---|---|")
    for subj in sorted(data):
        for p in sorted(data[subj], key=lambda x: x["year"]):
            mark = " ⚠补抽" if p.get("backfilled") else ""
            L.append(f"| {subj} | {p['year']}{mark} | {p['raw']} | {p['unit']} | {fmt(p['yuan'])} |")
    L.append("\n> 本报告全部数字由 `numcheck.py` 确定性计算，未使用任何模型估算。")
    return "\n".join(L) + "\n", counts


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--selftest":
        return selftest()
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    src, out = sys.argv[1], sys.argv[2]
    if not os.path.isfile(src):
        sys.exit(f"[拒绝] 输入文件不存在：{src}")
    with open(src, "r", encoding="utf-8") as f:
        obj = json.load(f)
    numbers = obj.get("numbers")
    if not isinstance(numbers, list):
        sys.exit("[拒绝] screen_result.json 缺少 numbers 数组（结构不完整），按 SKILL.md 停止。")

    data, dupes = build(numbers)
    backfilled = backfill_base_years(numbers, data, dupes)
    rep = Report()
    check_yoy(data, rep)
    check_ar_vs_revenue(data, rep)
    check_ocf_vs_np(data, rep)
    check_balance(data, rep)
    md, counts = render(rep, data, dupes, src, backfilled)

    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    summary = {
        "counts": counts,
        "fail_items": [i for i in rep.items if i["结果"] == "FAIL"],
        "warn_items": [i for i in rep.items if i["结果"] == "WARN"],
        "dup_issues": dupes,
        "thresholds": {"ar_rev_gap_pct": AR_REV_GAP_PCT, "ocf_np": [OCF_NP_LOW, OCF_NP_HIGH],
                       "balance_tol_pct": BALANCE_TOL_PCT, "yoy_spike_pct": YOY_SPIKE_PCT},
    }
    with open(os.path.splitext(out)[0] + ".summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"[完成] {out}")
    print(f"       PASS={counts.get('PASS',0)} FAIL={counts.get('FAIL',0)} "
          f"WARN={counts.get('WARN',0)} SKIP={counts.get('SKIP',0)}")
    for i in rep.items:
        if i["结果"] in ("FAIL", "WARN"):
            print(f"       {i['结果']} | {i['检查项']} | {i['科目']} | {i['期间']} | {i['说明']}")
    return 0


# --------------------------- 自测（不依赖 Ollama，可随时重跑） ---------------------------
DEMO = {
    "numbers": [
        {"科目": "营业收入", "期间": "2023", "值": 100000.0, "单位": "万元"},
        {"科目": "营业收入", "期间": "2024", "值": 105000.0, "单位": "万元"},   # 收入 +5%
        {"科目": "应收账款", "期间": "2023", "值": 20000.0, "单位": "万元"},
        {"科目": "应收账款", "期间": "2024", "值": 32000.0, "单位": "万元"},    # 应收 +60% → 背离 FAIL
        {"科目": "净利润", "期间": "2024", "值": 8000.0, "单位": "万元"},
        {"科目": "经营活动产生的现金流量净额", "期间": "2024", "值": 1200.0, "单位": "万元"},  # 0.15 → FAIL
        {"科目": "资产总计", "期间": "2024", "值": 60000.0, "单位": "万元"},
        {"科目": "负债合计", "期间": "2024", "值": 30000.0, "单位": "万元"},
        {"科目": "所有者权益合计", "期间": "2024", "值": 29000.0, "单位": "万元"},  # 差 1000 万 → 勾稽 FAIL
    ],
    "flags": [{"编号": 1, "科目": "应收账款", "疑点": "演示"}],
}


def selftest():
    # 场景 A：完整两期数据 → 应收/收入背离必须 FAIL
    data, dupes = build(DEMO["numbers"])
    rep = Report()
    check_yoy(data, rep)
    check_ar_vs_revenue(data, rep)
    check_ocf_vs_np(data, rep)
    check_balance(data, rep)
    fails = {i["检查项"] for i in rep.items if i["结果"] == "FAIL"}
    expect = {"应收/收入增速背离", "经营现金流/净利润", "资产负债表勾稽"}
    print("[selftest A] FAIL 项：", sorted(fails))
    if not expect.issubset(fails):
        print("[selftest A] 未捕获预期背离：", sorted(expect - fails))
        return 1
    if counts_of(rep).get("PASS", 0) == 0:
        print("[selftest A] 没有任何 PASS 项，疑似阈值或数据异常")
        return 1

    # 场景 B：模型只抽了本期（实测 Saluki 的真实行为），基期只写在句子里没抽
    # → 基期兜底补抽必须让背离检查重新可判，而不是整项 SKIP
    demo_b = {
        "numbers": [
            {"科目": "营业收入", "期间": "2024", "值": 105000.0, "单位": "万元",
             "原文引用": "2024年度实现营业收入105,000万元，上年同期为100,000万元，同比增长5.0%。"},
            {"科目": "应收账款", "期间": "2024", "值": 32000.0, "单位": "万元",
             "原文引用": "应收账款期末余额32,000万元，较期初20,000万元增长60.0%，主要系回款周期延长所致。"},
        ],
        "flags": [],
    }
    data_b, dupes_b = build(demo_b["numbers"])
    rep_b = Report()
    check_ar_vs_revenue(data_b, rep_b)
    before = {i["结果"] for i in rep_b.items}
    print("[selftest B] 补抽前应收/收入增速背离判定：", before, "（预期 SKIP：只有一期收入）")
    if before != {"SKIP"}:
        print("[selftest B] 补抽前判定异常，应因缺基期而 SKIP，实际：", before)
        return 1
    backfilled = backfill_base_years(demo_b["numbers"], data_b, dupes_b)
    rep_b2 = Report()
    check_ar_vs_revenue(data_b, rep_b2)
    v2 = [(i["结果"], i["说明"]) for i in rep_b2.items]
    print("[selftest B] 补抽后：", v2)
    if not backfilled:
        print("[selftest B] 未补抽任何基期，补抽逻辑失效")
        return 1
    if any(i["结果"] == "SKIP" for i in rep_b2.items):
        print("[selftest B] 补抽后仍 SKIP，补抽未生效")
        return 1
    if not any(i["结果"] == "FAIL" for i in rep_b2.items):
        print("[selftest B] 补抽后应收增速 +60% vs 收入 +5% 未判 FAIL，背离逻辑失效")
        return 1
    if not dupes_b:
        print("[selftest B] 补抽未在报告中留痕，人工核对断链")
        return 1

    md, counts = render(rep, data, dupes, "DEMO", backfilled)
    print(f"[selftest] 汇总 PASS={counts.get('PASS',0)} FAIL={counts.get('FAIL',0)} "
          f"WARN={counts.get('WARN',0)} SKIP={counts.get('SKIP',0)}")
    print("\n[selftest] 通过：A) 故意构造的两处背离 + 勾稽不平均被判 FAIL，正常项判 PASS；"
          "\n             B) 模型漏抽基期时兜底补抽生效，背离检查不再静默 SKIP，且补抽值在报告中留痕。")
    return 0


def counts_of(rep):
    c = {}
    for it in rep.items:
        c[it["结果"]] = c.get(it["结果"], 0) + 1
    return c


if __name__ == "__main__":
    sys.exit(main())
