#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""article_polish.py — 稿件优化 job（内容优先，自动改稿 + 验收 + 回滚）

为什么要它：今天我在两篇稿子上手工做了 4 遍同样的事（删重复段、补代码块、压重复数字、
修围栏、补开场白）——R6 同一动作第 2 次即固化成脚本。手工做这些必然漏，所以做成 job。

硬约束（违反即回滚，不留半成品）：
  1. **只碰未发布稿**：标题出现在 data/published.md 里 ⇒ 跳过（R28 已发布不回改）
  2. **禁止新增数字**：优化稿里的数字集合必须是原稿的子集。多一个数字 = 模型编的 = 回滚
  3. 保留 H1 前缀「自养Agent日志：」与开场白「我是自养Agent，这是生存游戏的第 N 天。」
  4. 配图引用与代码块数量不得减少
  5. 优化后跑双闸门（fact_check/privacy_check）+ article_lint；出现 BLOCK ⇒ 回滚
  6. 字数目标 3000–5000，±20%（2800–5500）不拦（09-27 用户定：关注内容本身）
  7. 原稿留底 <file>.prepolish.md；每次改动落 state/article_polish.jsonl（R24 验证记录）

用法：
  python3 tools/article_polish.py --list                 # 看有哪些未发布稿可优化
  python3 tools/article_polish.py <file> --dry           # 只看会怎么改，不落盘
  python3 tools/article_polish.py <file> --apply         # 真改（带验收+回滚）
  python3 tools/article_polish.py --pending --apply      # 优化 content/ 下所有未发布稿
  python3 tools/article_polish.py <file> --apply                  # 默认走 free 档（改法 C）
  python3 tools/article_polish.py <file> --apply --tier editor     # 强制走付费 editor 档

成本：每次优化 = 1 次 editor 档模型调用（¥0.0x 量级）。用 --max-calls 限流。
job：config/jobs.json 的 article_polish（每天一次，role=host，¥0~¥0.1）
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"
PUBLISHED = ROOT / "data" / "published.md"
STATE = ROOT / "state" / "article_polish.jsonl"
H1_PREFIX = "自养Agent日志："
OPEN_RE = re.compile(r"我是自养Agent，这是生存游戏的第\s*\d+\s*天。")
NUM_RE = re.compile(r"(?:¥\s?\d+(?:\.\d+)?|\d+(?:\.\d+)?\s?%|\d+\s?倍|\d+\s?天|\d+\s?篇)")
CN_RE = re.compile(r"[\u4e00-\u9fff]")

POLISH_SPEC = """你是中文技术博客的责任编辑。下面是一篇已经写好的稿子，请**只做编辑层面的优化**，不要改变立场、不要新增任何事实或数字。

【硬性禁止】
1. 禁止引入原文没有的数字、日期、金额、百分比、倍数——一个都不许加。
2. 禁止改 H1 标题文字、禁止改开头那句「我是自养Agent，这是生存游戏的第 N 天。」
3. 禁止删除任何 ![](...) 配图引用；禁止把代码块数量减少。
4. 禁止新增小节（可以合并、拆分、改名，但总数保持在 4–12 之间）。

【要做的优化（按优先级）】
A. 去重：同一组数字或同一个论点如果在小节里重复出现，只保留最完整的一处。
B. 结构：每节开头 3 行内给出该节结论；每节控制在 400–900 字。
C. 可复制性：正文里"读者可以直接照做"的部分，用可复制的命令或清单块承载（可把散在正文里的命令集中成代码块，但不要新增内容）。
D. 语气：去掉自夸和铺垫，保留具体的数字与自嘲；争议点小节必须保留"反对者会说"和"认错条件"。
E. 标点与格式：中文全角标点；表格不超过 8 行。

【输出】
直接输出优化后的完整 markdown（不要任何解释、不要 ```markdown 包裹、不要"以下是优化后"这类开场白）。
"""


TASK = "polish"   # 任务名：任务级黑名单用它


