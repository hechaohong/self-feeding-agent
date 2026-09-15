#!/usr/bin/env python3
"""发文前隐私体检（发布脚本的硬闸门）。

  python3 tools/privacy_check.py            # 检查 content/ 全部
  python3 tools/privacy_check.py art.md     # 检查单个
  python3 tools/privacy_check.py --exit     # 有 BLOCK 级命中也返回 1

分级：
  BLOCK  = 真实身份 / 凭据 / 本机隐私（邮箱、手机、真名、API key、token、uuid、家庭路径、内网 IP…）
  WARN   = fine 但可能暴露环境（localhost:端口、通用 IP），只提醒不拦

发布脚本用 `common.privacy_gate(path)` 调用：BLOCK 直接中止发布。
例外白名单：example.com / example.org 域名的邮箱（文档演示用）。
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

# (级别, 名称, 正则)
PATTERNS = [
    ("BLOCK", "邮箱", r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}"),
    ("BLOCK", "手机号", r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    ("BLOCK", "API密钥", r"(sk-[A-Za-z0-9_\-]{12,}|Bearer\s+[A-Za-z0-9_\-\.]{16,}|AKID[A-Za-z0-9]{10,})"),
    ("BLOCK", "身份证", r"(?<!\d)\d{17}[\dXx](?!\d)"),
    ("BLOCK", "银行卡", r"(?<!\d)\d{16,19}(?!\d)"),
    ("BLOCK", "本机路径", r"(/home/[a-z0-9_]+|/data\d/[a-zA-Z_-]+|C:\\\\Users\\\\[^\\\\\s]+|/Users/[a-z0-9_]+)"),
    ("BLOCK", "内网IP", r"(192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)"),
    ("BLOCK", "会话/令牌", r"(_xsrf\s*[=:]\s*\S{6,}|session[_-]?id\s*[=:]\s*['\"]?[A-Za-z0-9_\-\.]{8,}|uuid=[A-Za-z0-9]{8,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"),
    ("BLOCK", "开放平台ID", r"(uname=\d+|wid=\d+|抖音号[:：]?\s?\d{6,})"),
    ("BLOCK", "微信/QQ", r"([Qq][Qq][:：]\s?\d{5,}|微信号[:：]?\s?[A-Za-z0-9_-]{5,})"),
    ("WARN", "本地服务", r"(127\.0\.0\.1:\d+|localhost:\d+)"),
    ("WARN", "用户名提及", r"/(?:home|Users)/"),
]

ALLOW = re.compile(r"@(example\.(com|org)|test\.com)")


def identity_values():
    """config/identity.json 的标量值 → 禁词（不改文件、不回显内容）"""
    out = []
    try:
        d = json.load(open(common.CONF / "identity.json"))
    except Exception:
        return out
    stack = [d]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
        elif isinstance(o, str) and len(o) >= 4:
            out.append(o)
    return out


def check(path, extra=None):
    """返回 [(level, kind, snippet, lineno)]"""
    extra = identity_values() if extra is None else extra
    txt = Path(path).read_text()
    hits = []
    for level, kind, pat in PATTERNS:
        for m in re.finditer(pat, txt):
            s = m.group(0)
            if kind == "邮箱" and ALLOW.search(s):
                continue
            hits.append((level, kind, s[:40], txt[:m.start()].count("\n") + 1))
    for v in extra:
        for m in re.finditer(re.escape(v), txt):
            hits.append(("BLOCK", "身份禁词", v[:4] + "***", txt[:m.start()].count("\n") + 1))
    return hits


def gate(path, verbose=True):
    """发布前闸门：有 BLOCK 返回 False。"""
    hits = check(path)
    blocks = [h for h in hits if h[0] == "BLOCK"]
    warns = [h for h in hits if h[0] == "WARN"]
    if verbose:
        for level, kind, s, ln in blocks[:20]:
            print(f"    ⛔ L{ln:<4} [{kind}] {s}")
        for level, kind, s, ln in warns[:8]:
            print(f"    ⚠️  L{ln:<4} [{kind}] {s}")
        print(f"    隐私闸门: BLOCK {len(blocks)} 处 / WARN {len(warns)} 处")
    return not blocks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?")
    ap.add_argument("--exit", action="store_true")
    a = ap.parse_args()

    files = [Path(a.file)] if a.file else sorted((common.ROOT / "content").glob("*.md"))
    bad = 0
    for f in files:
        if f.name == "INDEX.md":
            continue
        hits = check(f)
        nb = sum(1 for h in hits if h[0] == "BLOCK")
        nw = sum(1 for h in hits if h[0] == "WARN")
        bad += nb
        flag = "✅" if nb == 0 else "❌"
        print(f"{flag} {f.name}: BLOCK {nb} / WARN {nw}")
        seen = set()
        for level, kind, s, ln in hits:
            if (kind, s) in seen:
                continue
            seen.add((kind, s))
            mark = "⛔" if level == "BLOCK" else "⚠️ "
            print(f"    {mark} L{ln:<4} [{kind}] {s}")
    print(f"\n禁词源 identity.json ｜ BLOCK 合计 {bad} 处")
    if a.exit and bad:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
