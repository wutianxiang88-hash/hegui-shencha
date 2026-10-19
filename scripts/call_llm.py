#!/usr/bin/env python3
"""call_llm.py — 调用本地 Ollama 模型的 JSON 模式推理。

用法:
    python3 call_llm.py <模型名> <提示词文件> <输入路径> <输出JSON文件> [选项]

    输入路径可以是目录（Phase 1：拼接该目录下全部 .txt）或单个文件（Phase 3：复核输入）。

选项:
    --num-ctx N     上下文窗口（默认 32768）。估算 token 超过该值 90% 时拒绝执行。
    --num-predict N 输出 token 上限（默认 12288）。防止输出失控把生成段拖进超时。
    --timeout N     单次调用总时限秒数（默认 1800）。超时【不重试】，直接报错并建议降批。
    --stall-timeout N  底层读超时秒数（默认 480）。prompt eval 期间服务端不吐数据，
                        大 prompt 会静默数分钟，别调太小误判挂起。
    --temperature T 温度（默认 0.1，初筛/复核都要求低随机性）
    --retries N     JSON 解析失败后的重试次数（默认 1，仅用于解析失败，不用于超时）
    --ollama URL    Ollama 服务地址（默认 http://localhost:11434，或读环境变量 OLLAMA_HOST）
    --no-think     关闭模型思考模式（Phase 1 / Phase 3 均应加上）
    --presence-penalty P  存在惩罚（默认 0，见下方"已知坑"）

已知坑（qwen3.5:9b-q8_0，实测 2026-10-18）:
   该模型 Modelfile 默认 presence_penalty=1.5。与较长 prompt 组合时模型会陷入
   重复循环，Ollama 主动中止并返回 HTTP 500 "token repeat limit reached"。
   本脚本默认覆盖为 presence_penalty=0，实测可消除该报错，同时把复核耗时
   从 679 秒（开思考）降到约 50 秒。若你换用其他模型且不需要覆盖，传 --presence-penalty -1 可恢复模型默认值。

2026-10-18 改造（当天 5×2h 全灭换来的教训）:
   1. 超时从 7200s 降到 1800s，且超时【禁止原参数重试】——同一参数重试只会重复同一个错误。
   2. stream=True 流式读取：stderr 每 1000 token 打一行进度（只打计数与速率，不打正文），
      停滞超过 --stall-timeout 秒立即判挂起，不再空烧两小时才发现。
   3. 调用前先 GET /api/tags 健康检查，服务不可达 10 秒内退出。
   4. 输出加 --num-predict 上限，done_reason=length 时明确报错（此前会静默等到超时）。

设计约束（对应 hegui-shencha SKILL.md 硬规则）:
  1. 上下文超限时【直接拒绝退出】，而不是让 Ollama 静默截断。
     静默截断比报错更危险：模型没读到的部分会被当成"材料里没有"，红旗凭空消失。
  2. 输出必须是可解析 JSON。解析失败自动重试，仍失败则明确报错退出——
     绝不把残缺结果写成"成功"，绝不自行补全模型输出。
  3. 遇到 "token repeat limit" 类 500 时明确报告并建议降低 presence_penalty，
     不静默重试同一组参数（同一参数重试只会重复同一个错误）。
  4. stdout/stderr 只打印元数据（模型名、文件数、token 估算、耗时、速率、顶层键），
     绝不回显提示词与模型回复正文——正文可能含尽调材料原文。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_NUM_CTX = 32768


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：中日韩字符按 1 token/字，其余按 1 token/3.5 字符。

    故意偏保守（宁多勿少）：宁可因为高估而拒绝一次，也不要低估后静默截断。
    """
    cjk = 0
    for ch in text:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF:
            cjk += 1
    other = len(text) - cjk
    return int(cjk + other / 3.5)


