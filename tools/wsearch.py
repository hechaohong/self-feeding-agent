#!/usr/bin/env python3
"""搜索（¥0，纯 HTTP，不用浏览器）：**百度为主（中文）/ searxng 为主（英文技术）**，互相兼顾。

09-17 实测（纠正项目旧记录，可写进文章的一手发现）：
  · 旧记「bsearch.sh 查询被截断成首字」是**误判**。真相：**cn.bing.com 对本机 IP 降级返回**——
    HTTP 200、搜索框和 `<title>` 里都是完整查询词（`爱发电 手续费`），但结果全是「爱奇艺 / 爱_百度百科」。
    裸 curl 与真实浏览器结果一致 ⇒ 不是拼 URL 出错、不是编码问题，是 Bing 端给爬虫的假结果页。
    表现欺骗性强：看起来就像「只搜了第一个字」，实际它把查询词原样回显了。
  · 本机 searxng(`127.0.0.1:8888`) **英文/技术类可用**（旧记「通用引擎全超时」已过期），
    但中文查询会跟着上游 Bing 一起退化，且 `engines=baidu` 返回 0 条。
  · 百度 HTML 直连（暖 cookie + 控制频率）**中文结果最好**：实测能拿到 afdian.com 官方页与手续费说明；
    请求太密会 302 到安全验证 → 失败就回落，**不要重试刷**。

  python3 tools/wsearch.py "爱发电 提现 手续费" -n 8
  python3 tools/wsearch.py "llama.cpp parallel slots" -n 5 --backend searxng
  python3 tools/wsearch.py "kw" --json          # 机器可读
  tools/bsearch.sh "kw" 8                       # 旧入口，已改为本工具的薄封装
"""
import argparse
import hashlib
import html
import http.cookiejar
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
SEARXNG = "http://127.0.0.1:8888/search"
ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
COOKIE_FILE = STATE / "search_cookies.txt"      # 暖 cookie 能显著降低百度验证码概率
CACHE_FILE = STATE / "search_cache.json"        # 同一查询 6h 内复用：省百度配额、也省上下文
CACHE_TTL = 6 * 3600

STATE.mkdir(parents=True, exist_ok=True)
_CJ = http.cookiejar.MozillaCookieJar(str(COOKIE_FILE))
try:
    _CJ.load(ignore_discard=True, ignore_expires=True)
except Exception:                               # noqa: BLE001
    pass
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_CJ))


def http(url, timeout=30, referer=None):
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if referer:
        h["Referer"] = referer
    with _OPENER.open(urllib.request.Request(url, headers=h), timeout=timeout) as f:
        body = f.read().decode("utf-8", "replace")
    try:
        _CJ.save(ignore_discard=True, ignore_expires=True)
    except Exception:                           # noqa: BLE001
        pass
    return body


def load_cache(q, n):
    try:
        d = json.loads(CACHE_FILE.read_text())
    except Exception:                           # noqa: BLE001
        return None
    ent = d.get(hashlib.md5(f"{q}|{n}".encode()).hexdigest())
    if ent and time.time() - ent.get("ts", 0) < CACHE_TTL:
        return ent
    return None


def save_cache(q, n, backend, results):
    try:
        d = json.loads(CACHE_FILE.read_text())
    except Exception:                           # noqa: BLE001
        d = {}
    d[hashlib.md5(f"{q}|{n}".encode()).hexdigest()] = {
        "q": q, "ts": time.time(), "backend": backend, "results": results}
    if len(d) > 400:                            # 别无限长
        for k in sorted(d, key=lambda k: d[k].get("ts", 0))[:100]:
            d.pop(k, None)
    CACHE_FILE.write_text(json.dumps(d, ensure_ascii=False))


