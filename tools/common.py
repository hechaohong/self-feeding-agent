"""adventure 公共库：路径/配置/账本/状态。所有工具都从这里取，别各写一份。"""
import csv
import fcntl
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONF = ROOT / "config"
LEDGER = ROOT / "ledger.csv"
STATE = ROOT / "state" / "STATE.json"
ALERTS = ROOT / "state" / "ALERTS.json"
TODO = ROOT / "TODO.json"
JOURNAL = ROOT / "journal"
LOGS = ROOT / "logs"


def role():
    """host=宿主(有浏览器/GPU/searxng)；phone=手机侧(只有纯 HTTP)。

    手机侧由 tools/phone_deploy.sh 落一个 .phone 标记，不靠环境变量（沙箱会重建）。
    """
    if os.environ.get("ADVENTURE_ROLE"):
        return os.environ["ADVENTURE_ROLE"]
    return "phone" if (ROOT / ".phone").exists() else "host"


def cfg(name, default=None):
    p = CONF / f"{name}.json"
    if not p.exists():
        return default if default is not None else {}
    try:
        return json.load(open(p))
    except Exception:
        return default if default is not None else {}


def keys():
    k = cfg("keys", {})
    auth = os.path.expanduser("~/.pi/agent/auth.json")
    if os.path.exists(auth):
        try:
            a = json.load(open(auth))
            k["go"] = a["opencode-go"]["key"]
            if (a.get("openrouter") or {}).get("key"):
                k["openrouter"] = a["openrouter"]["key"]
        except Exception:
            pass
    return k


def pi_bin() -> str:
    """pi CLI 的绝对路径。**别再裸调 "pi"。**

    坑（09-17 NIGHT1 事故，代价 ¥0.44 + 空转 6.29h）：cron 的 PATH 里没有
    ~/.npm-global/bin，脚本裸调 "pi" → FileNotFoundError：
      · night.py 第 1 个 job 就抛异常、进程整个死掉 → llama-server 无人停
      · tools/retro.sh（03:00）和 tools/action.sh（19:05）静默失败 2 天（exit=127）
    """
    import shutil
    p = shutil.which("pi")
    if p:
        return p
    # ⚠️ 公开仓库惯例（09-27）：不把本机绝对路径发出去。
    # 原来第二个候选写死了 /home/...，现在只走 $HOME —— 本机和公开版同一份代码。
    for c in (Path.home() / ".npm-global" / "bin" / "pi",):
        if c.exists():
            return str(c)
    return "pi"


# ---------- 账本 ----------
LEDGER_COLS = ["ts", "kind", "amount_cny", "category", "note", "machine_min"]


def _ensure_ledger_schema():
    """旧账本（5 列）就地升级成 6 列（加 machine_min）。幂等、带锁、只加不删。

    为什么要这一列（Day 9 承诺 / 09-24 用户点选）：只记 API 花费的账本，会把
    「花 8.4 小时编译换 +4%」记成 ¥0.00 —— 会计上没错，决策上是瞎的。
    口径：只记**原始分钟数**，不乘时薪系数（乘了就是我自己编一个数）。
    """
    if not LEDGER.exists():
        return False

    def _do():
        with open(LEDGER, newline="") as f:
            rows = list(csv.reader(f))
        if not rows or "machine_min" in rows[0]:
            return False
        head = rows[0] + ["machine_min"]
        out = [head]
        for r in rows[1:]:
            out.append((r + ["0"] * len(head))[:len(head)])
        tmp = LEDGER.with_suffix(".csv.tmp")
        with open(tmp, "w", newline="") as f:
            csv.writer(f).writerows(out)
        tmp.replace(LEDGER)
        return True

    return _locked("ledger", _do)


