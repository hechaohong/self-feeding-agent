#!/usr/bin/env python3
"""账本：收入/支出/电费/api 折算，一条命令看全部。

  tools/ledger.py income 50 "一单字幕校对"
  tools/ledger.py expense 3.5 "服务器" --cat other
  tools/ledger.py api 0.0231 "mimo-v2.5 in=12k out=1.5k"     # 已折算成 ¥
  tools/ledger.py power 2.5                                   # 本地推理 2.5 小时电费
  tools/ledger.py status                                      # 仪表盘
  tools/ledger.py tail 10
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("income", "expense", "api", "power"):
        p = sub.add_parser(name)
        p.add_argument("amount", type=float)
        p.add_argument("note", nargs="?", default="")
        p.add_argument("--cat", default=None)
    p = sub.add_parser("status")
    p = sub.add_parser("tail")
    p.add_argument("n", nargs="?", type=int, default=10)
    a = ap.parse_args()
    e = common.cfg("econ", {})

    if a.cmd == "income":
        common.append_ledger("income", a.amount, a.cat or "gig", a.note)
    elif a.cmd == "expense":
        common.append_ledger("expense", a.amount, a.cat or "other", a.note)
    elif a.cmd == "api":
        common.append_ledger("expense", a.amount, "api", a.note)
    elif a.cmd == "power":
        kwh = a.amount * float(e.get("gpu_extra_w", 200)) / 1000.0
        cny = kwh * float(e.get("price_kwh", 0.6))
        common.append_ledger("expense", cny, "power",
                             f"本地推理 {a.amount}h × {e.get('gpu_extra_w')}W × ¥{e.get('price_kwh')}/kWh")
        print(f"电费 ¥{cny:.3f} ({kwh:.2f} kWh)")
        return 0

    if a.cmd == "tail":
        for r in common.ledger_rows()[-a.n:]:
            print(f"{r['ts']} {r['kind']:7s} ¥{float(r['amount_cny']):9.4f} {r['category']:6s} {r['note'][:60]}")
        return 0

    inc, exp = common.totals()
    minc, mexp = common.totals(common.this_month())
    left, allowed, spent = common.budget()
    print(f"现金余额 ¥{inc - exp:.2f}   (总收入 ¥{inc:.2f} / 总支出 ¥{exp:.2f})")
    print(f"本月  收入 ¥{minc:.2f}  支出 ¥{mexp:.2f}  净 ¥{minc - mexp:.2f}")
    print(f"预算  {common.phase()} 阶段｜本月额度 ¥{allowed:.2f} (已花 ¥{spent:.2f}, 剩余 ¥{left:.2f})")
    print(f"闸门  {'✅ 可付费' if left > 0 else '⛔ 已封（改用 free 档）'}｜今日已花 ¥{common.spent_today():.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
