#!/usr/bin/env python3
"""统一 LLM 入口：分层路由 + 失败轮换 + 自动记账 + 预算闸门。

用法：
  tools/llm.py --tier free "问题"          # zen 免费池（¥0）——默认优先用这个
  echo "长文" | tools/llm.py --tier free --system "你是分析员"
  tools/llm.py --tier cheap --json "问题"  # 便宜付费档，附 usage
  tools/llm.py --tier hard  "难问题"
  tools/llm.py --tier local "问题"         # 本地 llama-server(:8080)
  tools/llm.py --list                      # 看各档模型与单价
  tools/llm.py --probe                     # 触发一次可用性探测

设计原则：
  1. 免费优先，付费要么便宜要么明确需要。
  2. 预算闸门：本月 api 支出 >= allowed → 拒绝（除非 --force）。
  3. 任何调用都记账到 ledger.csv（category=api）。
坑：opencode 端点必须带 User-Agent + x-opencode-session，否则 403/400。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

UA = "pi (linux; x64)"
SESS_FILE = common.ROOT / "state" / "llm_session.json"


def session_id():
    """每天一个稳定 session id：利于端点侧路由/缓存"""
    try:
        d = json.load(open(SESS_FILE))
        if d.get("day") == time.strftime("%Y-%m-%d"):
            return d["sid"]
    except Exception:
        pass
    sid = str(uuid.uuid4())
    SESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    json.dump({"day": time.strftime("%Y-%m-%d"), "sid": sid}, open(SESS_FILE, "w"))
    return sid


def load_tiers():
    return common.cfg("llm", {}).get("tiers", {})


def call(entry, messages, max_tokens, timeout, temperature, stream=False, extra=None):
    k = common.keys()
    key = k.get(entry.get("key", ""), "") or ""
    body = {"model": entry["model"], "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature}
    if stream:
        body["stream"] = True
    if extra:
        body.update(extra)
    h = {"Content-Type": "application/json", "User-Agent": UA,
         "x-opencode-session": session_id()}
    if key:
        h["Authorization"] = "Bearer " + key
    req = urllib.request.Request(entry["base"].rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as f:
            return json.load(f)
    except urllib.error.HTTPError as e:
        # 坑（09-27 查到）：原来只让 HTTPError 往上抛，**错误 body 被吞掉**，
        # 表现就只剩一句「HTTP Error 400」——查不出是限流、参数不对、还是端点拒收。
        try:
            detail = e.read().decode(errors="replace")[:200]
        except Exception:
            detail = ""
        raise RuntimeError(f"HTTP {e.code} {detail or e.reason}") from None


def cost_cny(entry, usage, fx):
    cin = float(entry.get("in", 0) or 0)
    cout = float(entry.get("out", 0) or 0)
    p = usage.get("prompt_tokens", 0) or 0
    c = usage.get("completion_tokens", 0) or 0
    cached = (usage.get("prompt_cache_hit_tokens") or
              (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    miss = max(p - cached, 0)
    raw = miss * cin / 1e6 + cached * float(entry.get("cache", cin) or 0) / 1e6 + c * cout / 1e6
    if entry.get("currency") == "cny":
        return raw           # 已是人民币，别再乘汇率
    return raw * fx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?", default="")
    ap.add_argument("--tier", default="free", choices=["free", "cheap", "editor", "hard", "local", "persona"])
    ap.add_argument("--model", default=None, help="强制某模型 id（在档内匹配）")
    ap.add_argument("--system", default=None)
    ap.add_argument("--max-tokens", type=int, default=2048)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--json", action="store_true", help="输出原始 JSON（含 usage）")
    ap.add_argument("--persona", action="store_true",
                    help="加载 content/persona.json 作为 character_manifest（人设稳定）")
    ap.add_argument("--no-thinking", action="store_true", help="thinking.type=disabled（省成本提速）")
    ap.add_argument("--extra", default=None, help="额外请求体 JSON（合并进 body）")
    ap.add_argument("--force", action="store_true", help="忽略预算闸门")
    ap.add_argument("--task", default="",
                    help="任务名（polish/action/write/...）：用于任务级黑名单，"
                         "该任务已拉黑的模型会被从路由里剔除（tools/blacklist.py）")
    ap.add_argument("--fallback-tier", default="",
                    help="本档全挂时降级到哪一档（受 R2 单次/单日上限约束）")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    tiers = load_tiers()
    if args.list:
        e = common.cfg("econ", {})
        for t, entries in tiers.items():
            print(f"[{t}]")
            for x in entries:
                print(f"   {x['model']:34s} ${x.get('in')}/{x.get('out')} per M  {x['base']}")
        left, allowed, spent = common.budget()
        print(f"\n预算: 本月已花 ¥{spent} / 额度 ¥{allowed} (econ.fx={e.get('fx_usd_cny')})")
        return 0

    prompt = args.prompt or sys.stdin.read()
    if not prompt.strip():
        print("empty prompt", file=sys.stderr)
        return 2

    left, allowed, spent = common.budget()
    entries = tiers.get(args.tier, [])

    # ---- 任务级黑名单：先剔掉「这个任务上已拉黑」的模型（09-27 加）----
    if args.task:
        import blacklist
        entries, n_bl = blacklist.filter_entries(args.task, entries)
        if n_bl:
            print(f"[llm] 任务黑名单：{args.task} 剔掉 {n_bl} 个已拉黑模型"
                  f"（state/model_blacklist.json）", file=sys.stderr)
        if not entries:
            print(f"[llm] {args.task} 的 {args.tier} 档模型已全被拉黑 ⇒ 拒绝调用（不静默降级）",
                  file=sys.stderr)
            return 5

    paid = any(float(x.get("out", 0) or 0) > 0 for x in entries)
    if paid and left <= 0 and not args.force:
        print(f"[llm] 预算闸门：本月 api 已花 ¥{spent}/¥{allowed}，拒绝付费调用。"
              f"用 --tier free 或 --force（需理由）。", file=sys.stderr)
        return 3

    # ---- R2 clamp：免费层失效时的**单次**降级额度（09-27 加）----
    # R2 原文：「允许单次降级调用（单次 ≤¥0.10、当日 ≤¥0.30），且必须记入 ledger.csv」
    # 为什么要单独一个闸门：budget() 看的是**本月**额度用没用完（本月才花 31.9/60，
    # 闸门根本不会拦），但 R2 要限的是「单次」和「当日」两个更严的分母。
    # 口径：分母取**今日 api 类支出**，不是 spent_today()（那个把本机电费也算进来，
    # 会把「云调用降级额度」和本地开销混在一起，导致误拒）。两条分母别混。
    if paid and not args.force:
        cap_s = float(common.cfg("econ", {}).get("degrade_cap_single_cny", 0.10))
        cap_d = float(common.cfg("econ", {}).get("degrade_cap_daily_cny", 0.30))
        spent_d = common.spent_today_api()
        if spent_d >= cap_d:
            print(f"[llm] R2 单日降级闸门：今日 api 已 ¥{spent_d:.4f} ≥ ¥{cap_d}，拒绝付费调用。"
                  f"（--force 可越，需理由）", file=sys.stderr)
            return 7
        # 单次预估：输入未知（拿 prompt 长度粗估，中文 ~1.5 字/token，下限 1000），
        # 输出拿 --max-tokens 当上界 ⇒ 这是**保守高估**，宁可误拒不误放
        est_in = max(1000, (len(prompt_text := (args.prompt or "")) + len(args.system or "")) // 2)
        mx = max(float(x.get("in", 0) or 0) for x in entries)
        my = max(float(x.get("out", 0) or 0) or 0 for x in entries)
        fx0 = float(common.cfg("econ", {}).get("fx_usd_cny", 7.1))
        est = (est_in * mx + args.max_tokens * my) / 1e6 * fx0
        if est > cap_s:
            print(f"[llm] R2 单次闸门：本次预估 ¥{est:.4f} > ¥{cap_s}，拒绝。"
                  f"降 --max-tokens（当前 {args.max_tokens}）或改用 --tier free。", file=sys.stderr)
            return 6
        print(f"[llm] R2 降级额度：本次预估 ¥{est:.4f} ≤ 单次 ¥{cap_s}；"
              f"今日已用 ¥{spent_d:.4f}/{cap_d}", file=sys.stderr)

    if args.model:
        sel = [x for x in entries if x["model"] == args.model] or \
              [x for t in tiers.values() for x in t if x["model"] == args.model]
        entries = sel or entries

    msgs = ([{"role": "system", "content": args.system}] if args.system else []) + \
           [{"role": "user", "content": prompt}]

    # 附加请求体：人设档案 / 关闭思考 / 自定义
    extra = {}
    if args.persona:
        mf = common.ROOT / "content" / "persona.json"
        if mf.exists():
            extra["character_manifest"] = json.load(open(mf))
    if args.no_thinking:
        extra["thinking"] = {"type": "disabled"}
    if args.extra:
        try:
            extra.update(json.loads(args.extra))
        except Exception as ex:
            print(f"[llm] --extra 解析失败: {ex}", file=sys.stderr)

    fx = float(common.cfg("econ", {}).get("fx_usd_cny", 7.1))
    errs = []
    for entry in entries:
        t0 = time.time()
        # 档内模型可自带请求体扩展（例：本地 Qwen 思考模型需要 chat_template_kwargs
        # {"enable_thinking": false}，否则 max_tokens 会被思考层吃光、content 为空）
        ex = dict(entry.get("extra") or {})
        ex.update(extra)
        # 坑（09-27 实测）：zen 端点**拒收 body 里的 thinking 字段**，带了就 400
        # invalid_request_error（2/2 确定性复现，带/不带交替对比）。原先表现为
        # 「首选模型被拒、静默降到第 2 个」，完全看不出原因。
        # 所以按 provider 声明 no_thinking_field，丢掉该字段而不是报错。
        if "thinking" in ex and entry.get("no_thinking_field"):
            ex.pop("thinking")
        try:
            d = call(entry, msgs, args.max_tokens, args.timeout, args.temperature, extra=ex)
        except Exception as ex:
            errs.append(f"{entry['model']}: {type(ex).__name__} {str(ex)[:90]}")
            # 坑（09-27 查到）：原来只收进 errs，**要等全部模型挂掉才打印**。
            # 于是「首选模型被拒、静默降到第 2 个」完全看不见——
            # 09-27 实测：space-bunny-free 报 400 被静默跳过，事后才知道活是 Qwen2.5-7B 干的。
            # 跳一个报一个，「谁真正出的活」才永远可追（R27：判活看产出）。
            print(f"[llm] 跳过 {entry['model']}: {str(ex)[:120]}", file=sys.stderr)
            continue
        u = d.get("usage", {}) or {}
        msg = (d.get("choices") or [{}])[0].get("message", {}) or {}
        text = (msg.get("content") or "").strip()
        if not text and msg.get("reasoning_content"):
            text = "[仅思考层]: " + msg["reasoning_content"].strip()[:2000]
            print(f"[llm] ⚠️ {entry['model']} content 为空、只有思考层 → 提高 --max-tokens 或关思考"
                  f"（本地档已配 enable_thinking=false）", file=sys.stderr)
        c = cost_cny(entry, u, fx)
        if c > 0:
            common.append_ledger("expense", c, "api",
                                 f"{entry['model']} in={u.get('prompt_tokens')} out={u.get('completion_tokens')}")
        dt = time.time() - t0
        print(f"[llm] {entry['model']} {dt:.1f}s in={u.get('prompt_tokens')} "
              f"out={u.get('completion_tokens')} ¥{c:.4f} 余额¥{max(left-c,0):.2f}", file=sys.stderr)
        # 记下本次**实际**用的模型：任务级黑名单要按模型归因，不能只知道「用了 free 档」
        # （否则拉黑会拉在错误的模型头上，09-24 那篇的教训：不留证据的归因等于没测）
        try:
            lp = common.ROOT / "state" / "last_model.json"
            lp.parent.mkdir(parents=True, exist_ok=True)
            ltm = lp.with_suffix(".json.tmp")
            ltm.write_text(json.dumps({"model": entry["model"], "base": entry["base"],
                                       "tier": args.tier, "task": args.task,
                                       "ts": time.strftime("%F %T")},
                                      ensure_ascii=False), encoding="utf-8")
            ltm.replace(lp)
        except Exception:
            pass
        if args.json:
            print(json.dumps(d, ensure_ascii=False))
        else:
            print(text)
        return 0
    print("[llm] 全部模型失败：\n  " + "\n  ".join(errs), file=sys.stderr)

    # ---- 降级到 fallback 档（一次，不递归）----
    # 只降级一次：递归会让 R2 的单次/单日上限变成摆设（每降一层就多花一次）。
    if args.fallback_tier and args.fallback_tier != args.tier:
        print(f"[llm] 本档全挂 ⇒ 降级 {args.tier} → {args.fallback_tier}（仅一次，受 R2 限额）",
              file=sys.stderr)
        argv0 = sys.argv
        sys.argv = [argv0] + [a for a in argv0[1:] if a not in ("--tier", args.tier)] + \
                   ["--tier", args.fallback_tier, "--fallback-tier", ""]
        rc = main()
        sys.argv = argv0
        return rc
    return 4


if __name__ == "__main__":
    sys.exit(main())