def llm_call(prompt, tier="free", timeout=600, max_tokens=8000, temp=0.4, task=TASK):
    """复用 tools/llm.py（与 write_article.py 同一条链路：prompt 走位置参数、人设走 --system）

    2026-09-27（改法 C）：tier 默认由 editor 改成 **free**，挂 --fallback-tier editor。
      · 改的理由：editor 档 deepseek-v4.1-flash 是 $0.15/$0.6 付费档，是本仓**唯一
        每天自动跑的付费模型 job**（config/jobs.json 里 enabled=true / 1440min）。
      · 不直接删付费兜底：免费池会掉（09-21 一天少 4 个模型），全挂时需要有地方去。
      · 付费兜底受 R2 clamp 约束（单次 ≤¥0.10 / 当日 ≤¥0.30，llm.py 里拦）。
      · --task 让 llm.py 剔掉「polish 任务上已拉黑」的模型（tools/blacklist.py）。
    """
    cmd = [sys.executable, str(ROOT / "tools" / "llm.py"),
           "--tier", tier, "--no-thinking",
           "--max-tokens", str(max_tokens), "--temperature", str(temp),
           "--timeout", str(timeout), "--task", task,
           "--fallback-tier", "editor" if tier == "free" else ""]
    pf = CONTENT / "persona.json"
    if pf.exists():
        cmd += ["--system", pf.read_text(encoding="utf-8")]
    cmd += [prompt]
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT), timeout=timeout + 60)
    out = (p.stdout or "").strip()
    if not out:
        raise RuntimeError(f"llm.py 无输出 rc={p.returncode}: {(p.stderr or '')[-200:]}")
    return re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", out).strip()


def _mark_model(result_text, ok, why=""):
    """把本次验收结果记到**实际使用的模型**头上（llm.py 写的 state/last_model.json）。

    没有这一步，黑名单就只能拉在「free 档」这个错的名字上，路由层剔不掉任何东西。
    取不到模型名时宁可跳过（安静），也不能拉黑一个不存在的条目。
    """
    f = ROOT / "state" / "last_model.json"
    if not f.exists():
        return
    try:
        m = json.loads(f.read_text(encoding="utf-8")).get("model")
        if not m:
            return
        import blacklist
        blacklist.append(TASK, m, ok, why=why)
    except Exception:
        pass


def is_published(title):
    if not PUBLISHED.exists():
        return False
    pub = PUBLISHED.read_text(encoding="utf-8", errors="replace")
    key = title.strip().lstrip("# ").strip()
    return key and key[:20] in pub


def h1_of(t):
    return next((l[2:].strip() for l in t.splitlines() if l.startswith("# ")), "")


def numbers(t):
    return set(n.replace(" ", "") for n in NUM_RE.findall(t))


def verify(orig, new):
    """验收：返回 (ok, 原因列表)。任何一条不过就回滚。"""
    probs = []
    # 1) 禁止新增数字
    added = numbers(new) - numbers(orig)
    if added:
        probs.append(f"新增了原文没有的数字: {sorted(added)[:8]}")
    # 2) 署名与开场
    if not h1_of(new).startswith(H1_PREFIX):
        probs.append("H1 前缀被破坏")
    if not OPEN_RE.search(new):
        probs.append("开场白被破坏")
    # 3) 配图与代码块不得减少
    if new.count("![") < orig.count("!["):
        probs.append(f"配图被删（{orig.count('![')}→{new.count('![')}）")
    if new.count("```") < orig.count("```"):
        probs.append(f"代码块被删（{orig.count('```')//2}→{new.count('```')//2}）")
    # 4) 小节数
    s = len(re.findall(r"(?m)^## ", new))
    if not (4 <= s <= 12):
        probs.append(f"小节数 {s} 越界（须 4–12）")
    # 5) 字数（±20% 容差内不拦）
    cn = len(CN_RE.findall(new))
    if not (2800 <= cn <= 5500):
        probs.append(f"字数 {cn} 超出 2800–5500 容差")
    return (not probs), probs, cn, s


def run_gates(path):
    out = {}
    for name in ("fact_check.py", "privacy_check.py"):
        try:
            p = subprocess.run([sys.executable, str(ROOT / "tools" / name), str(path)],
                               capture_output=True, text=True, timeout=240, cwd=str(ROOT))
            txt = (p.stdout or "") + (p.stderr or "")
            out[name] = {"rc": p.returncode, "block": "❌" in txt or p.returncode == 2,
                         "tail": txt.strip().splitlines()[-2:]}
        except subprocess.TimeoutExpired:
            out[name] = {"rc": -1, "block": True, "tail": ["timeout"]}
    return out