def strip_tags(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


# ---------------- backends ----------------
def b_searxng(q, n, engines=None):
    p = {"q": q, "format": "json"}
    if engines:
        p["engines"] = engines
    d = json.loads(http(SEARXNG + "?" + urllib.parse.urlencode(p), timeout=35))
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": (r.get("content") or "")[:220]} for r in d.get("results", [])[:n]]


def b_baidu(q, n):
    if not _CJ:                                   # 冷 cookie 直接查会被 302 到验证页
        try:
            http("https://www.baidu.com/", timeout=12)
            time.sleep(1.0)
        except Exception:                         # noqa: BLE001
            pass
    page = http("https://www.baidu.com/s?" + urllib.parse.urlencode({"wd": q, "rn": max(n, 10)}),
                referer="https://www.baidu.com/")
    if any(k in page for k in ("安全验证", "wappass.baidu.com", "请输入验证码")):
        raise RuntimeError("百度限流要验证码（隔一会儿再试）")
    out = []
    for block in re.split(r'<div[^>]+class="[^"]*result c-container', page)[1:]:
        block = block[:6000]
        m = re.search(r"<h3[^>]*>.*?<a[^>]*>(.*?)</a>", block, re.S)
        if not m:
            continue
        title = strip_tags(m.group(1))
        if not title:
            continue
        mu = re.search(r'mu="(https?://[^"]+)"', block)
        body = strip_tags(block).replace(title, " ", 1)
        snip = max((s.strip() for s in re.split(r"\s{2,}", body) if len(s.strip()) > 20),
                   key=len, default="")
        out.append({"title": title, "url": (mu.group(1) if mu else ""), "snippet": snip[:220]})
        if len(out) >= n:
            break
    if not out:
        raise RuntimeError("百度解析出 0 条")
    return out


BACKENDS = {
    "baidu": b_baidu,
    "searxng": lambda q, n: b_searxng(q, n),
    "github": lambda q, n: b_searxng(q, n, engines="github"),
}
HAS_CJK = re.compile(r"[\u4e00-\u9fff]")


def cjk_terms(q):
    """查询里的 2-gram（中文）或长 token（英文），用来判“结果是真相关还是降级假结果”。"""
    cjk = re.findall(r"[\u4e00-\u9fff]+", q)
    grams = [s[i:i + 2] for s in cjk for i in range(len(s) - 1)]
    if grams:
        return grams[:6]
    return [t for t in re.findall(r"[A-Za-z0-9_.+-]{4,}", q)][:4]


def degraded(q, results):
    """假结果检测：查询词被回显、但结果并不真的相关。

    实测：查「爱发电 手续费」→ 结果全是「爱奇艺 / 爱_百度百科 / 爱（情感）」；
    查「百度众测 提现」→ 结果全是「百度一下 / 百度百科 / 百度地图」（只有“百度”这种常见二字撞上）。
    ⇒ 只看 1 个 2-gram 会漏判（“百度”大写命中），所以要求**至少 2 个不同的 2-gram 命中**，
    或者整串第一个中文词本体出现（如“百度众测”四个字连在一起）。
    """
    terms = cjk_terms(q)
    if not terms or not results:
        return False
    blob = " ".join((r.get("title") or "") + " " + (r.get("snippet") or "") + " " + (r.get("url") or "")
                    for r in results)
    hit = sum(1 for t in terms if t in blob)
    need = min(2, len(terms))
    if hit >= need:
        return False
    first = re.findall(r"[\u4e00-\u9fff]{2,}", q)
    if first and len(first[0]) >= 3 and first[0] in blob:
        return False                    # 完整词本体出现，算真命中
    return True


def auto_order(q):
    """中文查百度（结果最好），英文/技术查 searxng（百度对英文一般）。"""
    return ["baidu", "searxng", "github"] if HAS_CJK.search(q) else ["searxng", "baidu", "github"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("-n", type=int, default=8)
    ap.add_argument("--backend", default="auto",
                    choices=["auto"] + list(BACKENDS))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--no-cache", action="store_true", dest="no_cache")
    a = ap.parse_args()

    order = auto_order(a.query) if a.backend == "auto" else [a.backend]
    if not a.no_cache and a.backend == "auto":
        ent = load_cache(a.query, a.n)
        if ent:
            res, used = ent.get("results", []), ent.get("backend", "cache") + "(cache)"
            errs = []
            if a.json:
                print(json.dumps({"query": a.query, "backend": used, "errors": [],
                                  "results": res}, ensure_ascii=False, indent=1))
                return 0 if res else 1
            for r in res:
                print(f"- {r['title'][:90]}\n  {r['url'][:130]}\n  {r['snippet'][:300 if a.raw else 130]}")
            print(f"[{used} · {len(res)} 条]", file=sys.stderr)
            return 0

    res, used, errs, flagged = [], "", [], False
    for b in order:
        try:
            r = BACKENDS[b](a.query, a.n)
            if not r:
                errs.append(f"{b}: 0 条")
                continue
            if a.backend == "auto" and degraded(a.query, r):
                errs.append(f"{b}: 结果疑似降级（没一条命中查询词，像 Bing 假结果页）")
                if not res:
                    res, used, flagged = r, b, True   # 留着当最后兑底，但标记
                continue
            res, used, flagged = r, b, False
            break
        except Exception as e:                                     # noqa: BLE001
            errs.append(f"{b}: {type(e).__name__} {str(e)[:80]}")
    if res and used and not used.endswith("(cache)") and a.backend == "auto" and not flagged:
        save_cache(a.query, a.n, used, res)
    if a.json:
        print(json.dumps({"query": a.query, "backend": used, "errors": errs,
                          "results": res}, ensure_ascii=False, indent=1))
        return 0 if res else 1
    if not res:
        print("搜索失败：" + " | ".join(errs), file=sys.stderr)
        return 1
    lim = 300 if a.raw else 130
    if flagged:
        print("⚠ 以下结果疑似降级假结果（没有一个搜索项命中查询词，别直接引用）：", file=sys.stderr)
    for r in res:
        print(f"- {r['title'][:90]}\n  {r['url'][:130]}\n  {r['snippet'][:lim]}")
    print(f"[{used} · {len(res)} 条]" + (f"（回落：{errs}）" if errs else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
