#!/usr/bin/env python3
"""热门话题采集器（素材池）—— ¥0，不调 LLM。

为什么有它：`write_article.py` 只会写"我自己的事"，选题来源单一；
本工具从 5 个免费榜抓"外面在热什么"，按本账号人设相关性打分，
沉淀成可复用素材池 data/hot_topics.json + 人读摘要 data/hot_topics.md。

  python3 tools/hot_topics.py fetch                 # 抓全部源（默认）
  python3 tools/hot_topics.py fetch --sources hn,juejin
  python3 tools/hot_topics.py top --n 20 [--new]    # 看榜（--new 只看本次新出现）
  python3 tools/hot_topics.py pick --n 5            # 生成选题候选 → data/topic_pool.md
  python3 tools/hot_topics.py sources               # 源健康度（最近一次成败）

设计要点
- **只存增量**：每条按标题归一化哈希去重，记录 first_seen / last_seen / seen_count
  → seen_count 就是"热度持续性"，比单次排名更有信息量。
- **打分纯规则**（¥0）：关键词相关性 + 排名 + 持续性 − 娱乐噪声降权。
- **索引分层**：L2 原始 `data/hot_topics.json` ｜ L1 人读 `data/hot_topics.md`
  ｜ 工具索引 `TOOLS.md` 一行。
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import ssl
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STORE = DATA / "hot_topics.json"
MD = DATA / "hot_topics.md"
POOL = DATA / "topic_pool.md"
CST = timezone(timedelta(hours=8))

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# ---------- 相关性词典（本账号人设：自养 Agent / 成本 / 本地部署 / 踩坑） ----------
K_HIGH = ["agent", "智能体", "llm", "大模型", "大语言模型", "gpt", "claude", "deepseek",
          "qwen", "gemini", "开源", "open source", "推理", "inference", "显存", "gpu",
          "3090", "token", "context", "本地部署", "self-host", "ollama", "vllm",
          "llama.cpp", "微调", "finetune", "rag", "mcp", "agentic", "copilot"]
K_MED = ["ai", "编程", "开发", "程序员", "工具", "效率", "自动化", "脚本", "python",
         "github", "独立开发", "副业", "变现", "部署", "接口", "api", "免费", "开源项目",
         "价格", "涨价", "降价", "成本", "免费额度", "浏览器", "插件", "数据库", "linux"]
K_NEG = ["明星", "综艺", "电视剧", "演唱会", "球赛", "彩票", "房价", "离婚", "婚礼",
         "车祸", "凶杀", "诈骗案", "高考", "台风", "天气", "机票", "退休金", "养老金"]
MD_BAD = "\x00"


def now() -> datetime:
    return datetime.now(CST)


def fetch(url: str, timeout: int = 15, headers: dict | None = None) -> bytes:
    h = dict(UA)
    h.update(headers or {})
    h.setdefault("Connection", "close")
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            return r.read()
    except http.client.IncompleteRead as e:   # github chunked 响应会半途断流
        return e.partial or b""


def fetch_json(url: str, timeout: int = 15, headers: dict | None = None):
    return json.loads(fetch(url, timeout, headers).decode("utf-8", "replace"))


def walk(obj, key: str):
    """递归找出所有含指定 key 的 dict。"""
    if isinstance(obj, dict):
        if key in obj:
            yield obj
        for v in obj.values():
            yield from walk(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v, key)


# ============================== 源实现 ==============================
# 每个源返回 [(title, url)]，顺序即榜单排名（越前越热）

def src_hn(limit=20):
    ids = fetch_json("https://hacker-news.firebaseio.com/v0/topstories.json")[:limit]

    def one(i):
        try:
            d = fetch_json(f"https://hacker-news.firebaseio.com/v0/item/{i}.json")
            if d and d.get("title"):
                return (d["title"], d.get("url") or f"https://news.ycombinator.com/item?id={i}")
        except Exception:
            return None

    with ThreadPoolExecutor(8) as ex:
        return [x for x in ex.map(one, ids) if x]


def src_juejin(limit=30):
    d = fetch_json("https://api.juejin.cn/content_api/v1/content/article_rank"
                   "?category_id=1&type=hot&spider=0")
    out = []
    for it in d.get("data") or []:
        c = it.get("content") or {}
        aid = c.get("content_id")
        title = (c.get("title") or "").strip()
        if aid and title:
            out.append((title, f"https://juejin.cn/post/{aid}"))
    return out[:limit]


def src_baidu(limit=30):
    d = fetch_json("https://top.baidu.com/api/board?platform=wise&tab=realtime")
    out = []
    for node in walk(d, "word"):
        w = (node.get("word") or "").strip()
        desc = (node.get("desc") or "").strip()
        url = node.get("url") or (f"https://www.baidu.com/s?wd={urllib.parse.quote(w)}")
        if w:
            out.append((f"{w}｜{desc}" if desc else w, url))
    _dedup_keep_order(out)
    return out[:limit]


def src_github(limit=25):
    html = fetch("https://github.com/trending?since=daily").decode("utf-8", "replace")
    seen, out = set(), []
    for m in re.finditer(r'<h2 class="h3 lh-condensed">\s*<a[^>]*?href="([^"]+)"', html):
        path = m.group(1).strip()
        if path in seen:
            continue
        seen.add(path)
        out.append((path.strip("/") + " (GitHub Trending)", "https://github.com" + path))
    return out[:limit]


def src_zhihu(limit=20):
    d = fetch_json("https://api.zhihu.com/topstory/hot-list?limit=%d" % limit)
    out = []
    for it in d.get("data") or []:
        t = it.get("target") or {}
        title = ((t.get("title_area") or {}).get("text")
                 or t.get("title")
                 or (t.get("question") or {}).get("title") or "")
        url = ((t.get("link") or {}).get("url")
               or f"https://www.zhihu.com/question/{t.get('id')}")
        title = re.sub(r"<[^>]+>", "", str(title)).strip()
        if title:
            out.append((title, url))
    return out


SOURCES = {
    "hn": ("Hacker News", src_hn),
    "juejin": ("掘金热榜·AI", src_juejin),
    "baidu": ("百度热搜", src_baidu),
    "github": ("GitHub Trending", src_github),
    "zhihu": ("知乎热榜", src_zhihu),
}


def _dedup_keep_order(pairs):
    seen, i = set(), 0
    while i < len(pairs):
        if pairs[i][0] in seen:
            pairs.pop(i)
        else:
            seen.add(pairs[i][0])
            i += 1


# ============================== 打分 / 存储 ==============================

def norm(title: str) -> str:
    t = re.sub(r"[\s\W_]+", "", title.lower())
    return t[:120]


def key_of(title: str) -> str:
    return hashlib.md5(norm(title).encode()).hexdigest()[:12]


def score_of(title: str, rank: int, seen_count: int) -> tuple[int, list[str]]:
    low = title.lower()
    hits = [k for k in K_HIGH if k in low]
    meds = [k for k in K_MED if k in low]
    negs = [k for k in K_NEG if k in low]
    s = 40 * min(len(hits), 3) + 12 * min(len(meds), 3)
    s += max(0, int((30 - rank) / 30 * 20))          # 排名分
    s += min(seen_count, 5) * 6                      # 持续性分
    s -= 30 * len(negs)
    return s, (hits + meds)[:4]


def load() -> dict:
    if STORE.exists():
        try:
            return json.loads(STORE.read_text())
        except Exception:
            pass
    return {"items": {}, "runs": []}


def save(db: dict):
    DATA.mkdir(exist_ok=True)
    tmp = STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False, indent=1))
    tmp.replace(STORE)


def cmd_fetch(args) -> int:
    names = [n.strip() for n in args.sources.split(",") if n.strip()] or list(SOURCES)
    db = load()
    stamp = now().strftime("%Y-%m-%d %H:%M")
    today = now().strftime("%Y-%m-%d")
    new_ids, ok, fail = [], [], []

    def run(n):
        label, fn = SOURCES[n]
        try:
            return n, label, fn(), None
        except Exception as e:
            return n, label, [], f"{type(e).__name__}: {str(e)[:70]}"

    with ThreadPoolExecutor(len(names)) as ex:
        for n, label, items, err in ex.map(run, names):
            if err:
                fail.append(f"{n}({err})")
                continue
            ok.append(f"{n}={len(items)}")
            for rank, (title, url) in enumerate(items):
                k = key_of(title)
                it = db["items"].get(k)
                if it is None:
                    it = {"title": title[:200], "url": url, "first_seen": stamp,
                          "last_seen": stamp, "seen_count": 0, "sources": [], "best_rank": 999}
                    db["items"][k] = it
                    new_ids.append(k)
                it["last_seen"] = stamp
                it["seen_count"] += 1
                it["best_rank"] = min(it["best_rank"], rank + 1)
                if label not in it["sources"]:
                    it["sources"].append(label)
                it["score"], it["tags"] = score_of(it["title"], it["best_rank"], it["seen_count"])

    db["runs"].append({"ts": stamp, "ok": ok, "fail": fail, "new": len(new_ids)})
    db["runs"] = db["runs"][-200:]
    save(db)
    write_md(db, new_ids, stamp, today)

    print(f"[{stamp}] 源: {' '.join(ok) or '-'}" + (f" ｜ 失败: {' '.join(fail)}" if fail else ""))
    print(f"本次新出现 {len(new_ids)} 条 ｜ 池内共 {len(db['items'])} 条 → {MD.relative_to(ROOT)}")
    for k in new_ids[:5]:
        it = db["items"][k]
        print(f"  NEW {it['score']:>3} {it['title'][:70]}")
    return 0


def ranked(db: dict, only_new: bool = False, days: int = 3):
    cut = (now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    items = [(k, v) for k, v in db["items"].items()
             if v.get("last_seen", "") >= cut and (not only_new or v.get("seen_count", 0) <= 1)]
    items.sort(key=lambda kv: (-kv[1].get("score", 0), -kv[1].get("seen_count", 0)))
    return items


def write_md(db: dict, new_ids: list[str], stamp: str, today: str):
    items = ranked(db, days=3)
    lines = [f"# 热门素材池（近 3 天，自动生成：tools/hot_topics.py）", "",
             f"> 更新时间 {stamp} ｜ 池内 {len(db['items'])} 条 ｜ "
             f"本次新 {len(new_ids)} 条 ｜ 打分=关键词相关性+排名+持续性−娱乐噪声", "",
             "| 分 | 出现 | 来源 | 标题 | 首见 |", "|---|---|---|---|---|"]
    for k, v in items[:40]:
        new = " 🆕" if k in new_ids else ""
        title = v["title"].replace("|", "｜")[:90]
        lines.append(f"| {v.get('score',0)} | {v.get('seen_count',0)}× | "
                     f"{','.join(s.split('·')[0].replace('Trending','') for s in v.get('sources', []))[:22]} | "
                     f"[{title}]({v['url']}){new} | {v.get('first_seen','')[-5:]} |")
    lines += ["", "## 分源健康度（最近 5 次）", ""]
    for r in db["runs"][-5:]:
        lines.append(f"- {r['ts']} ✅ {' '.join(r['ok'])}" + (f" ❌ {' '.join(r['fail'])}" if r["fail"] else ""))
    lines += ["", "下一步：`python3 tools/hot_topics.py pick --n 5` 生成选题候选池。"]
    MD.write_text("\n".join(lines) + "\n")


def cmd_top(args) -> int:
    db = load()
    items = ranked(db, only_new=args.new, days=args.days)
    if not items:
        print("池子是空的，先跑 fetch")
        return 1
    for k, v in items[:args.n]:
        tag = "/".join(v.get("tags") or [])
        print(f"{v.get('score',0):>3} {v.get('seen_count',0)}× "
              f"[{','.join(s.split('·')[0] for s in v.get('sources',[]))[:20]}] "
              f"{v['title'][:78]}" + (f"  ‹{tag}›" if tag else ""))
    return 0


def cmd_pick(args) -> int:
    db = load()
    items = ranked(db, days=args.days)
    pool = [kv for kv in items if (kv[1].get("score") or 0) >= args.min_score]
    if not pool:
        print(f"没有 >= {args.min_score} 分的素材（池内 {len(db['items'])} 条），试试 --min-score 40")
        return 1
    pool = pool[:args.n]
    lines = [f"# 选题候选池（自动生成：tools/hot_topics.py pick）", "",
             f"> 生成 {now().strftime('%Y-%m-%d %H:%M')} ｜ "
             f"来源：data/hot_topics.json（近 {args.days} 天，score≥{args.min_score}）", "",
             "> 用法：挑 1 条，与「我自己的真实账单/踩坑」结合成选题（本账号的护城河是自证数据，"
             "热点只做入口，不做主体）。", ""]
    for i, (k, v) in enumerate(pool, 1):
        lines += [f"## {i}. {v['title'][:80]}",
                  f"- 出处：{'/'.join(v.get('sources', []))} ｜ 热度分 {v.get('score')} ｜ "
                  f"出现 {v.get('seen_count')}× ｜ 首见 {v.get('first_seen')}",
                  f"- 链接：{v['url']}",
                  f"- 可写角度（人工填）：",
                  f"  - 我的真实数据能对上吗？（台账/journal 里有没有对应数字）",
                  f"  - 我能不能实测一遍并给出反例？",
                  ""]
    POOL.write_text("\n".join(lines) + "\n")
    print(f"已写入 {POOL.relative_to(ROOT)}（{len(pool)} 条候选）")
    for i, (k, v) in enumerate(pool, 1):
        print(f"  {i}. [{v.get('score')}] {v['title'][:70]}")
    print("（在文件里填「可写角度」后再交给 write_article.py）")
    return 0


def cmd_sources(args) -> int:
    db = load()
    for r in db["runs"][-10:]:
        print(f"{r['ts']}  ✅ {' '.join(r['ok'])}" + (f"  ❌ {' '.join(r['fail'])}" if r["fail"] else ""))
    known = set(SOURCES)
    seen = set()
    for r in db["runs"][-10:]:
        seen |= {x.split("=")[0] for x in r["ok"]}
        seen |= {x.split("(")[0] for x in r["fail"]}
    print("\n未在本机打过照面的源:", ", ".join(sorted(known - seen)) or "无")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="热门话题采集器（素材池，¥0）")
    sub = ap.add_subparsers(dest="cmd")

    f = sub.add_parser("fetch", help="抓取榜单并入库")
    f.add_argument("--sources", default="", help="如 hn,juejin,baidu,github,zhihu")
    f.set_defaults(func=cmd_fetch)

    t = sub.add_parser("top", help="看榜")
    t.add_argument("-n", type=int, default=20)
    t.add_argument("--new", action="store_true", help="只看本次新出现")
    t.add_argument("--days", type=int, default=3)
    t.set_defaults(func=cmd_top)

    p = sub.add_parser("pick", help="生成选题候选池 data/topic_pool.md")
    p.add_argument("-n", type=int, default=5)
    p.add_argument("--min-score", type=int, default=55)
    p.add_argument("--days", type=int, default=3)
    p.set_defaults(func=cmd_pick)

    s = sub.add_parser("sources", help="源健康度")
    s.set_defaults(func=cmd_sources)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        args = ap.parse_args(["fetch"])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
