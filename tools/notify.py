#!/usr/bin/env python3
"""通知：email 优先（Resend），失败落 logs/outbox（微信通道不可靠，email 更稳）。

（公开仓库消毒版：发件人/收件人只从 config/notify.json 或环境变量取，仓库里不写真实地址。）

  echo "正文" | tools/notify.py --subject "日报 09-15"
  tools/notify.py --subject "PING" "短消息"
配置 config/notify.json: {"to":[...], "from":"...", "min_interval_min":30}
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

RESEND = "https://api.resend.com/emails"
LAST = common.ROOT / "state" / "last_notify.json"


def _throttle(min_interval):
    """防止刷屏：同类型通知最短间隔"""
    try:
        d = json.load(open(LAST))
        if time.time() - d.get("ts", 0) < min_interval * 60:
            return True
    except Exception:
        pass
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("body", nargs="?", default="")
    ap.add_argument("--subject", default="adventure 通知")
    ap.add_argument("--attach", action="append", default=[], help="附件路径（可多次）")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-email", action="store_true")
    a = ap.parse_args()
    body = a.body or sys.stdin.read()
    c = common.cfg("notify", {})
    if not a.force and _throttle(int(c.get("min_interval_min", 30))):
        print("[notify] 节流跳过", file=sys.stderr)
        return 0

    ok, err = False, ""
    # 环境变量优先（宿主 shell），否则读 config/keys.json —— 手机侧 cron/ssh 没有宿主 env
    key = os.environ.get("RESEND_API_KEY", "") or common.keys().get("resend", "")
    if key and not a.no_email:
        payload = {"from": c.get("from") or os.environ.get("NOTIFY_FROM", ""),   # 消毒：真实发件人放 config/notify.json
                   "to": c.get("to", []), "subject": a.subject, "text": body}
        atts = []
        import base64 as _b64
        for p in a.attach:
            try:
                atts.append({"filename": os.path.basename(p),
                             "content": _b64.b64encode(open(p, "rb").read()).decode()})
            except Exception as ex:
                err += f" 附件失败 {p}: {ex}"
        if atts:
            payload["attachments"] = atts
        req = urllib.request.Request(RESEND, data=json.dumps(payload).encode(),
                                     headers={"Authorization": "Bearer " + key,
                                              "Content-Type": "application/json",
                                              "User-Agent": "adventure/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as f:
                r = json.load(f)
            ok = bool(r.get("id"))
        except urllib.error.HTTPError as ex:
            err = f"{ex.code} {ex.read()[:200].decode(errors='replace')}"
        except Exception as ex:
            err = f"{type(ex).__name__} {ex}"

    out = common.LOGS / "outbox"
    out.mkdir(parents=True, exist_ok=True)
    f = out / (time.strftime("%Y%m%d-%H%M%S") + ".txt")
    f.write_text(f"SUBJECT: {a.subject}\nTO: {c.get('to')}\nEMAIL_OK: {ok} {err}\n\n{body}")
    LAST.parent.mkdir(parents=True, exist_ok=True)
    json.dump({"ts": time.time()}, open(LAST, "w"))
    print(f"[notify] email={'OK' if ok else 'FAIL ' + err}  落盘={f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
