#!/usr/bin/env python3
"""把 pi 会话（含当前这个 session）的 API 花费计入账本。

  python3 tools/session_cost.py            # 结算增量并入账
  python3 tools/session_cost.py --dry      # 只看，不记账
  python3 tools/session_cost.py --status   # 只显示各会话累计

原理：session jsonl 里每条 assistant 消息带 usage.cost.total(USD)。
按文件维护检查点 state/session_costs.json，只把**增量**记进 ledger，避免重复计。
新调用的 session（例如本对话）只要写进 jsonl 就会被下次结算捞到。
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

SESS_DIR = Path(os.path.expanduser("~/.pi/agent/sessions"))
CKPT = common.ROOT / "state" / "session_costs.json"


def scan(path):
    """返回 (usd, in_tok, out_tok)"""
    usd = 0.0
    tin = tout = 0
    try:
        with open(path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                stack = [d]
                while stack:
                    o = stack.pop()
                    if isinstance(o, dict):
                        u = o.get("usage")
                        if isinstance(u, dict) and "cost" in u:
                            c = u["cost"]
                            usd += float(c.get("total") or 0) if isinstance(c, dict) else float(c or 0)
                            tin += int(u.get("input") or 0)
                            tout += int(u.get("output") or 0)
                        stack.extend(o.values())
                    elif isinstance(o, list):
                        stack.extend(o)
    except Exception:
        pass
    return usd, tin, tout


def sessions_for(project=None):
    pat = f"--{project}--" if project else "*"
    return sorted(SESS_DIR.glob(f"{pat}/*.jsonl"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="data2-adventure", help="会话目录前缀（默认本冒险项目）")
    ap.add_argument("--all", action="store_true", help="扫全部项目（会把别的项目的花费也算进来，慎用）")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()

    fx = float(common.cfg("econ", {}).get("fx_usd_cny", 7.1))
    ck = json.load(open(CKPT)) if CKPT.exists() else {}
    files = sessions_for(None if a.all else a.project)

    rows, d_usd = [], 0.0
    for f in files:
        usd, tin, tout = scan(f)
        prev = ck.get(str(f), {}).get("usd", 0.0)
        d = max(0.0, usd - prev)
        d_usd += d
        rows.append((f, usd, tin, tout, d))
        if not a.dry:
            ck[str(f)] = {"usd": usd, "in": tin, "out": tout}

    rows.sort(key=lambda r: -r[1])
    print(f"会话数 {len(files)} ｜ 本次新增花费 ${d_usd:.4f} ≈ ¥{d_usd * fx:.3f}")
    for f, usd, tin, tout, d in rows[:6]:
        if usd > 0:
            print(f"  {f.name[:36]:38s} 累计 ${usd:.4f} (¥{usd * fx:.2f}) 入{tin} 出{tout} 增量¥{d * fx:.3f}")

    if a.status or a.dry:
        return 0
    if d_usd > 0.00005:
        cny = round(d_usd * fx, 4)
        common.append_ledger("expense", cny, "session-api",
                             f"pi session 增量 ${d_usd:.4f} @{fx}（{len(files)} 个会话文件）")
        print(f"✅ 已入账 支出 ¥{cny}")
    else:
        print("无新增，不入账")
    json.dump(ck, open(CKPT, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
