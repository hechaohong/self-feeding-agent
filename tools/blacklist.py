#!/usr/bin/env python3
"""任务级模型黑名单：某任务用某模型多次验收不过 ⇒ 该任务不再派它。

为什么要有（09-27 用户提出）
----------------------------
free_pool.py 的可达性判据只验「回没回字符串」：prompt="Reply with exactly: OK"、
max_tokens=32、判据 ``ok = bool(txt)``（free_pool.py:101-113）。
于是 7B/8B 弱模型会以「健康」身份进池，被路由派去写 5000 字稿。
**可达 ≠ 能用。** 而且「能用」不是模型的全局属性，是**某个任务上的属性**：
同一个模型可能在「抽 JSON」上合格、在「改长稿」上不合格。
所以黑名单必须是**任务级**，不是全局级：
  · 全局拉黑 → 误伤（它在别的任务上还是好的）
  · 不拉黑   → 同一个任务反复踩同一个坑，白花钱还得出坏稿

判据
----
同一 (task, model) **连续** FAILS 次验收不过 ⇒ 拉黑 DAYS 天。
  · 默认 FAILS=2：只失败一次可能是偶然（限流/超时/脏 prompt），不该定性
  · 中间只要有 1 次 ok，连续计数清零 —— 一次成功说明之前是噪声

存储
----
  state/model_blacklist.jsonl  append-only 历史（每条 record 一行，永不重写）
  state/model_blacklist.json   从历史**重算**出的当前生效名单（可审计、可复现）
重算而不是就地改，是为了让「为什么这个模型被拉黑」永远能追回到原始记录
（09-24 那篇的教训：拿不出证据的归因等于没测）。

R24：判活看产出。本模块被 llm.py 在**每次路由前**读，所以不需要额外调度；
它自己的自检入口是 `python3 tools/blacklist.py selftest`，见文末。
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "state" / "model_blacklist.jsonl"
LIVE = ROOT / "state" / "model_blacklist.json"

FAILS_DEFAULT = 2
DAYS_DEFAULT = 7


def _hist_rows():
    if not HIST.exists():
        return []
    out = []
    for ln in HIST.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue          # 半行/坏行跳过，不让一条脏数据毒化整个名单
    return out


def append(task, model, ok, why="", fails_default=None, days=None):
    """记一次验收结果。ok=True 表示这次做对了（会清零连续失败计数）。"""
    f = int(fails_default or FAILS_DEFAULT)
    d = int(days or DAYS_DEFAULT)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "task": task, "model": model,
           "ok": bool(ok), "why": (why or "")[:300], "fails_default": f, "days": d}
    HIST.parent.mkdir(parents=True, exist_ok=True)
    with HIST.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    recompute()
    return rec


def recompute():
    """从 append-only 历史重算当前生效名单。返回 {task: {model: {...}}}。"""
    streak = defaultdict(int)       # (task,model) -> 连续失败次数
    rules = {}                      # (task,model) -> (fails_default, days)
    for r in _hist_rows():
        key = (r["task"], r["model"])
        rules[key] = (int(r.get("fails_default", FAILS_DEFAULT)), int(r.get("days", DAYS_DEFAULT)))
        if r["ok"]:
            streak[key] = 0         # 一次成功就清零
        else:
            streak[key] += 1

    live = defaultdict(dict)
    for (task, model), n in streak.items():
        f, d = rules[(task, model)]
        if n >= f:
            live[task][model] = {
                "fail_streak": n, "fail_threshold": f, "days": d,
                "last_why": next((r["why"] for r in reversed(_hist_rows())
                                  if r["task"] == task and r["model"] == model and not r["ok"]), ""),
                "since": time.strftime("%F")}

    live_d = {t: dict(v) for t, v in live.items()}
    tmp = LIVE.with_suffix(".json.tmp")
    LIVE.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(live_d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(LIVE)
    return live_d


def _load_live():
    if not LIVE.exists():
        return {}
    try:
        return json.loads(LIVE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def blocked(task, model):
    """该任务是否已被拉黑这个模型。"""
    return model in _load_live().get(task, {})


def blocked_reason(task, model):
    return _load_live().get(task, {}).get(model, {}).get("last_why", "")


def filter_entries(task, entries):
    """给 llm.py 用：把该任务已拉黑的模型从路由列表里剔掉。

    entries 是 config/llm.json 的档内列表，每项至少有 'model'。
    返回 (剩下的 entries, 被剔掉的个数)。**不改动入参**。
    """
    live = _load_live().get(task, {})
    if not live:
        return list(entries), 0
    keep = [e for e in entries if e.get("model") not in live]
    return keep, len(entries) - len(keep)


def unblock(task, model):
    """人工解除拉黑：记一条 ok，把连续失败清零。"""
    return append(task, model, True, why="manual unblock")


def status():
    return _load_live()


def selftest():
    """R24 的验证入口：造一组假数据，验阈值/清零/任务隔离三条。

    刻意不碰真实 HIST —— 验证代码不该污染生产数据。
    """
    global HIST, LIVE
    bak_h, bak_l = HIST, LIVE
    HIST = ROOT / "state" / ".selftest_hist.jsonl"
    LIVE = ROOT / "state" / ".selftest_live.json"
    try:
        for p in (HIST, LIVE):
            if p.exists():
                p.unlink()
        checks = []

        # 1) 连续 2 次失败 ⇒ 拉黑
        append("t1", "m_bad", False, "编数字")
        checks.append(("1次失败不拉黑", not blocked("t1", "m_bad")))
        append("t1", "m_bad", False, "又编数字")
        checks.append(("连续2次失败拉黑", blocked("t1", "m_bad")))

        # 2) 一次成功 ⇒ 立刻解封
        append("t1", "m_bad", True, "这次对了")
        checks.append(("成功后解除", not blocked("t1", "m_bad")))

        # 3) 任务隔离：同模型在别的任务上不受影响
        append("t2", "m_bad", False, "a"); append("t2", "m_bad", False, "b")
        checks.append(("任务A拉黑", blocked("t2", "m_bad")))
        checks.append(("任务B不受影响", not blocked("t1", "m_bad")))

        # 4) filter_entries 不改入参
        ents = [{"model": "m_bad"}, {"model": "m_good"}]
        keep, n = filter_entries("t2", ents)
        checks.append(("filter 剔掉1个", n == 1 and len(keep) == 1 and keep[0]["model"] == "m_good"))
        checks.append(("filter 不改入参", len(ents) == 2))

        # 5) 坏行不毒化名单
        HIST.open("a").write("{not json\n")
        recompute()
        checks.append(("坏行不影响重算", blocked("t2", "m_bad")))

        ok = all(c[1] for c in checks)
        for name, good in checks:
            print(f"  {'✅' if good else '❌'} {name}")
        print(f"\nselftest: {'全部通过' if ok else '有失败'}（{sum(1 for c in checks if c[1])}/{len(checks)}）")
        return 0 if ok else 1
    finally:
        for p in (HIST, LIVE):
            if p.exists():
                p.unlink()
        HIST, LIVE = bak_h, bak_l


def main():
    ap = __import__("argparse").ArgumentParser(description="任务级模型黑名单")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("cmd", nargs="?", choices=["status", "fail", "ok", "unblock", "selftest", "recompute"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--why", default="")
    a = ap.parse_args()

    if a.cmd == "selftest":
        return selftest()
    if a.cmd in (None, "status"):
        s = status()
        print(json.dumps(s, ensure_ascii=False, indent=1) if a.json or not s
              else json.dumps(s, ensure_ascii=False, indent=1))
        if not s:
            print("（当前无拉黑记录）" if not a.json else "")
        return 0
    if a.cmd == "recompute":
        recompute(); print("recomputed"); return 0
    if len(a.args) < 2:
        print(f"用法: {a.args[0] if a.args else a.cmd} <task> <model>", file=sys.stderr)
        return 2
    task, model = a.args[0], a.args[1]
    if a.cmd == "fail":
        r = append(task, model, False, a.why)
    elif a.cmd in ("ok", "unblock"):
        r = append(task, model, True, a.why or "ok")
    else:
        return 2
    print(f"recorded {a.cmd}: {task}/{model} → 现在{'已拉黑' if blocked(task, model) else '未拉黑'}")
    if a.why:
        print(f"  why: {a.why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
