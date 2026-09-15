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
            k["go"] = json.load(open(auth))["opencode-go"]["key"]
        except Exception:
            pass
    return k


# ---------- 账本 ----------
def append_ledger(kind, cny, category, note):
    new = not LEDGER.exists()
    with open(LEDGER, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts", "kind", "amount_cny", "category", "note"])
        w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), kind, f"{cny:.4f}", category, note[:200]])


def ledger_rows():
    if not LEDGER.exists():
        return []
    with open(LEDGER) as f:
        return list(csv.DictReader(f))


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
    day = time.strftime("%Y-%m-%d")
    return round(sum(float(r["amount_cny"]) for r in ledger_rows() if r["ts"].startswith(day)), 4)


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
