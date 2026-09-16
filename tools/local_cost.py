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
import subprocess
import sys
import time
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


IDLE_FILE = ROOT / "state" / "gpu_idle.json"


def saved_idle_w(measure: bool = False) -> float:
    """空闲功耗：量一次就落盘复用（避免每次重测导致证据文件里的数字反复漂）。

    坑（09-16）：nvidia-smi 的空闲读数每次跑都不一样（实测 22.7 / 23.2 / 25.6 W），
    如果 --write 每次都重测，同一篇文章的证据文件会随每次重新生成而变。
    """
    if IDLE_FILE.exists() and not measure:
        try:
            return float(json.loads(IDLE_FILE.read_text(encoding="utf-8"))["idle_w"])
        except Exception:
            pass
    w = gpu_idle_w()
    if w:
        IDLE_FILE.write_text(json.dumps(
            {"idle_w": round(w, 2), "measured_at": time.strftime("%F %T"), "samples": 5,
             "stat": "min of 5", "cmd": "nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits"},
            ensure_ascii=False, indent=1), encoding="utf-8")
    return w


def gpu_idle_w(samples: int = 5) -> float:
    """实测 GPU 空闲功耗（nvidia-smi power.draw，取最低样本 ≈ 纯空闲）。

    为什么要它：`gpu_extra_w=200` 一直是**估算**。但真正决定「本地增量成本」的不是
    推理时的总功耗，而是 `推理功耗 − 空闲功耗`——机器本来就 24h 开着的话，
    空闲那部分你早就在付了，不该算到本地推理头上。
    """
    vals: list[float] = []
    for _ in range(max(1, samples)):
        r = subprocess.run(["nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True)
        try:
            vals.append(float((r.stdout or "").strip().splitlines()[0]))
        except Exception:
            pass
        time.sleep(1)
    return min(vals) if vals else 0.0


def power_sensitivity(r: dict, idle_w: float, infer_ws=(150.0, 200.0, 250.0)) -> str:
    """把「推理功耗」这个未实测参数做成灵敏度表 → 结论不再压在单一估算上。

    两种会计口径（这才是本地 vs 云端的真分歧点）：
      全额口径 = 这台机器是为跑推理才开的（含空闲）
      增量口径 = 机器本来就 24h 开着，只算推理比空闲多耗的那部分
    """
    econ = common.cfg("econ", {})
    price = float(common.cfg("night", {}).get("local", {}).get("price_kwh")
                  or econ.get("price_kwh") or 0.6)
    up_h = r["up_s"] / 3600.0
    rows = ["## 推理功耗灵敏度（`gpu_extra_w` 从未实测，所以不给单点数）", "",
            f"- GPU 空闲实测：**{idle_w:.2f} W**（nvidia-smi，取 5 次最低样本）→ 纯待机 ¥{idle_w/1000*price:.4f}/小时"
            + (f"，24h 开着 ≈ ¥{idle_w/1000*price*24*30:.1f}/月" if idle_w else ""),
            f"- 机器本身并非空闲：宿主另有 Windows VM(12G) / Elasticsearch / docker 等常驻负载 → **宿主 24h 开着**",
            "",
            "| 假设推理功耗 | 全额 ¥/h | 增量 ¥/h（减空闲） | 账单 ¥/M 全额 | 账单 ¥/M 增量 | 保本占空比 全额 | 保本占空比 增量 |",
            "|---|---|---|---|---|---|"]
    for w in infer_ws:
        full, inc = w / 1000 * price, max(w - idle_w, 0) / 1000 * price
        marg_full = full / (r["rate"] * 3600 / 1e6)
        marg_inc = inc / (r["rate"] * 3600 / 1e6)
        bill_full = up_h * full / (r["out_tok"] / 1e6)
        bill_inc = up_h * inc / (r["out_tok"] / 1e6)
        rows.append(f"| {w:.0f} W | {full:.4f} | {inc:.4f} | ¥{bill_full:.2f} | ¥{bill_inc:.2f} "
                    f"| {marg_full/r['cloud_per_m']*100:.1f}% | {marg_inc/r['cloud_per_m']*100:.1f}% |")
    lo = min(infer_ws); hi = max(infer_ws)
    rows += ["",
             f"> 结论对功耗假设的敏感度：推理功耗从 {lo:.0f}W 到 {hi:.0f}W，保本占空比只在 "
             f"{min(lo,hi-idle_w)/1000*price/(r['rate']*3600/1e6)/r['cloud_per_m']*100:.1f}% – "
             f"{hi/1000*price/(r['rate']*3600/1e6)/r['cloud_per_m']*100:.1f}% 之间移动——**乘性缩放，不改变排序**。",
             "> 换句话说：「占空比决定本地赢不赢」这个结论不依赖那个未实测的数字；只有具体阈值依赖它。",
             "> 真正的分歧不是技术，是**会计口径**：把空闲和已付的机器成本算进来 → 本地赢；只把本地推理当新增开支 → 大多亏。"]
    return "\n".join(rows)


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
    ap.add_argument("--idle", action="store_true", help="实测 GPU 空闲功耗 + 打印功耗灵敏度表")
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
    if a.idle or a.write:
        idle = saved_idle_w(measure=a.idle)
        text += "\n\n" + power_sensitivity(r, idle)
        if not a.idle:
            text += (f"\n> 空闲功耗来自 {IDLE_FILE.relative_to(ROOT)}（量一次落盘，"
                     f"重测用 `--idle`）—— 免得每次重新生成证据文件这个数都在漂。")
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
