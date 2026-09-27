#!/usr/bin/env python3
"""改法 C 的判据汇总：免费期里 session_cost 增量恒为 0，成本判据失效，改看这三项。

为什么需要这个（09-27）
----------------------
改法 C 的原判据是「24h 内所有要模型的活走 chat.py，判据 = session_cost.py 增量」。
实测：09-27 07:30 连跑两次 session_cost 都是「增量 ¥0.000 / 无新增，不入账」，
ledger.csv 当天 0 条 —— 主模型在限时免费档，**成本分母被压成 0，这个实验做不出结论**。

那就把判据换成三个不被免费档污染的数：
  1. **路由分布**  ledger.csv 当天 api 条目按档位分组 → 免费占比（改法 C 的目的）
  2. **产出条数**  action tick 的 toolcalls 计数（真干活没有）—— R27 判活看产出
  3. **闸门结果**  article_lint 当天 block/warn（改法 C 会不会降质量）—— 认错条件

产出
----
  state/route_metrics.jsonl   每次追加一行（append-only，可重算）
  data/route_metrics.md       人读的最新一条

R24：写完必须挂调度 + 留一条验证记录。本文件挂 config/jobs.json 的 route_metrics job
（tick.py 每 10 分钟自动带）；验证记录 = 首次运行后在 route_metrics.jsonl 留下的那行。
三段字段任一为空 ⇒ 判据没建起来 ⇒ 该次实测**不算数**，不许写进文章。

  python3 tools/route_metrics.py            # 跑一次
  python3 tools/route_metrics.py --show 7   # 看最近 7 条
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "ledger.csv"
HIST = ROOT / "state" / "route_metrics.jsonl"
REPORT = ROOT / "data" / "route_metrics.md"
LINT = ROOT / "state" / "article_lint.jsonl"
BLACKLIST = ROOT / "state" / "model_blacklist.json"

# 付费档的标志：这些 tier 一旦出现在当天 api 条目里，就说明「免费层没兜住」
PAID_TIERS = ("deepseek-v4.1-flash", "mimo-v2.5", "qwen3.8-flash", "deepseek-v4-flash")
# 成本档位映射：llm.py 记账的 note 里是 "<model> in=... out=..."
TIER_OF = {"editor": "editor", "cheap": "cheap", "hard": "hard"}


def _rows():
    if not LEDGER.exists():
        return []
    out = []
    for ln in LEDGER.read_text(encoding="utf-8").splitlines()[1:]:
        p = ln.split(",", 5)
        if len(p) >= 4:
            out.append({"ts": p[0], "kind": p[1], "amt": p[2], "cat": p[3],
                        "note": p[4] if len(p) > 4 else ""})
    return out


def _tier_of(note):
    """把 ledger 的一条 api 记录归到档位。free 档 model 都不在 PAID_TIERS 里。"""
    m = re.match(r"([^\s]+)\s+in=", note or "")
    model = m.group(1) if m else (note or "").split()[0] if note else "?"
    for t, pref in TIER_OF.items():
        if model.startswith(pref):
            return t, model
    if model in PAID_TIERS:
        return "paid", model
    return "free", model


def collect(day=None):
    day = day or time.strftime("%Y-%m-%d")
    rows = [r for r in _rows() if r["ts"].startswith(day)]
    api = [r for r in rows if r["cat"] in ("api", "cloud", "session-api") and r["kind"] == "expense"]

    # 1) 路由分布
    tiers, models, spend = Counter(), Counter(), 0.0
    for r in api:
        t, m = _tier_of(r["note"])
        tiers[t] += 1
        models[m] += 1
        try:
            spend += float(r["amt"])
        except Exception:
            pass
    free_n = tiers.get("free", 0)
    paid_n = sum(v for k, v in tiers.items() if k != "free")

    # 2) 产出条数：action tick 的 toolcalls（R27 判活看产出，不看 exit 码）
    log = ROOT / "logs" / f"action-{day}.log"
    toolcalls = None
    if log.exists():
        hits = re.findall(r"toolcalls=(\d+)", log.read_text(encoding="utf-8", errors="replace"))
        if hits:
            toolcalls = sum(int(h) for h in hits)

    # 3) 闸门结果
    blk = wrn = lint_runs = 0
    if LINT.exists():
        for ln in LINT.read_text(encoding="utf-8", errors="replace").splitlines():
            if not ln.strip():
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if str(d.get("ts", "")).startswith(day):
                lint_runs += 1
                blk += int(d.get("block", 0) or 0)
                wrn += int(d.get("warn", 0) or 0)

    bl = {}
    if BLACKLIST.exists():
        try:
            bl = json.loads(BLACKLIST.read_text(encoding="utf-8"))
        except Exception:
            bl = {}

    return {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "day": day,
            "route": {"tiers": dict(tiers), "free_n": free_n, "paid_n": paid_n,
                      "free_ratio": round(free_n / max(free_n + paid_n, 1), 3),
                      "top_models": models.most_common(3), "spend_cny": round(spend, 4)},
            "output": {"toolcalls": toolcalls, "baseline_lo": 10,
                       "note": "基线下界 10 = 09-23/09-26 实测；不用最高的 37（那等于自设不可能的门槛）"},
            "gates": {"runs": lint_runs, "block": blk, "warn": wrn},
            "blacklist": {t: list(v.keys()) for t, v in bl.items()}}


def verdict(m):
    """三条验收。缺数据一律判不通过——不拿「没数据」当「没问题」。"""
    r, o, g = m["route"], m["output"], m["gates"]
    checks = []
    checks.append(("免费占比 100%", r["paid_n"] == 0,
                   f"free={r['free_n']} paid={r['paid_n']}"))
    tc = o["toolcalls"]
    checks.append(("产出没降(≥10)", (tc is not None and tc >= 10),
                   f"toolcalls={tc} 基线下界 10"))
    checks.append(("闸门没变差", g["runs"] > 0 and g["block"] == 0 and g["warn"] <= 1,
                   f"runs={g['runs']} block={g['block']} warn={g['warn']}"))
    return checks


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", type=int, default=0, help="打印最近 N 条历史")
    ap.add_argument("--day", default=None)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    if a.show:
        rows = [json.loads(l) for l in HIST.read_text(encoding="utf-8").splitlines() if l.strip()]
        for r in rows[-a.show:]:
            print(json.dumps(r, ensure_ascii=False))
        return 0

    m = collect(a.day)
    HIST.parent.mkdir(parents=True, exist_ok=True)
    with HIST.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(m, ensure_ascii=False) + "\n")   # R24：留痕

    ch = verdict(m)
    ok = all(c[1] for c in ch)
    L = [f"# 改法 C 路由判据（自动生成：tools/route_metrics.py）", "",
         f"> {m['ts']} ｜ {m['day']} ｜ 结论：**{'✅ 三条全过' if ok else '⚠️ 未全过'}**", "",
         "## 1. 路由分布（改法 C 的目的）", "",
         f"- 免费档调用 **{m['route']['free_n']}** 次 ｜ 付费档 **{m['route']['paid_n']}** 次"
         f" ｜ 免费占比 **{m['route']['free_ratio']*100:.0f}%**",
         f"- 当日 api 类支出 **¥{m['route']['spend_cny']}**",
         f"- 主要模型：{m['route']['top_models'] or '（当天无 api 调用）'}", "",
         "## 2. 产出条数（R27 判活看产出）", "",
         f"- action toolcalls = **{m['output']['toolcalls']}**（基线下界 10）", "",
         "## 3. 闸门结果（认错条件）", "",
         f"- article_lint 跑 {m['gates']['runs']} 次 ｜ block **{m['gates']['block']}**"
         f" ｜ warn **{m['gates']['warn']}**", "",
         "## 验收（三条，缺数据判不通过）", ""]
    for name, good, detail in ch:
        L.append(f"- {'✅' if good else '❌'} {name} — {detail}")
    if m["blacklist"]:
        L += ["", "## 任务级黑名单", ""]
        for t, ms in m["blacklist"].items():
            L.append(f"- `{t}`：{', '.join(ms)}")
    L += ["", "> 判活口径：宿主夜间关机（23:16→06:46），00:00-07:00 的 job 永不触发。",
          "> 汇总时若跨零点，须扣非在线时段，否则开窗第一分钟必假告警（R29）。"]

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(L) + "\n", encoding="utf-8")

    if not a.quiet:
        print(f"{'✅' if ok else '⚠️ '} {m['ts']} "
              f"free={m['route']['free_n']} paid={m['route']['paid_n']} "
              f"toolcalls={m['output']['toolcalls']} "
              f"lint={m['gates']['runs']}次/block={m['gates']['block']}/warn={m['gates']['warn']}")
        for name, good, detail in ch:
            print(f"   {'✅' if good else '❌'} {name} — {detail}")
        print(f"   留痕 → {HIST.relative_to(ROOT)} ｜ 报告 → {REPORT.relative_to(ROOT)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
