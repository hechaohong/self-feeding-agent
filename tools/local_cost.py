#!/usr/bin/env python3
"""本地推理真实成本（¥0，纯脚本）：拆四种口径，回答「本地到底省不省钱」。

  python3 tools/local_cost.py                      # 用 logs/ 里最新的 llama-*.log
  python3 tools/local_cost.py --log logs/llama-qwen27b.log
  python3 tools/local_cost.py --write data/local-gpu-cost.md
  python3 tools/local_cost.py --hours 5.5          # 投影：把服务挂 H 小时（夜间窗口）

为什么要它：`¥0.93/M 出` 是**满负荷边际成本**（跑分口径），不是账单口径。
账单按**开机小时**计费（¥0.12/h），所以真实 ¥/M 由**占空比**决定，不由 tok/s 决定。
决策用错口径 → 把 4.6× 当成年化收益去买卡，或反过来把白送的算力当亏损。
（NIGHT.md N4「无活不开机」的依据就是这个公式，不是感觉。）

口径定义：
  A 满负荷边际  = 出速 tok/s 全速跑，只算这几秒的电
  B 本次账单    = 整段开机时间 × 功率 × 电价 ÷ 本次产出 token（含冷启动 + 空转）
  C 同工作量    = 同一批 in/out，云端要多少钱（含云端输入费）
  D 保本占空比  = 本地 B = 云端 C 时的占空比；实测占空比低于它 → 本地亏
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

ROOT = common.ROOT
TS = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d+) ")
PE = re.compile(r"prompt eval time =\s*([\d.]+) ms /\s*(\d+) tokens")
EV = re.compile(r"(?<!prompt )\beval time =\s*([\d.]+) ms /\s*(\d+) tokens")


def _t(m) -> float:
    """llama.cpp 日志时间戳 M.SS.mmm.uuu → 秒。"""
    return int(m[0]) * 60 + int(m[1]) + int(m[2]) / 1000


def parse(path: Path) -> dict:
    rows = []
    for ln in path.read_text(errors="replace").splitlines():
        m = TS.match(ln)
        if m:
            rows.append((_t(m.groups()), ln))
    if not rows:
        return {}
    load = next((s for s, l in rows if "model loaded" in l), 0.0)
    pe = pt = ev = ot = nreq = 0.0
    for _, l in rows:
        if "print_timing" not in l:
            continue
        m = PE.search(l)
        if m:
            pe += float(m.group(1)); pt += int(m.group(2)); continue
        m = EV.search(l)
        if m:
            ev += float(m.group(1)); ot += int(m.group(2)); nreq += 1
    return {"log": path.name, "up_s": rows[-1][0] - rows[0][0], "load_s": load,
            "requests": int(nreq), "in_tok": int(pt), "out_tok": int(ot),
            "gen_s": (pe + ev) / 1000.0, "ev_s": ev / 1000.0}


def cost(logs: list[Path], cloud_key: str = "deepseek-v4-flash", hours: float | None = None) -> dict:
    sess = [s for s in (parse(p) for p in logs) if s and s["out_tok"]]
    if not sess:
        raise SystemExit("没解析到带 token 的日志（print_timing 行）")
    econ, llm = common.cfg("econ", {}), common.cfg("llm", {})
    fx = float(econ.get("fx_usd_cny") or 7.1)
    kw = common.cfg("night", {}).get("local", {})
    w = float(kw.get("extra_w") or econ.get("gpu_extra_w") or 200) / 1000.0   # kW
    price = float(kw.get("price_kwh") or econ.get("price_kwh") or 0.6)        # ¥/kWh
    per_h = w * price                                                          # ¥/小时（开机即计费）

    up_s = sum(s["up_s"] for s in sess)
    gen_s = sum(s["gen_s"] for s in sess)
    out = sum(s["out_tok"] for s in sess)
    tin = sum(s["in_tok"] for s in sess)
    load_s = max(s["load_s"] for s in sess)
    if hours:                      # 投影：只把开机时间换掉，产出沿用实测速率
        up_s = hours * 3600.0
    elec = up_s / 3600.0 * per_h
    rate = out / gen_s if gen_s else 0.0
    ev_s = sum(s["ev_s"] for s in sess)
    decode_rate = out / ev_s if ev_s else 0.0

    cloud = next((t for t in llm.get("tiers", {}).get("cheap", []) if t.get("model") == cloud_key), None)
    c_in = float(cloud.get("in") or 0) * fx if cloud else 0.0
    c_out = float(cloud.get("out") or 0) * fx if cloud else 0.0
    cloud_total = tin / 1e6 * c_in + out / 1e6 * c_out
    # 单位成本口径统一成「¥/M 输出 token」（输入按实测比例折算进去）
    ratio = (tin / out) if out else 0.0
    cloud_per_m = (ratio * c_in + c_out)
    local_marginal_per_m = per_h / (rate * 3600 / 1e6) if rate else 0.0
    local_bill_per_m = elec / (out / 1e6) if out else 0.0
    breakeven_duty = (local_marginal_per_m / cloud_per_m) if cloud_per_m else 0.0
    duty = gen_s / up_s if up_s else 0.0
    return {"sessions": len(sess), "log": sess[-1]["log"], "up_s": up_s, "load_s": load_s,
            "requests": sum(s["requests"] for s in sess), "in_tok": tin, "out_tok": out,
            "gen_s": gen_s, "rate": rate, "decode_rate": decode_rate, "per_h": per_h, "elec": elec, "duty": duty,
            "cloud_key": cloud_key, "c_in": c_in, "c_out": c_out, "cloud_total": cloud_total,
            "cloud_per_m": cloud_per_m, "local_marginal_per_m": local_marginal_per_m,
            "local_bill_per_m": local_bill_per_m, "breakeven_duty": breakeven_duty,
            "in_out_ratio": ratio, "projected_hours": hours}


def report(r: dict) -> str:
    win = "实测" if not r["projected_hours"] else f"投影 {r['projected_hours']}h 开机"
    verdict = "本地赢" if r["duty"] > r["breakeven_duty"] else "本地亏"
    v_work = "本地便宜" if r["elec"] < r["cloud_total"] else "本地更贵"
    L = [
        f"# 本地推理真实成本（{win}）",
        f"- 样本：{r['log']} ｜ {r['sessions']} 段会话 ｜ 请求 {r['requests']} 个",
        f"- 开机 {r['up_s']:.1f}s（{r['up_s']/60:.1f} 分钟，冷启动 {r['load_s']:.1f}s）｜ 实际计算 {r['gen_s']:.1f}s ｜ **占空比 {r['duty']*100:.1f}%**",
        f"- 产出：入 {r['in_tok']} tok / 出 {r['out_tok']} tok（入:出 = {r['in_out_ratio']:.2f}:1）｜ 出速 **{r['rate']:.1f} tok/s**",
        f"- 电费口径：{r['per_h']:.4f} ¥/小时（开机即计费，不论是否在算）",
        "",
        f"| 口径 | 单价 | 说明 |",
        f"|---|---|---|",
        f"| A 满负荷边际 | **¥{r['local_marginal_per_m']:.2f}/M 出** | 全速跑才成立，跑分口径 |",
        f"| B 本次账单 | **¥{r['local_bill_per_m']:.2f}/M 出** | {r['elec']:.6f} 元 ÷ {r['out_tok']} tok |",
        f"| C 云端同工作量 | **¥{r['cloud_per_m']:.2f}/M 出**（¥{r['cloud_total']:.6f} 元）| {r['cloud_key']}：入 {r['c_in']:.3f} + 出 {r['c_out']:.2f} |",
        f"| D 保本占空比 | **{r['breakeven_duty']*100:.1f}%** | A=C 的临界点；实测 {r['duty']*100:.1f}% → **{verdict}** |",
        "",
        f"- 同工作量比价：本地 ¥{r['elec']:.6f} vs 云端 ¥{r['cloud_total']:.6f} → **{v_work}**（{abs(1-r['elec']/r['cloud_total'])*100:.0f}%）",
        f"- 判据：占空比 = 计算秒数 ÷ 开机秒数。它由「一天几个 job、每个 job 多长」决定，跟 tok/s 跑分无关。",
        f"- 冷启动：{r['load_s']:.1f}s（{r['load_s']/3600*r['per_h']:.6f} 元/次；按小时租的机器上这 {r['load_s']:.0f}s 也是计费的）",
        f"- 投影公式：开机 H 小时的账单 = H × ¥{r['per_h']:.2f}（跟有没有在算无关）。打平云端需要**平均**出速 ≥ {r['breakeven_duty']*r['rate']:.1f} tok/s"
        f"（= 保本占空比 {r['breakeven_duty']*100:.0f}% × 实测出速 {r['rate']:.1f} tok/s；同 {r['in_out_ratio']:.1f}:1 输入比口径），而纯解码峰值是 {r['decode_rate']:.1f} tok/s。",
    ]
    return "\n".join(L)


def breakeven_table(r: dict) -> str:
    """把实测 in:out 比代进每个云端档位 → 本地赢需要多高占空比。

    这一列才是选型依据：本地 GPU 的对手不是「云端 API」这个笼统概念，
    而是你手上具体那个模型的价格。价格越低（尤其 ¥0 免费池），本地越难赢。
    """
    llm, econ = common.cfg("llm", {}), common.cfg("econ", {})
    fx = float(econ.get("fx_usd_cny") or 7.1)
    ratio = r["in_out_ratio"]
    rows = ["| 云端档位 | 模型 | ¥/M 入 | ¥/M 出 | 同工作量 ¥/M 出 | **保本占空比** |",
            "|---|---|---|---|---|---|"]
    for tier in ("free", "cheap", "hard", "persona"):
        for t in llm.get("tiers", {}).get(tier, []):
            cur = t.get("currency", "usd")
            f = 1.0 if cur == "cny" else fx
            ci, co = float(t.get("in") or 0) * f, float(t.get("out") or 0) * f
            if ci == 0 and co == 0:
                be = "∞（本地永远不划算：对手不要钱）"
                unit = "0"
            else:
                unit = f"{ratio*ci+co:.2f}"
                be = f"{(r['local_marginal_per_m']/(ratio*ci+co)*100):.1f}%"
            rows.append(f"| {tier} | {t.get('model')} | {ci:.3f} | {co:.2f} | {unit} | {be} |")
    return "\n".join(["## 保本占空比对照表（把实测 in:out = %0.2f:1 代进各档位）" % ratio,
                      ""] + rows + ["",
                      f"> 读法：实测占空比 **{r['duty']*100:.1f}%** 高于表里的保本线 → 用本地；低于 → 用云端。",
                      f"> 本地满负荷边际成本用 ¥{r['local_marginal_per_m']:.2f}/M 出（实测 {r['rate']:.1f} tok/s、¥{r['per_h']:.2f}/小时）。",
                      "> 免费池看着最香，但本项目的免费档有速率与质量限制，¥0 只在「跑得通」时才成立 —— 那部分我没数据，不算进结论。"])


def window_hours() -> float:
    """夜间窗口小时数（config/night.json；解析失败则 5.5）。"""
    w = (common.cfg("night", {}) or {}).get("window", {})
    try:
        h1, m1 = (int(x) for x in str(w.get("start", "00:10")).split(":"))
        h2, m2 = (int(x) for x in str(w.get("end", "05:40")).split(":"))
        d = (h2 * 60 + m2) - (h1 * 60 + m1)
        return round(d / 60, 2) if d > 0 else 5.5
    except Exception:
        return 5.5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=None)
    ap.add_argument("--all", action="store_true", help="聚合 logs/llama-*.log 全部")
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--cloud", default="deepseek-v4-flash")
    ap.add_argument("--breakeven-table", action="store_true", help="打印各云端档位的保本占空比")
    ap.add_argument("--write", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.log:
        logs = [Path(a.log)]
    else:
        logs = sorted((ROOT / "logs").glob("llama-*.log"))
    if not a.all and len(logs) > 1:
        logs = logs[-1:]
    if not logs:
        raise SystemExit("logs/ 下没有 llama-*.log")
    r = cost(logs, a.cloud, a.hours)
    text = report(r)
    if a.breakeven_table:
        text += "\n\n" + breakeven_table(r)
    if a.write and not a.hours:      # 顺手把「服务挂满夜间窗口」的投影也写进去（N4 的依据）
        h = window_hours()
        p = cost(logs, a.cloud, h)
        text += (f"\n\n---\n\n{report(p)}\n\n"
                 f"> 同一批 token、同一台机器，只是把开机时间从 {r['up_s']/60:.1f} 分钟拉到夜间窗口 {h}h。\n"
                 f"> 占空比 {p['duty']*100:.1f}% < 保本线 {p['breakeven_duty']*100:.1f}% → 单价从 ¥{r['local_bill_per_m']:.2f}/M 涨到 ¥{p['local_bill_per_m']:.2f}/M。\n"
                 f"> 这就是 NIGHT.md N4「无活不开机」的量化依据：钱花在**开机**上，不是花在 token 上。")
        text += "\n\n" + breakeven_table(r)
    if a.write:
        dst = Path(a.write) if Path(a.write).is_absolute() else ROOT / a.write
        dst.write_text(text + "\n", encoding="utf-8")
        print(f"已写 {dst}")
    print(json.dumps(r, ensure_ascii=False, indent=1) if a.json else text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
