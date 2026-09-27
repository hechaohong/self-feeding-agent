#!/usr/bin/env python3
"""结算 pi --mode json 的花费 → ledger.csv，并把最终回答摘出来给人看。

  python3 tools/pi_cost.py logs/action-2026-09-15.log --tag action
  python3 tools/pi_cost.py <log> --dry        # 只算不记账
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--tag", default="action")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--from", dest="offset", type=int, default=0,
                    help="从日志的字节偏移开始解析（避免把上一次 tick 的花费重复计入）")
    ap.add_argument("--print-text", action="store_true", default=True)
    ap.add_argument("--verdict", action="store_true",
                    help="只输出 ok=0/1（exit code 同步），用于判断这次 tick 是否真的跑成了")
    a = ap.parse_args()

    usd = 0.0
    tin = tout = 0
    models = set()
    texts = []
    ntool = 0
    errored = False
    emsg = ""
    with open(a.log, errors="replace") as fh:
        fh.seek(a.offset)
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            for obj in (d, d.get("message") if isinstance(d.get("message"), dict) else {}):
                u = obj.get("usage") if isinstance(obj, dict) else None
                if isinstance(u, dict) and "cost" in u and isinstance(u["cost"], dict):
                    usd += float(u["cost"].get("total", 0) or 0)
                    tin += int(u.get("input", 0) or 0)
                    tout += int(u.get("output", 0) or 0)
                    if obj.get("model"):
                        models.add(str(obj["model"]))
            mm = d.get("message") if isinstance(d.get("message"), dict) else {}
            if d.get("type") == "tool_execution_start":
                ntool += 1
            if mm.get("role") == "assistant":
                if mm.get("stopReason") == "error":
                    errored = True
                    emsg = str(mm.get("errorMessage", ""))[:120]
                for c in mm.get("content", []) or []:
                    if isinstance(c, dict) and c.get("type") == "text" and (c.get("text") or "").strip():
                        texts.append(c["text"])

    fx = float(common.cfg("econ", {}).get("fx_usd_cny", 7.1))
    cny = usd * fx
    ok = 1 if (ntool >= 3 or texts) else 0
    if a.verdict:
        print(f"ok={ok} toolcalls={ntool} texts={len(texts)} err={'yes' if errored else 'no'} {emsg}")
        # ⚠️ 坑（09-27 找到）：原来写的是 `return 1 if ok else 0`——**极性反了**。
        # shell 语义里 0 才是成功。原写法让「跑成了」退出 1、「没跑成」退出 0。
        # 为什么 12 天没人发现：action.sh 那时写的是
        #     python3 tools/pi_cost.py ... | tail -1 ; return $?
        # 管道里 `$?` 取的是 **tail** 的退出码（恒 0），把这里的反极性**完全盖住**了。
        # 两个 bug 互相遮掩：先修了管道这行，反极性才会暴露出来。
        # R27：判活看产出，不看进程死没死——ok 的判据是 toolcalls/texts。
        return 0 if ok else 1
    print(f"[pi_cost] models={','.join(sorted(models)) or '-'} in={tin} out={tout} "
          f"toolcalls={ntool} ${usd:.5f} = ¥{cny:.4f}" + (f"  ERROR: {emsg}" if errored else ""))
    if texts:
        tail = "\n".join(texts[-1].strip().splitlines()[-8:])
        print("---- 最终输出 ----\n" + tail)
    if not a.dry and cny > 0:
        common.append_ledger("expense", cny, "api",
                             f"pi-tick({a.tag}) {','.join(sorted(models))} in={tin} out={tout}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
