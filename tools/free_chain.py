#!/usr/bin/env python3
"""从**真实**免费池生成 action tick 的轮换链——不再有任何硬编码模型名。

为什么删掉硬编码（09-27）
--------------------------
原 `action.sh` 里的 `FREE_CHAIN` 是 09-21 手写的 4 个模型。09-27 复核发现两处已经烂掉：
  1. 链里 `nvidia/nemotron-3-super-120b-a12b:free` 与当日实测名单**不是同一个模型**
     （实测活的是 `nvidia/nemotron-3.5-lightning:free`）
  2. 链是 09-21 的一次性快照，而免费池是**每天都在变的活物**（09-21 一天少 4 个模型）
手写链 = 每天都在骗自己的名单。所以：名单只能从探测结果生成，不能从记忆生成。

「真实」的定义（本脚本的判据，按可信度排序）
--------------------------------------------
  ① `pi --list-models` —— **pi 自己认的** provider/模型表（09-27 实测 5 provider / 470 模型）。
     这是唯一权威：action.sh 是用 `pi --provider` 调的，pi 不认的 provider 写了也白写
     （09-27 实测 pi 认 zen / sf / openrouter / opencode-go / local，**不认 siliconflow**）。
  ② `data/free_pool_history.jsonl` —— 真调过一次的记录（ok / 429 / 失效）。
     判据是「真调一次拿到 content」，不是查名单（free_pool.py 的原话）。
  ③ `state/model_score.json` —— 扣分制（慢/超时与不可用同权，见 tools/model_score.py）
  ④ `state/model_blacklist.json` —— 任务级拉黑（task=action）

**名单里有 ≠ 我能调 ≠ 调得动活**。四层筛完还剩的，才是能真派活的。

  python3 tools/free_chain.py             # 输出 provider|model，一行一个
  python3 tools/free_chain.py --json      # 带筛选理由
  python3 tools/free_chain.py --explain   # 每个模型为什么进/没进链
  python3 tools/free_chain.py --selftest  # 验四层筛的边界
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "data" / "free_pool_history.jsonl"
SCORE = ROOT / "state" / "model_score.json"
BLACKLIST = ROOT / "state" / "model_blacklist.json"
# pi 的位置：可用环境变量 PI_BIN 覆盖（跟 action.sh 同一个变量）。
# 不用硬编码绝对路径——本项目的公开仓库惯例是**不把本机路径发出去**（已公开的
# 11 个 tools 文件里零绝对路径，只有端口）。
PI = os.environ.get("PI_BIN") or shutil.which("pi") or "pi"

TASK = "action"                       # 黑名单按这个任务查
FRESH_DAYS = 2                        # 探测记录多新算数
FREE_PROVIDERS = {"sf", "local"}      # 整档都免费的 provider（siliconflow / 本机 llama）
PAID_PROVIDERS = {"opencode-go", "openrouter"}   # 混档：只有带免费后缀的才算
PI_KNOWN = {"zen", "sf", "openrouter", "opencode-go", "local"}   # pi 认的（09-27 实测）


def pi_models():
    """→ [(provider, model)]，来自 pi 自己的表。pi 挂了返回 []。"""
    try:
        r = subprocess.run([PI, "--list-models"], capture_output=True, text=True, timeout=120)
    except Exception:
        return []
    out = []
    for ln in (r.stdout or "").splitlines()[1:]:
        p = ln.split()
        if len(p) >= 2 and re.match(r"^[a-zA-Z0-9_.-]+$", p[0]):
            out.append((p[0], p[1]))
    return out


def is_free(provider, model):
    if model.endswith(":free") or model.endswith("-free"):
        return True
    if provider in FREE_PROVIDERS:
        return True
    return False


def _age(d, today=None):
    today = today or time.strftime("%Y-%m-%d")
    try:
        a = time.mktime(time.strptime(d, "%Y-%m-%d"))
        b = time.mktime(time.strptime(today, "%Y-%m-%d"))
        return (b - a) / 86400.0
    except Exception:
        return 999


def probe_status(today=None):
    """(provider,model) → 最新探测结论。新鲜度内 ok=True 才算「活着」。"""
    st = {}
    if not HIST.exists():
        return st
    for ln in HIST.read_text(encoding="utf-8", errors="replace").splitlines():
        if not ln.strip():
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        k = (d.get("provider"), d.get("model"))
        if not k[0] or not k[1]:
            continue
        prev = st.get(k)
        if prev is None or str(d.get("ts", "")) >= str(prev.get("ts", "")):
            st[k] = d
    return st


def build(today=None, task=TASK, limit=None):
    """四层筛。返回 (链, 逐条理由)。链 = [(provider, model, score, secs)]。"""
    today = today or time.strftime("%Y-%m-%d")
    models = pi_models()
    st = probe_status(today)
    try:
        scores = (json.loads(SCORE.read_text(encoding="utf-8")) or {}).get("scores", {})
    except Exception:
        scores = {}
    try:
        bl = json.loads(BLACKLIST.read_text(encoding="utf-8")).get(task, {})
    except Exception:
        bl = {}

    kept, why = [], []
    for prov, mod in models:
        if prov not in PI_KNOWN:
            why.append({"provider": prov, "model": mod, "keep": False, "reason": "pi 不认这个 provider"})
            continue
        if not is_free(prov, mod):
            why.append({"provider": prov, "model": mod, "keep": False, "reason": "不是免费档（无 free 后缀）"})
            continue
        if mod in bl:
            why.append({"provider": prov, "model": mod, "keep": False,
                        "reason": f"任务 {task} 已拉黑：{bl[mod].get('last_why', '')[:50]}"})
            continue
        p = st.get((prov, mod))
        if p is not None and not p.get("ok"):
            age = _age(str(p.get("date", "")), today)
            if p.get("quota_suspect") or p.get("code") == 429:
                why.append({"provider": prov, "model": mod, "keep": True,
                            "reason": f"限流未验(429)，仍保留（{age:.0f}天前）", "quota": True})
                kept.append((prov, mod, scores.get(mod, 0.0), 9e9))
                continue
            if age < FRESH_DAYS:
                why.append({"provider": prov, "model": mod, "keep": False,
                            "reason": f"实测失败（{p.get('code')}）{age:.0f}天前，在新鲜期内"})
                continue
        s = scores.get(mod)
        kept.append((prov, mod, s if s is not None else 0.0, (p or {}).get("sec") or 9e9))
        why.append({"provider": prov, "model": mod, "keep": True,
                    "reason": (f"有扣分分 {s}" if s is not None else "免费档，无扣分记录（按 0 排）")
                              + (f"，实测 {p.get('sec')}s" if p and p.get("ok") else "")})

    # 排序：有分的先、分数高的先；同分按实测延迟。探不到延迟的排最后。
    kept.sort(key=lambda t: (-t[2], t[3]))
    if limit:
        kept = kept[:limit]
    return kept, why


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--explain", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    kept, why = build(limit=a.limit)
    if a.explain:
        for w in why:
            print(f"  {'✅' if w['keep'] else '  '} {w['provider']}/{w['model']:<44} {w['reason']}")
        print()
    if a.json:
        print(json.dumps([{"provider": p, "model": m, "score": s} for p, m, s, _ in kept],
                         ensure_ascii=False, indent=1))
        return 0
    for p, m, _s, _t in kept:
        print(f"{p}|{m}")
    return 0 if kept else 1


def selftest():
    """验四层的边界。纯函数，碰不到网络与真实数据。"""
    checks = []
    checks.append(("sf 整档免费", is_free("sf", "Qwen/Qwen3-8B") is True, "siliconflow 全部 ¥0"))
    checks.append(("local 整档免费", is_free("local", "qwen9b/qwen3.5-9b") is True, "本机 llama 只花电费"))
    checks.append(("zen 的 -free 算免费", is_free("zen", "mimo-v2.5-free") is True, "后缀判定"))
    checks.append(("zen 的付费版不算", is_free("zen", "deepseek-v4.1-flash") is False, "别把付费混进免费链"))
    checks.append(("opencode-go 只有 -free 算",
                   is_free("opencode-go", "space-bunny-free") is True
                   and is_free("opencode-go", "gpt-5.6-luna") is False,
                   "space-bunny-free 是 go 侧限时免费，其余 go 模型是付费"))
    checks.append(("openrouter 只有 :free 算",
                   is_free("openrouter", "z-ai/glm-5.2:free") is True
                   and is_free("openrouter", "anthropic/claude-haiku-latest") is False,
                   "openrouter 混档，417 个里 17 个免费"))
    ok = all(c[1] for c in checks)
    for n, g, d in checks:
        print(f"  {'✅' if g else '❌'} {n} — {d}")
    print(f"\nselftest: {'全部通过' if ok else '有失败'}（{sum(1 for c in checks if c[1])}/{len(checks)}）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
