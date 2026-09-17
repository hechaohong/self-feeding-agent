#!/usr/bin/env python3
"""守卫 tick：不调 LLM（¥0）。查健康/预算/待办，异常才举手（写 ALERTS + NEED_ACTION）。

  tools/guard.py            # 跑一次，输出一行状态
  tools/guard.py --json
cron: */30 * * * *  —— 便宜、无声、只在异常时产生告警。
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

FLAG = common.ROOT / "state" / "NEED_ACTION"


def port_ok(p, host="127.0.0.1", t=1.5):
    try:
        with socket.create_connection((host, p), timeout=t):
            return True
    except Exception:
        return False


# ---------- 常驻循环保活（09-18 事故的兜底） ----------
# 事故：手机 minis 从 09-17 19:25 起 sshd 拒绝连接（**ping 通**，设备在线但 PRoot 沙箱挂了），
# 常驻循环停摆 11.5h 无人知道 —— 因为告警只写在 ALERTS.json 里，而看它的人（日报）也停了。
# 规矩：判活必须用**直连探测**，不能只看心跳文件（心跳由宿主 cron 拉取，宿主自己离线时滞后，会误报）。
PHONE_HOST = os.environ.get("LOOP_HOST", "127.0.0.1")   # 消毒：常驻循环所在主机
PHONE_PORT = int(os.environ.get("LOOP_PORT", "22"))      # 消毒：sshd 端口


def loop_probe(t=3.0):
    """常驻循环（手机沙箱）是否活着：sshd 端口能否连上。"""
    try:
        with socket.create_connection((PHONE_HOST, PHONE_PORT), timeout=t):
            return True
    except Exception:
        return False


def loop_job_age_min():
    """常驻循环最后一次 job 距今多少分钟（手机在跑但 job 没跑 = 静默失败）。

    09-18 复盘：三类事故（宿主离线/沙箱挂/兜底未挂 cron）共同特征是**静默**——
    进程没报错、日志没内容、钱也没花，看起来一切正常。所以判活要看"产出"。
    """
    try:
        d = json.loads((common.ROOT / "state" / "lastrun.json").read_text())
        ts = max(float(v.get("ts") or 0) for v in d.values() if isinstance(v, dict))
        return (time.time() - ts) / 60 if ts else None
    except Exception:
        return None


def host_uptime_min():
    try:
        return float(open("/proc/uptime").read().split()[0]) / 60
    except Exception:
        return 999.0


def gpu():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        u, mu, mt = [x.strip() for x in out.split(",")]
        return {"util": int(u), "mem_used": int(mu), "mem_total": int(mt), "busy": int(mu) > 8000}
    except Exception as e:
        return {"err": str(e)[:60]}


# ---------- 僵尸 llama-server 看门狗（09-17 NIGHT1 事故的兜底） ----------
# 事故：night.py 抛异常死掉 → 没人执行 serve("stop")，llama-server 带着 17GB 显存
# 空转 6.29h（加载态实测 117W ≈ ¥0.44），产出 0。教训：硬停逻辑写在**会被崩掉的
# 那个进程**里 = 没有硬停。兜底必须在一个跟夜班无关的进程里 —— 就是这里（cron */30）。
_N = common.cfg("night", {}) or {}
_WD = (_N.get("caps", {}) or {}).get("watchdog", {})
WD_MAX_UPTIME_H = float(_WD.get("max_uptime_h", 6.0))
WD_IDLE_KILL_MIN = float(_WD.get("idle_kill_min", 45))
WD_GRACE_MIN = float(_WD.get("grace_min", 15))
WD_STATE = common.ROOT / "state" / "llama_watch.json"
WD_KEEP = common.ROOT / "state" / "llama.keep"      # 手动使用 GPU 时的豁免开关


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def night_window_open() -> bool:
    """是否处在“允许 llama-server 开着”的时段（窗口开始 ~ hard_stop + grace）。"""
    w = _N.get("window", {"start": "00:10", "end": "05:40"})
    stop = _N.get("hard_stop", "05:55")
    t = time.localtime()
    return _hm(w["start"]) <= t.tm_hour * 60 + t.tm_min <= _hm(stop) + WD_GRACE_MIN


def _llama_log_state():
    """当前 llama-server 日志的 size/mtime —— 用它判断“有没有在干活”。

    服务在跑但日志长时间不增长 = 零请求（NIGHT1 就是这种僵尸态空转 6.29h）。
    比 /metrics 兼容性好：不看 llama.cpp 版本的指标名，任何版本都有日志。
    """
    logs = [p for p in common.LOGS.glob("llama-*.log") if p.is_file()]
    if not logs:
        return None
    p = max(logs, key=lambda x: x.stat().st_mtime)
    st = p.stat()
    return {"path": str(p), "size": st.st_size}


def llama_watchdog(chk) -> str:
    """僵尸 llama-server 看门狗（¥0，不调 LLM）。返回动作描述；无动作返回 ""。"""
    pidf = common.ROOT / "state" / "llama.pid"
    startf = common.ROOT / "state" / "llama.start"
    if not pidf.exists() and not startf.exists():
        WD_STATE.unlink(missing_ok=True)
        return ""
    if WD_KEEP.exists():
        return "llama.keep 存在 → 看门狗让位（手动在用 GPU）"

    alive = False
    pid = None
    if pidf.exists():
        try:
            pid = int(pidf.read_text().strip())
            os.kill(pid, 0)
            alive = True
        except Exception:
            alive = False

    uptime_h = 0.0
    if startf.exists():
        try:
            uptime_h = (time.time() - int(startf.read_text().strip())) / 3600
        except Exception:
            uptime_h = 0.0

    reason = ""
    if not alive:
        reason = "进程已死但 pid/start 文件残留（清掉；有 STARTF 就补记电费）"
    elif not chk["night_window"]:
        reason = f"已过夜间窗口仍开机 {uptime_h:.2f}h（N1 硬停兜底）"
    elif uptime_h > WD_MAX_UPTIME_H:
        reason = f"开机 {uptime_h:.2f}h 超过上限 {WD_MAX_UPTIME_H:.1f}h"
    else:
        st = _llama_log_state()
        if st:
            try:
                wd = json.loads(WD_STATE.read_text())
            except Exception:
                wd = {}
            if wd.get("path") == st["path"] and wd.get("size") == st["size"]:
                since = float(wd.get("since") or time.time())
                if (time.time() - since) / 60 >= WD_IDLE_KILL_MIN:
                    reason = (f"服务在跑但日志 {WD_IDLE_KILL_MIN:.0f} 分钟没增长"
                              f"（零请求空转，占空比 0%）")
                else:
                    wd["since"] = since
            else:
                wd = {"path": st["path"], "size": st["size"], "since": time.time()}
            WD_STATE.write_text(json.dumps(wd))

    if not reason:
        return ""
    subprocess.run(["tools/localserve.sh", "stop"], cwd=str(common.ROOT),
                   capture_output=True, text=True, timeout=180)
    WD_STATE.unlink(missing_ok=True)
    common.alert_add(f"看门狗停机 llama-server：{reason}", "warn")
    common.journal(f"🛑 看门狗停机 llama-server：{reason}（up {uptime_h:.2f}h, pid={pid}）", tag="guard")
    return f"🛑 watchdog 停 llama-server：{reason}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    st = common.state_read()
    inc, exp = common.totals()
    left, allowed, spent = common.budget()
    try:
        todo = json.load(open(common.TODO)).get("todo", [])
    except Exception:
        todo = []
    ntodo = len([t for t in todo if t.get("status") != "done"])
    alerts = common.alerts_read()
    jtail = common.tail_journal(1)
    last_j = jtail[0][:10] if jtail else "none"
    hour = time.localtime().tm_hour

    chk = {
        "searxng": port_ok(8888),
        "llama_local": port_ok(8080),
        "ollama": port_ok(11434),
        "gpu": gpu(),
        "budget_left": left,
        "budget_allowed": allowed,
        "alerts": len(alerts),
        "todo_open": ntodo,
        "valley_hours": hour >= 22 or hour < 7,
        "night_window": night_window_open(),
    }
    wd_action = llama_watchdog(chk) if common.role() == "host" else ""
    problems = []
    # 保活：刚开机的前 3 分钟不判活（局域网/网卡还没起，会误报）。
    # 用直连探测而不是心跳文件，所以不需要等 sync 拉取。
    if common.role() == "host" and host_uptime_min() > 3 and not loop_probe():
        problems.append(f"常驻循环失联：{PHONE_HOST}:{PHONE_PORT} 连不上（ping 通 = 设备在线但沙箱挂了）")
    elif common.role() == "host":
        age = loop_job_age_min()
        if age is not None and age > 180 and host_uptime_min() > age:
            problems.append(f"常驻循环活着但 {age:.0f} 分钟没跑过任何 job（静默失败）")
    if not chk["searxng"] and common.role() == "host":
        # 手机侧没有 searxng/llama，这不是故障，是分工（R22）。在手机上照实报会把告警刷满。
        problems.append("searxng 挂了（搜索能力丧失）")
    if left <= 0:
        problems.append("预算已耗尽：只能走 free 档 / 本地")
    elif allowed and left / allowed < 0.2:
        problems.append(f"预算仅剩 {left:.2f}/{allowed}")
    if ntodo == 0:
        problems.append("待办为空：需要找新的赚钱线索")
    for p in problems:
        common.alert_add(p, "warn")
    # 保活/钱的告警必须推给人：只写 ALERTS.json = 只有下一个日报才看得到（11.5h 的教训）。
    # 节流在 notify.py 里（min_interval_min），这里不重复实现。
    crit = [p for p in problems if any(k in p for k in ("失联", "预算已耗尽", "看门狗", "僵尸"))]
    if crit and common.role() == "host":
        subprocess.run([sys.executable, str(common.ROOT / "tools" / "notify.py"),
                        "--subject", "🔴 adventure 保活告警",
                        "; ".join(crit) + f" ｜ 余额¥{inc - exp:.2f} ｜ {time.strftime('%F %T')}"],
                       cwd=str(common.ROOT), capture_output=True, text=True, timeout=120)
    # 有告警 或 待办 >=1 时，允许行动 tick 干活（避免空转烧钱）
    need = bool(problems) or ntodo > 0
    if need:
        FLAG.write_text(json.dumps({"ts": time.strftime("%F %T"), "reasons": problems[:3]}))
        if FLAG.exists() and not problems:
            FLAG.unlink()

    line = (f"{time.strftime('%F %T')} guard role={common.role()} searxng={int(chk['searxng'])} "
            f"local={int(chk['llama_local'])} "
            f"gpu={chk['gpu'].get('util')}%/{chk['gpu'].get('mem_used')}M 余额¥{inc-exp:.2f} "
            f"额度剩¥{left:.2f} 待办{ntodo} 告警{len(alerts)} 谷时={int(chk['valley_hours'])}"
            + (f" ｜ {wd_action}" if wd_action else ""))
    common.LOGS.mkdir(parents=True, exist_ok=True)
    with open(common.LOGS / "guard.log", "a") as f:
        f.write(line + "\n")
    # 日志轮转：只留最后 2000 行
    gf = common.LOGS / "guard.log"
    ls = gf.read_text().splitlines()
    if len(ls) > 2000:
        gf.write_text("\n".join(ls[-1000:]) + "\n")
    print(json.dumps(chk, ensure_ascii=False) if a.json else line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
