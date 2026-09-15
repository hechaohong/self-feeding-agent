#!/usr/bin/env python3
"""阅读数据回采（养号期唯一的量化反馈信号）—— ¥0。

三个平台的取数方式（都是今天实测出来的，别改瞎）：
  · CSDN  → HTTP API `community/home-api/v1/get-business-list`（一次拿全部文章：阅读/点赞/评论/收藏）
  · 掘金  → 文章页 HTML 正则 `got_view_count=(\\d+)`（只有阅读数；页面上其它计数是异步渲染的）
  · 知乎  → 登录态浏览器页内 fetch `/api/v4/creators/creations/v2/all`
            （服务端直连 403 缺 zse 签名，必须借页面上下文）

产物（三层索引）：
  L2 `data/stats.jsonl`  每次一条快照（追加，永不重写）
  L1 `data/stats.md`     最新快照 + 相对上次的增量（人读）
  L0 `STATE.md`          由本脚本不碰，人工/action tick 摘一句

用法：
  python3 tools/stats.py                 # 回采全部（含知乎浏览器，约 40s）
  python3 tools/stats.py --fast          # 跳过浏览器（只 CSDN+掘金，约 5s）
  python3 tools/stats.py --platform csdn # 只回采一个平台
  python3 tools/stats.py show            # 只看最新快照（不发请求）
  python3 tools/stats.py show --json
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

DATA = ROOT / "data"
PUBLISHED = DATA / "published.md"
JSONL = DATA / "stats.jsonl"
MD = DATA / "stats.md"
PW = ROOT / "state" / "pw"
CST = timezone(timedelta(hours=8))

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

PLATFORMS = ("csdn", "juejin", "zhihu")
PLAT_LABEL = {"csdn": "CSDN", "juejin": "掘金", "zhihu": "知乎"}

# ---------- ① 已发布清单（唯一事实源：data/published.md） ----------

def load_published() -> list[dict]:
    out = []
    if not PUBLISHED.exists():
        return out
    for line in PUBLISHED.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| 20"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        when, plat, title, url = cells[0], cells[1], cells[2], cells[3]
        p = None
        for k, v in PLAT_LABEL.items():
            if v.lower() in plat.lower() or k in url:
                p = k
        if not p:
            continue
        m = (re.search(r"/post/(\d+)", url) or re.search(r"/p/(\d+)", url)
             or re.search(r"/details/(\d+)", url))
        out.append({"platform": p, "title": title, "url": url,
                    "id": m.group(1) if m else "", "published": when})
    # 同 id 去重（published.md 会追加"已公开"等更新行）
    seen, uniq = set(), []
    for e in out:
        if e["id"] and e["id"] in seen:
            continue
        seen.add(e["id"])
        uniq.append(e)
    return uniq


def norm_title(t: str) -> str:
    """标题归一化：去空白/标点，用于跨源匹配（published.md 与平台返回值）。"""
    return re.sub(r"[\s\W_]+", "", (t or "").lower())[:60]


def site_opt(key: str, env: str = "") -> str:
    """站点参数（如 CSDN 用户名）从 config/sites.json 或环境变量读，不写死在代码里。"""
    p = ROOT / "config" / "sites.json"
    if p.exists():
        try:
            v = json.loads(p.read_text()).get(key)
            if v:
                return str(v)
        except Exception:
            pass
    return os.environ.get(env, "") if env else ""


def cookie_header(state_file: Path, domain: str) -> str:
    st = json.loads(state_file.read_text())
    return "; ".join(f"{c['name']}={c['value']}"
                     for c in st.get("cookies", []) if domain in c.get("domain", ""))


def http_get(url: str, cookie: str = "", referer: str = "", timeout: int = 25) -> str:
    h = dict(UA)
    if cookie:
        h["Cookie"] = cookie
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return r.read().decode("utf-8", "replace")


# ---------- ② 各平台取数 ----------

def stat_csdn(entries: list[dict]) -> tuple[dict, str]:
    """一次 API 拿全部文章的 阅读/点赞/评论/收藏。"""
    user = site_opt("csdn_username", "CSDN_USER")
    if not user:
        return {}, "跳过（未配置 csdn_username）"
    try:
        ck = cookie_header(PW / "csdn.json", "csdn")
        url = ("https://blog.csdn.net/community/home-api/v1/get-business-list"
               f"?page=1&size=100&businessType=blog&orderby=&noMore=false&username={user}")
        d = json.loads(http_get(url, ck, f"https://blog.csdn.net/{user}"))
        rows = ((d.get("data") or {}).get("list")) or []
        m = {}
        for it in rows:
            m[str(it.get("articleId"))] = {
                "views": it.get("viewCount"), "likes": it.get("diggCount"),
                "comments": it.get("commentCount"), "favorites": it.get("collectCount"),
                "real_title": it.get("title"), "url": it.get("url"),
                "published": it.get("postTime"),
            }
        return m, f"ok {len(m)}/{len(entries)}"
    except Exception as e:
        return {}, f"FAIL {type(e).__name__}: {str(e)[:60]}"


def stat_juejin(entries: list[dict]) -> tuple[dict, str]:
    """文章页 HTML 里的 got_view_count（作者维度的阅读数就取这个，实测与前台一致）。"""
    ck = cookie_header(PW / "juejin.json", "juejin")
    m, bad = {}, 0
    for e in entries:
        try:
            html = http_get(e["url"], ck, "https://juejin.cn/")
            hit = re.search(r"[.\w\]]{0,12}(?:got_)?view_count=(\d+)", html)
            m[e["id"]] = {"views": int(hit.group(1)) if hit else None,
                          "likes": None, "comments": None, "favorites": None}
        except Exception:
            bad += 1
            m[e["id"]] = {"views": None, "likes": None, "comments": None, "favorites": None}
    return m, f"ok {len(m) - bad}/{len(entries)}" + (f"（{bad} 条取数失败）" if bad else "")


ZH_JS = """async () => {
  const r = await fetch('/api/v4/creators/creations/v2/all?start=0&end=0&limit=50&offset=0'
                        + '&need_co_creation=1&sort_type=created',
                        {headers:{'x-requested-with':'fetch'}});
  if (!r.ok) return JSON.stringify({error: 'HTTP ' + r.status});
  const j = await r.json();
  return JSON.stringify((j.data || []).map(it => ({
    id: String((it.data || {}).id || ''),
    title: ((it.data || {}).title || '').slice(0, 60),
    created: (it.data || {}).created_time,
    views: ((it.reaction || {}).read_count) ?? ((it.reaction || {}).view_count) ?? null,
    likes: ((it.reaction || {}).vote_up_count) ?? null,
    comments: ((it.reaction || {}).comment_count) ?? null,
    favorites: ((it.reaction || {}).collect_count) ?? null,
    like2: ((it.reaction || {}).like_count) ?? null
  })));
}"""


def stat_zhihu(entries: list[dict]) -> tuple[dict, str]:
    """借登录态浏览器页内 fetch（服务端缺 zse 签名会 403）。"""
    try:
        import pwsession
    except Exception as e:
        return {}, f"FAIL import pwsession: {e}"
    s = None
    try:
        s = pwsession.Session()
        page = s.page()
        page.goto("https://www.zhihu.com/creator/manage/creation/all",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        raw = page.evaluate(ZH_JS)
        data = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(data, dict) and data.get("error"):
            return {}, f"FAIL {data['error']}"
        m = {}
        for it in data:
            m[str(it.get("id"))] = {"views": it.get("views"), "likes": it.get("likes"),
                                    "comments": it.get("comments"),
                                    "favorites": it.get("favorites") or it.get("like2"),
                                    "real_title": it.get("title")}
        return m, f"ok {len(m)}/{len(entries)}"
    except Exception as e:
        return {}, f"FAIL {type(e).__name__}: {str(e)[:70]}"
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass


# ---------- ③ 快照 / 渲染 ----------

def snapshot(platforms, include_browser: bool) -> dict:
    entries = load_published()
    src = {p: [e for e in entries if e["platform"] == p] for p in PLATFORMS}
    tables, notes = {}, {}
    for p in PLATFORMS:
        if not src[p]:
            tables[p], notes[p] = {}, "无文章"
            continue
        if p == "csdn":
            tables[p], notes[p] = stat_csdn(src[p])
        elif p == "juejin":
            tables[p], notes[p] = stat_juejin(src[p])
        elif p == "zhihu":
            if not include_browser:
                tables[p], notes[p] = {}, "跳过（--fast）"
            else:
                tables[p], notes[p] = stat_zhihu(src[p])
    items = []
    for p in PLATFORMS:
        table = tables[p]
        # 标题兜底索引：published.md 里存的是编辑器 URL（拿不到 id）时用它匹配
        by_title = {norm_title(r.get("real_title", "")): r for r in table.values()
                    if r.get("real_title")}
        for e in src[p]:
            row = table.get(e["id"]) or by_title.get(norm_title(e["title"])) or {}
            items.append({"platform": p, "id": e["id"], "title": e["title"],
                          "url": e["url"], "published": e["published"],
                          "views": row.get("views"), "likes": row.get("likes"),
                          "comments": row.get("comments"), "favorites": row.get("favorites")})
    return {"ts": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "platforms": notes, "items": items}


def load_history() -> list[dict]:
    if not JSONL.exists():
        return []
    out = []
    for line in JSONL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def append_history(snap: dict):
    DATA.mkdir(exist_ok=True)
    with JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")


def render(snap: dict, prev: dict | None, out_path: Path = MD):
    pv = {}
    if prev:
        pv = {(i["platform"], i["id"]): i for i in prev.get("items", [])}
    rows, totals = [], {}
    for it in snap["items"]:
        o = pv.get((it["platform"], it["id"]), {})
        d = (it["views"] - o["views"]) if (it["views"] is not None and o.get("views") is not None) else None
        t = totals.setdefault(it["platform"], {"n": 0, "v": 0, "known": 0})
        t["n"] += 1
        if it["views"] is not None:
            t["v"] += it["views"]
            t["known"] += 1
        rows.append((it, d))
    rows.sort(key=lambda r: ((r[0]["views"] if r[0]["views"] is not None else -1)), reverse=True)

    L = ["# 阅读数据快照（自动生成：tools/stats.py）", "",
         f"> 采集时间 {snap['ts']} ｜ 平台状态：" +
         " ｜ ".join(f"{PLAT_LABEL[p]}={snap['platforms'].get(p, '-')}" for p in PLATFORMS), ""]
    if all(not v for v in snap["platforms"].values()) or not rows:
        L += ["（本次没有取到任何数据）", ""]
    L += ["| 平台 | 阅读 | 赞 | 评 | 藏 | Δ阅读 | 标题 | 链接 |", "|---|---|---|---|---|---|---|---|"]
    for it, d in rows:
        f = lambda x: "-" if x is None else str(x)          # noqa: E731
        dtxt = "" if d is None else (f"+{d}" if d > 0 else str(d))
        title = it["title"]
        short = title[:26] + ("…" if len(title) > 26 else "")
        L.append(f"| {PLAT_LABEL[it['platform']]} | {f(it['views'])} | {f(it['likes'])} | "
                 f"{f(it['comments'])} | {f(it['favorites'])} | {dtxt} | "
                 f"[{short}]({it['url']}) | {it['published'][-5:]} |")
    L += ["", "## 汇总", ""]
    for p in PLATFORMS:
        t = totals.get(p)
        if not t:
            continue
        avg = (t["v"] / t["known"]) if t["known"] else 0
        L.append(f"- **{PLAT_LABEL[p]}**：{t['known']}/{t['n']} 篇有数据 ｜ 总阅读 {t['v']} ｜ 篇均 {avg:.1f}")
    allv = sum(t["v"] for t in totals.values())
    known = sum(t["known"] for t in totals.values())
    L += [""]
    L.append(f"- **全渠道**：总阅读 {allv} ｜ 篇均 {allv / known:.1f}（{known} 篇有数据）"
             if known else "- **全渠道**：还没有数据")
    if prev:
        L += [f"- 上次采集：{prev['ts']}", ""]
    L += ["", "> 用途：判断「哪个平台/哪类选题真的有人看」。养号期唯一客观信号，"
              "严禁用阅读数换钱，也严禁为阅读数买流量。", ""]
    out_path.write_text("\n".join(L), encoding="utf-8")
    return out_path


def cmd_show(args) -> int:
    hist = load_history()
    if not hist:
        print("还没有快照，先跑 python3 tools/stats.py")
        return 1
    snap, prev = hist[-1], (hist[-2] if len(hist) > 1 else None)
    if args.json:
        print(json.dumps(snap, ensure_ascii=False, indent=1))
        return 0
    print(f"{snap['ts']} ｜ " + " ｜ ".join(f"{PLAT_LABEL[p]}={snap['platforms'].get(p, '-')}" for p in PLATFORMS))
    for it in sorted(snap["items"], key=lambda i: -(i["views"] or -1)):
        print(f"  {PLAT_LABEL[it['platform']]:<4} 阅读 {str(it['views']):>5} "
              f"赞 {str(it['likes']):>3} 评 {str(it['comments']):>3} "
              f"藏 {str(it['favorites']):>3}  {it['title'][:40]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="阅读数据回采（¥0）")
    ap.add_argument("cmd", nargs="?", default="fetch", choices=["fetch", "show"])
    ap.add_argument("--fast", action="store_true", help="跳过浏览器（不采知乎）")
    ap.add_argument("--platform", default="", help="只采某平台：csdn/juejin/zhihu")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.cmd == "show":
        return cmd_show(a)

    global PLATFORMS
    if a.platform:
        PLATFORMS = tuple(x.strip() for x in a.platform.split(","))
    hist = load_history()
    prev = hist[-1] if hist else None
    snap = snapshot(PLATFORMS, include_browser=not a.fast)
    append_history(snap)
    render(snap, prev)
    ok = " ｜ ".join(f"{PLAT_LABEL[p]}={snap['platforms'].get(p, '-')}" for p in PLATFORMS)
    got = [i for i in snap["items"] if i["views"] is not None]
    total = sum(i["views"] for i in got)
    print(f"[{snap['ts']}] {ok}")
    print(f"共 {len(snap['items'])} 篇（{len(got)} 篇有数据）｜总阅读 {total} → {MD.relative_to(ROOT)}")
    if a.json:
        print(json.dumps(snap, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
