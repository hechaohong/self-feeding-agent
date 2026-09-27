#!/usr/bin/env python3
"""模型扣分制：慢和超时按「不可用」扣分，逐日结算，每二天重置，且可带衰减影响第二天。

为什么要有（09-27 用户提出）
----------------------------
09-27 探针实测：`THUDM/GLM-Z1-9B-0414` 慢到 **277s 超时**，`Qwen/Qwen3-VL-8B-Instruct` 也超时。
用户原话：「和不可用一样可以扣分」。道理：路由是**轮换**的，一个慢模型的代价不只是自己那次失败，
还把它后面所有模型的机会往后推 —— 一次 277s 超时，这轮任务就废了。
所以「慢」必须和「做错」进同一个分母，否则快而错的模型刷分、慢而对的模型被长期消耗。

为什么不能只有「当天分」
------------------------
只用当天分有个洞：模型在 27 号慢、28 号没人探它，28 号的分就是干净的 ⇒ 一次慢只疼一天。
而实际上限流/拥堵会**连着好几天**。所以要跨天，但跨天又不能等权：
  · 等权累积 ⇒ 一次偶发慢永久拉黑，过度反应
  · 纯当天 ⇒ 挡不住连续几天的拥堵
折中：**衰减**。昨天的扣分今天还剩 `decay` 比例（默认 0.5），前天剩 `decay²`，
超过 `window_days` 归零（=「每二天重置」）。

分数怎么算
----------
每天对每个模型算一个净值：

    day_delta = ok_reward × n_ok
              − fail_penalty  × n_fail     # 做错了（T2 FAIL / 验收不过）
              − error_penalty × n_error    # 请求失败、超时（用户 09-27：与不可用同权）
              − slow_penalty  × n_slow     # 超过 slow_threshold_s（也当不可用算）

总���（衰减加权）：

    score = Σ_d  day_delta(d) × decay^(今天 − d)      仅统计最近 window_days 天

`score` 越大越好。路由筛选用 `floor_score`：低于它就排到后面（不是直接禁用——
    「拉黑」是任务级的硬判决，见 tools/blacklist.py；这里是全局的软排序）。

参数在 config/llm.json 的 `penalty` 段，全部可调，不用改代码：

    slow_threshold_s  超过多少秒算慢         （默认 60）
    fail_penalty      做错扣几分             （默认 1.0）
    error_penalty     超时/失败扣几分        （默认 1.0）
    slow_penalty      慢扣几分               （默认 1.0）
    ok_reward         做对加几分             （默认 1.0）
    decay             衰减因子（0~1）        （默认 0.5）← 「影响第二天」的旋钮
    window_days       几天重置一次           （默认 2）  ← 「每二天重置」
    floor_score       低于这个分就不优先派    （默认 0.0）

  python3 tools/model_score.py                 # 看今天的分
  python3 tools/model_score.py --table         # 排行
  python3 tools/model_score.py --selftest      # 验衰减/窗口/慢判定三条
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "state" / "model_score.jsonl"
SCORE = ROOT / "state" / "model_score.json"

DEFAULTS = {"slow_threshold_s": 60, "fail_penalty": 1.0, "error_penalty": 1.0,
            "slow_penalty": 1.0, "ok_reward": 1.0, "decay": 0.5,
            "window_days": 2, "floor_score": 0.0}


def params():
    cfg = json.loads((ROOT / "config" / "llm.json").read_text(encoding="utf-8"))
    return {**DEFAULTS, **(cfg.get("penalty") or {})}


def record(model, outcome, secs=None, note=""):
    """记一次观测。outcome ∈ ok / fail / error / slow。

    `slow` 与 `fail`/`error` 分开记但**同权扣分**（用户 09-27 定的），
    分开只是为了报告里能看出「是慢还是错」——归因不能因为并成一个数就丢掉。
    """
    p = params()
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "day": time.strftime("%Y-%m-%d"),
           "model": model, "outcome": outcome,
           "secs": round(secs, 1) if isinstance(secs, (int, float)) else None,
           "note": (note or "")[:200]}
    HIST.parent.mkdir(parents=True, exist_ok=True)
    with HIST.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _rows():
    if not HIST.exists():
        return []
    out = []
    for ln in HIST.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def classify(ok, err, secs, p=None):
    """一次探针/任务的结果 → outcome。**慢优先于 ok**：慢到超时就是不可用，不是「做对了」。"""
    p = p or params()
    if err:
        return "error"
    if not ok:
        return "fail"
    if isinstance(secs, (int, float)) and secs > p["slow_threshold_s"]:
        return "slow"
    return "ok"


def day_deltas(rows=None, p=None):
    """{model: {day: delta}}"""
    p = p or params()
    rows = rows if rows is not None else _rows()
    d = defaultdict(lambda: defaultdict(float))
    for r in rows:
        w = {"ok": p["ok_reward"], "fail": -p["fail_penalty"],
             "error": -p["error_penalty"], "slow": -p["slow_penalty"]}.get(r["outcome"], 0.0)
        d[r["model"]][r["day"]] += w
    return {m: dict(v) for m, v in d.items()}


def scores(today=None, p=None, rows=None):
    """衰减加权总分。today/rows 参数存在是为了 selftest 能验衰减而不依赖真实日期与真实数据。"""
    p = p or params()
    today = today or time.strftime("%Y-%m-%d")
    out, detail = {}, {}
    for m, days in day_deltas(rows=rows, p=p).items():
        s = 0.0
        parts = []
        for day, delta in sorted(days.items()):
            age = _age(day, today)
            if age < 0 or age >= p["window_days"]:
                continue                      # 未来日期 / 超出窗口 ⇒ 归零（= 每二天重置）
            wgt = p["decay"] ** age
            s += delta * wgt
            parts.append({"day": day, "delta": round(delta, 2), "age": age,
                          "weight": round(wgt, 3), "contrib": round(delta * wgt, 3)})
        out[m] = round(s, 3)
        detail[m] = sorted(parts, key=lambda x: x["day"], reverse=True)
    return out, detail


def _age(day, today):
    try:
        a = time.mktime(time.strptime(day, "%Y-%m-%d"))
        b = time.mktime(time.strptime(today, "%Y-%m-%d"))
        return int(round((b - a) / 86400))
    except Exception:
        return 999


def ranking(today=None, p=None):
    sc, _ = scores(today, p)
    return sorted(sc.items(), key=lambda kv: kv[1], reverse=True)


def below_floor(today=None, p=None):
    p = p or params()
    return [m for m, s in ranking(today, p) if s < p["floor_score"]]


def recompute():
    sc, det = scores()
    SCORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SCORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"ts": time.strftime("%F %T"), "scores": sc, "detail": det},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(SCORE)
    return sc


def selftest():
    """验四条：慢>阈值判 slow、超时判 error 且优先于 ok、衰减生效、窗口外归零。"""
    p = params()
    checks = []
    checks.append(("60s 判 ok", classify(True, False, 59, p) == "ok", "59 < 60 阈值"))
    checks.append(("61s 判 slow", classify(True, False, 61, p) == "slow", "超时即不可用（用户定）"))
    checks.append(("超时优先于 ok", classify(True, "Timeout", 300, p) == "error",
                   "跑成了但超时 ⇒ error，不是 ok"))

    # 造数据验衰减与窗口（直接喂 day_deltas，不碰真实 HIST）
    # 坑（09-27 selftest 第一次失败）：fixture 里不小心给今天放了两条
    # （strftime 和 _shift(0) 是同一天）⇒ 断言 -1.0 实际 -2.0。
    # 教训同 09-24 那篇：**先证明测试本身对，再信它的结论**。
    fake = [{"model": "M", "day": _shift(0), "outcome": "fail"},
            {"model": "M", "day": _shift(-1), "outcome": "fail"},
            {"model": "M", "day": _shift(-2), "outcome": "fail"}]
    dd = day_deltas(fake, p)
    today = time.strftime("%Y-%m-%d")
    sc, det = scores(today, p, rows=fake)
    got = [x for x in det["M"] if x["age"] == 0][0]["contrib"]
    yst = [x for x in det["M"] if x["age"] == 1][0]["contrib"]
    checks.append((f"今天扣 {p['fail_penalty']}", got == -p["fail_penalty"], f"实际 {got}"))
    checks.append((f"昨天衰减到 {p['decay']}×",
                   abs(yst - (-p["fail_penalty"] * p["decay"])) < 1e-6, f"实际 {yst}"))
    checks.append((f"{p['window_days']} 天前的分归零（每二天重置）",
                   sc["M"] == -p["fail_penalty"] * (1 + p["decay"]), f"实际总分 {sc['M']}"))

    ok = all(c[1] for c in checks)
    for n, good, d in checks:
        print(f"  {'✅' if good else '❌'} {n} — {d}")
    print(f"\nselftest: {'全部通过' if ok else '有失败'}（{sum(1 for c in checks if c[1])}/{len(checks)}）"
          f"\n当前参数: {json.dumps(p, ensure_ascii=False)}")
    return 0 if ok else 1


def _shift(days):
    return time.strftime("%Y-%m-%d", time.localtime(time.time() + days * 86400))


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", action="store_true", help="排行")
    ap.add_argument("--detail", default="", help="看某个模型的逐日明细")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--params", action="store_true", help="只打参数")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.params:
        print(json.dumps(params(), ensure_ascii=False, indent=1))
        return 0
    recompute()
    sc, det = scores()
    if not sc:
        print("还没有观测记录。先跑 `python3 tools/cap_probe.py`。")
        return 0
    if a.detail:
        print(json.dumps(det.get(a.detail, []), ensure_ascii=False, indent=1))
        return 0
    p = params()
    print(f"评分口径：窗口 {p['window_days']} 天 ｜ 衰减 {p['decay']} ｜ "
          f"慢阈值 {p['slow_threshold_s']}s ｜ 地板分 {p['floor_score']}\n")
    for m, s in ranking():
        kinds = Counter_kinds(m)
        flag = "  ⛔低于地板分" if s < p["floor_score"] else ""
        print(f"  {s:>7.3f}  {m:<44} {kinds}{flag}")
    return 0


def Counter_kinds(model):
    c = defaultdict(int)
    for r in _rows():
        if r["model"] == model:
            c[r["outcome"]] += 1
    return dict(c)


if __name__ == "__main__":
    sys.exit(main())