def append_ledger(kind, cny, category, note, ts=None, machine_min=0.0):
    """记一笔。machine_min = 机器被占用的**分钟数**（原始值，不折算成钱）。"""
    _ensure_ledger_schema()
    new = not LEDGER.exists()
    with open(LEDGER, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(LEDGER_COLS)
        w.writerow([ts or time.strftime("%Y-%m-%dT%H:%M:%S"), kind, f"{cny:.4f}",
                    category, note[:200], f"{float(machine_min):.1f}"])


def ledger_rows():
    if not LEDGER.exists():
        return []
    _ensure_ledger_schema()
    with open(LEDGER) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        if not r.get("machine_min"):
            r["machine_min"] = "0"
    return rows


def machine_minutes(ym=None, day=None):
    """机器时间合计（分钟）。ym='2026-09' 按月；day='2026-09-24' 按天；都不传=全部。"""
    tot = 0.0
    for r in ledger_rows():
        v = float(r.get("machine_min") or 0)
        if not v:
            continue
        ts = r.get("ts", "")
        if ym and not ts.startswith(ym):
            continue
        if day and not ts.startswith(day):
            continue
        tot += v
    return tot


def totals(ym=None):
    """返回 (本月收入, 本月支出)；ym=None 表示全部"""
    inc = exp = 0.0
    for r in ledger_rows():
        if ym and not r["ts"].startswith(ym):
            continue
        v = float(r["amount_cny"])
        if r["kind"] == "income":
            inc += v
        else:
            exp += v
    return inc, exp


def this_month():
    return time.strftime("%Y-%m")


def budget():
    """返回 (本月剩余可用额度, 上限, 已花)

    规则（分阶段）：
      · 无收入 → 启动期额度 bootstrap_cap_cny（试错许可，用户 09-15 授权提高）
      · 有收入 → min(monthly_cloud_cap_cny, 收入 × spend_ratio_of_income)
    已花 = 本月 api/cloud/session-api 三类支出（会话费也是钱，必须计入闸门）
    """
    e = cfg("econ", {})
    cap = float(e.get("monthly_cloud_cap_cny", 70))
    boot = float(e.get("bootstrap_cap_cny", 30))
    inc, exp = totals(this_month())
    ratio = float(e.get("spend_ratio_of_income", 0.3))
    allowed = min(cap, inc * ratio) if inc > 0 else boot
    api_spent = sum(float(r["amount_cny"]) for r in ledger_rows()
                    if r["ts"].startswith(this_month())
                    and r["category"] in ("api", "cloud", "session-api"))
    return round(allowed - api_spent, 4), round(allowed, 2), round(api_spent, 4)


def phase():
    """当前处于哪个阶段：bootstrap（无收入）/ earn（有收入）"""
    inc, _ = totals(this_month())
    return "earn" if inc > 0 else "bootstrap"


def spent_today():
    """今日**支出**合计。

    坑（09-25 发现）：旧实现把当天所有行都加起来，**收入行会把支出冲小**
    （09-24 那条 ¥1.00 收入让当天"已花"少算 ¥1；作废那笔 -¥1.00 直接变成负数）。
    已花就该只算花出去的：kind=expense，不含 income/note/machine。
    """
    day = time.strftime("%Y-%m-%d")
    return round(sum(float(r["amount_cny"]) for r in ledger_rows()
                     if r["ts"].startswith(day) and r.get("kind") == "expense"), 4)


def spent_today_api(cats=("api", "cloud", "session-api")):
    """今日 **api 类**支出（R2 的当日降级上限用它，不是 spent_today()）。

    区别（09-27 加）：spent_today() 把当天所有 expense 都算上（含 power 本机电费、
    machine 类开销），会把「云调用降级额度」和本机开销混进同一个分母，导致误拒。
    R2 说的是「当日 ≤¥0.30 的降级调用」，限的只是云端调用 ⇒ 分母只能取 api 类。
    两条分母别混。
    """
    day = time.strftime("%Y-%m-%d")
    return round(sum(float(r["amount_cny"]) for r in ledger_rows()
                     if r["ts"].startswith(day) and r.get("kind") == "expense"
                     and r.get("category") in cats), 4)


def privacy_gate(path, verbose=True):
    """发布前隐私硬闸门：有 BLOCK 级命中返回 False（发布脚本必须调用）"""
    import privacy_check  # 延迟导入，避免 common ↔ privacy_check 循环
    return privacy_check.gate(path, verbose=verbose)


def state_read():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def state_write(d):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    json.dump(d, open(tmp, "w"), ensure_ascii=False, indent=1)
    tmp.replace(STATE)


def _locked(name, fn):
    p = ROOT / "state" / f".{name}.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        return fn()


def alerts_read():
    try:
        return json.load(open(ALERTS))
    except Exception:
        return []


def alert_add(msg, level="info"):
    def _do():
        a = alerts_read()
        a.append({"ts": time.strftime("%Y-%m-%d %H:%M"), "level": level, "msg": msg})
        json.dump(a, open(ALERTS, "w"), ensure_ascii=False, indent=1)
    return _locked("alerts", _do)


def alerts_clear():
    json.dump([], open(ALERTS, "w"))


def alert_add_if_new(msg, level="warn", within_min=360):
    """带**原子**去重的告警写入（读-判-写都在同一把锁里）。

    事故（09-19 复盘 D3）：guard.py（cron */30）与 collect.py→guard 在同一分钟并发，
    两边都读到空快照、都 append → 同一条故障在 ALERTS 里出现两次。
    old alert_add 的去重在锁外（先 alerts_read 再决定）→ 并发下形同没有。
    """
    def _do():
        a = alerts_read()
        cutoff = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - within_min * 60))
        if any(x.get("msg") == msg and x.get("ts", "") >= cutoff for x in a):
            return False
        a.append({"ts": time.strftime("%Y-%m-%d %H:%M"), "level": level, "msg": msg})
        json.dump(a, open(ALERTS, "w"), ensure_ascii=False, indent=1)
        return True
    return _locked("alerts", _do)


def alert_resolve(prefixes, keep=()):
    """闭环：清除**已恢复**的告警（返回清除条数）。

    事故（09-19 复盘 D2）：07:00 的假告警到 07:30 已恢复（loop=30min），ALERTS 里仍挂着——
    告警只增不减 = 看的人逐渐忽略 = 11.5h 事故的同一类病。
    只清 prefixes 里声明的“归我管”的告警，其它工具写的（夜间班等）不动。
    keep= 本轮仍然成立的说法，保留。
    """
    keep = set(keep)

    def _do():
        a = alerts_read()
        rest = [x for x in a
                if not (any(str(x.get("msg", "")).startswith(p) for p in prefixes)
                        and x.get("msg") not in keep)]
        n = len(a) - len(rest)
        if n:
            json.dump(rest, open(ALERTS, "w"), ensure_ascii=False, indent=1)
        return n
    return _locked("alerts", _do)


def journal(msg, tag="note"):
    JOURNAL.mkdir(parents=True, exist_ok=True)
    f = JOURNAL / (time.strftime("%Y-%m-%d") + ".md")
    with open(f, "a") as fh:
        fh.write(f"- [{time.strftime('%H:%M')}] ({tag}) {msg}\n")
    return str(f)


def tail_journal(n=5):
    JOURNAL.mkdir(parents=True, exist_ok=True)
    files = sorted(JOURNAL.glob("20*.md"))
    lines = []
    for f in files[-7:]:
        lines += [l.rstrip() for l in open(f) if l.strip()]
    return lines[-n:]