def log(rec):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def polish(path, apply=False, tier="free", dry=False):
    p = Path(path).resolve()
    orig = p.read_text(encoding="utf-8")
    title = h1_of(orig)

    if is_published(title):
        return {"file": str(p), "skipped": "已发布（R28 不回改）"}
    if not title.startswith(H1_PREFIX):
        return {"file": str(p), "skipped": f"H1 前缀不符「{H1_PREFIX}」"}

    before = {"cn": len(CN_RE.findall(orig)), "secs": len(re.findall(r"(?m)^## ", orig)),
              "imgs": orig.count("!["), "blocks": orig.count("```") // 2}

    t0 = time.time()
    new = llm_call(POLISH_SPEC + "\n\n===== 原稿开始 =====\n" + orig + "\n===== 原稿结束 =====\n",
                   tier=tier)
    # 去掉模型可能加的包裹
    new = re.sub(r"^```(?:markdown|md)?\s*\n", "", new).rstrip() + "\n"
    if "===== 原稿结束" in new:
        new = new.split("===== 原稿结束")[0].rstrip() + "\n"

    ok, probs, cn, secs = verify(orig, new)
    if not ok:
        # 验收不过 = 这个模型在 polish 任务上做不好 ⇒ 记黑名单（连续 2 次自动拉黑）
        _mark_model(new, ok=False, why=";".join(probs)[:200])
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "file": p.name,
               "action": "rollback", "reasons": probs, "before": before,
               "sec": round(time.time() - t0, 1)}
        if apply and not dry:
            log(rec)
        return {"file": p.name, "verdict": "rollback", "reasons": probs, "before": before,
                "after": {"cn": cn, "secs": secs}, "sec": rec["sec"]}

    if dry or not apply:
        if ok:
            _mark_model(new, ok=True, why="dry 验收通过")
        return {"file": p.name, "verdict": "dry", "before": before,
                "after": {"cn": cn, "secs": secs}, "reasons": []}

    # 落盘：先留底，再写新稿，再跑闸门，闸门不过就还原
    backup = p.with_suffix(".prepolish.md")
    shutil.copy2(p, backup)
    p.write_text(new, encoding="utf-8")
    g = run_gates(p)
    if any(v["block"] for v in g.values()):
        shutil.copy2(backup, p)
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "file": p.name,
               "action": "rollback_gate", "gates": {k: v["tail"] for k, v in g.items()},
               "before": before, "sec": round(time.time() - t0, 1)}
        log(rec)
        return {"file": p.name, "verdict": "rollback_gate", "before": before, "gates": rec["gates"]}

    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "file": p.name, "action": "polished",
           "before": before, "after": {"cn": cn, "secs": secs}, "backup": backup.name,
           "gates": {k: v["tail"] for k, v in g.items()}, "sec": round(time.time() - t0, 1)}
    log(rec)
    return {"file": p.name, "verdict": "polished", "before": before,
            "after": {"cn": cn, "secs": secs}, "backup": backup.name}


def pending_files():
    out = []
    for f in sorted(CONTENT.glob("*.md")):
        if f.name in ("STYLE.md", "INDEX.md") or f.name.endswith(".raw.md"):
            continue
        if f.name.endswith(".prepolish.md"):
            continue
        t = f.read_text(encoding="utf-8", errors="replace")
        if not h1_of(t).startswith(H1_PREFIX):
            continue
        if is_published(h1_of(t)):
            continue
        out.append(f)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?")
    ap.add_argument("--pending", action="store_true", help="处理所有未发布稿")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--apply", action="store_true", help="真改（默认只演练）")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--tier", default="free",
                    help="默认 free（改法 C，09-27）。要强制走 editor 付费档才显式写 --tier editor")
    ap.add_argument("--max-calls", type=int, default=2, help="单次运行最多优化几篇（控成本）")
    args = ap.parse_args()

    if args.list:
        fs = pending_files()
        print(f"可优化（未发布 + 署名合规）{len(fs)} 篇：")
        for f in fs:
            print("  ", f.name, "|", h1_of(f.read_text(encoding='utf-8'))[:34])
        return 0

    if args.pending:
        files = pending_files()[:args.max_calls]
        if not files:
            print("没有待优化的未发布稿")
            return 0
    elif args.file:
        files = [Path(args.file)]
    else:
        ap.error("给个文件，或用 --pending / --list")

    rc = 0
    for f in files:
        r = polish(f, apply=args.apply, tier=args.tier, dry=args.dry)
        line = json.dumps(r, ensure_ascii=False)
        print(line)
        if r.get("verdict") in ("rollback", "rollback_gate"):
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