def load_inputs(input_path: str):
    """读取输入：目录 → 全部 .txt（按文件名排序拼接）；文件 → 原文。"""
    if os.path.isdir(input_path):
        names = sorted(n for n in os.listdir(input_path) if n.lower().endswith(".txt"))
        if not names:
            sys.exit(f"[拒绝] 目录 {input_path} 下没有 .txt 文本文件。先完成 Phase 0 材料入库。")
        parts, loaded = [], []
        for n in names:
            p = os.path.join(input_path, n)
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                parts.append(f.read())
            loaded.append(n)
        return "\n\n".join(parts), loaded
    if os.path.isfile(input_path):
        with open(input_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(), [os.path.basename(input_path)]
    sys.exit(f"[拒绝] 输入路径不存在：{input_path}")


class CallTimeout(Exception):
    """总时限或停滞超时——这类错误禁止原参数重试。"""


def health_check(base_url):
    req = urllib.request.Request(base_url.rstrip("/") + "/api/tags", method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def call_ollama(base_url, model, prompt, num_ctx, num_predict, temperature,
                timeout, stall_timeout, presence_penalty=0, think=None):
    """流式调用 /api/generate。返回最终 done 帧（含 response 全文与计数）。

    - 总时限 timeout：从发请求到收完最后一个 token 的整个窗口。
    - 停滞 stall_timeout：底层 socket 读超时（两次收到新数据之间的最大间隔）。
    进度只往 stderr 打 token 计数与速率，不打正文。
    """
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "format": "json",
        "options": {"temperature": temperature, "num_ctx": num_ctx, "num_predict": num_predict},
    }
    if presence_penalty >= 0:
        payload["options"]["presence_penalty"] = presence_penalty
    if think is not None:
        payload["think"] = think
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.time()
    deadline = started + timeout
    next_report = 1000
    chunks = []
    final = None
    with urllib.request.urlopen(req, timeout=stall_timeout) as resp:
        for raw_line in resp:
            now = time.time()
            if now > deadline:
                raise CallTimeout(
                    f"总时限 {timeout}s 耗尽（已生成 {sum(len(c) for c in chunks)} 字符）")
            line = raw_line.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            chunks.append(frame.get("response", ""))
            nchars = sum(len(c) for c in chunks)
            if frame.get("done"):
                final = frame
                break
            if nchars >= next_report:
                dt = now - started
                rate = nchars / dt if dt > 0 else 0
                print(f"[进度] 已收 {nchars} 字符，{dt:.0f}s，{rate:.1f} 字符/s", file=sys.stderr)
                next_report = (nchars // 2000 + 1) * 2000
    if final is None:
        raise CallTimeout(f"响应流提前结束（停滞超 {stall_timeout}s 或连接中断）")
    final["response"] = "".join(chunks)
    return final


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("model")
    ap.add_argument("prompt_file")
    ap.add_argument("input_path")
    ap.add_argument("output_json")
    ap.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX)
    ap.add_argument("--num-predict", type=int, default=12288)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--stall-timeout", type=int, default=480,
                    help="底层读超时（秒）。prompt eval 期间服务端本就不发数据，"
                         "大 prompt 可能静默数分钟，故默认 480 而不是 120。")
    ap.add_argument("--temperature", type=float, default=0.1)
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("--ollama", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    ap.add_argument("--no-think", action="store_true", help="关闭模型思考模式（抽取/复核任务建议加上）")
    ap.add_argument("--presence-penalty", type=float, default=0,
                    help="存在惩罚，默认 0。传 -1 表示用模型自身默认值。")
    args = ap.parse_args()

    if not os.path.isfile(args.prompt_file):
        sys.exit(f"[拒绝] 提示词文件不存在：{args.prompt_file}")

    with open(args.prompt_file, "r", encoding="utf-8") as f:
        prompt_template = f.read()
    material, loaded = load_inputs(args.input_path)
    prompt = prompt_template + "\n\n# 以下为待审材料\n\n" + material

    est = estimate_tokens(prompt)
    if est > args.num_ctx * 0.9:
        sys.exit(
            "[拒绝] 上下文超限：估算约 {est} token，窗口 {ctx}（上限 {lim}）。\n"
            "      已加载 {n} 个输入文件。请拆分材料分批处理，或用 --num-ctx 调大窗口后重跑。\n"
            "      （本脚本不做静默截断：截断会让模型漏读材料，红旗凭空消失。）".format(
                est=est, ctx=args.num_ctx, lim=int(args.num_ctx * 0.9), n=len(loaded)
            )
        )

    try:
        health_check(args.ollama)
    except Exception as e:
        sys.exit(f"[失败] Ollama 健康检查未通过（{args.ollama}）：{type(e).__name__}: {e}\n"
                 "       请先确认 `ollama serve` 在运行，且模型已导入（ollama list）。")

    last_err = None
    for attempt in range(1, args.retries + 2):
        started = time.time()
        try:
            raw = call_ollama(
                args.ollama, args.model, prompt, args.num_ctx, args.num_predict,
                args.temperature, args.timeout, args.stall_timeout,
                presence_penalty=args.presence_penalty,
                think=(False if args.no_think else None),
            )
        except CallTimeout as e:
            sys.exit(
                f"[失败] 调用超时：{e}。\n"
                "       按 2026-10-18 教训：不要原参数重试（只会重复同一个错误）。\n"
                "       请减小批次（减少输入文件数）、调低 --num-ctx，或确认 KV/内存配置后重跑。"
            )
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if "repeat limit" in body or "prediction aborted" in body:
                sys.exit(
                    "[拒绝] Ollama 报 token repeat limit（HTTP {c}：{b}）。\n"
                    "       这通常是 presence_penalty 偏高导致模型陷入重复循环。\n"
                    "       本脚本默认已传 presence_penalty=0；若你显式覆盖过，请改回 0 或去掉 --presence-penalty。\n"
                    "       不要用同一组参数反复重试——只会重复同一个错误。".format(c=e.code, b=body[:200])
                )
            sys.exit(f"[失败] Ollama HTTP {e.code}：{body[:300] or e}")
        except urllib.error.URLError as e:
            sys.exit(f"[失败] 连不上 Ollama（{args.ollama}）：{e}\n       请先确认 `ollama serve` 在运行，且模型已导入（ollama list）。")
        except Exception as e:  # 停滞、连接中断、JSON 流解析错误等
            last_err = f"{type(e).__name__}: {e}"
            print(f"[重试 {attempt}/{args.retries + 1}] 调用异常：{last_err}", file=sys.stderr)
            continue

        if raw.get("done_reason") == "length":
            sys.exit(
                "[拒绝] 模型输出达到 --num-predict 上限被截断（done_reason=length）。"
                "请调大 --num-predict 或减小批次后重跑；不要把截断的输出当成完整结果。"
            )

        pe = raw.get("prompt_eval_count")
        if pe is not None and pe >= args.num_ctx - 8:
            sys.exit(
                f"[拒绝] 疑似上下文被截断：prompt_eval_count={pe} 已贴近窗口 {args.num_ctx}。"
                "模型很可能没读完整份材料，结果不可信。请拆分材料或调大 --num-ctx。"
            )

        text = (raw.get("response") or "").strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            last_err = f"JSON 解析失败：{e}"
            print(f"[重试 {attempt}/{args.retries + 1}] {last_err}", file=sys.stderr)
            continue

        if not isinstance(parsed, dict):
            last_err = f"顶层不是 JSON 对象，而是 {type(parsed).__name__}"
            print(f"[重试 {attempt}/{args.retries + 1}] {last_err}", file=sys.stderr)
            continue

        os.makedirs(os.path.dirname(os.path.abspath(args.output_json)) or ".", exist_ok=True)
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(parsed, f, ensure_ascii=False, indent=2)

        # 只回显元数据，不回显正文
        dt = time.time() - started
        ec = raw.get("eval_count", "?")
        rate = (ec / dt) if isinstance(ec, int) and dt > 0 else 0
        print("[成功] 模型={m} 输入文件={n} 估算token≈{est} 实际prompt_eval={pe} 生成={ec} 耗时={s:.0f}s（{r:.1f} tok/s）".format(
            m=args.model, n=len(loaded), est=est,
            pe=raw.get("prompt_eval_count", "?"), ec=ec, s=dt, r=rate,
        ))
        print("       输出={o} 顶层键={k}".format(o=args.output_json, k=sorted(parsed.keys())))
        print("       请人工核对顶层键是否包含预期的 flags / numbers；结构不完整按 SKILL.md 处理。")
        return 0

    sys.exit(f"[失败] 连续 {args.retries + 1} 次未拿到可解析 JSON（{last_err}）。\n"
             f"       按 SKILL.md：记录\"初筛失败\"并停止，不要自行编造结果。")


if __name__ == "__main__":
    sys.exit(main())
