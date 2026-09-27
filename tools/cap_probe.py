#!/usr/bin/env python3
"""能力探针：把免费池的判据从「能回字符串」升级成「能干活」（09-27 用户提出）。

问题（09-27 实测确认）
----------------------
free_pool.py:101-113 的探测是：
    prompt = "Reply with exactly: OK"   max_tokens = 32   判据 ok = bool(txt)
所以 7B/8B 弱模型会以「健康」身份进池，被路由派去写 5000 字稿。
09-27 实测：`Qwen/Qwen2.5-7B-Instruct` 在池里显示健康，真去 polish 一篇 2296 字稿，
把稿子砍到 1381 字、破坏开场白 ⇒ 验收回滚。**可达 ≠ 能用。**

「能用」是**任务属性**，不是模型全局属性。所以本探针不产出「好/坏模型」的全局结论，
它只回答一道题：**这个模型在「长文结构化改写」这类活上，会不会按格式办事。**
任务级淘汰交给 tools/blacklist.py（连续 2 次验收不过 ⇒ 该任务拉黑它）。

两道题（09-27 实测过区分度，不是拍脑袋）
--------------------------------------
T1 json_extract  干扰文本里挑出被明确指认的那个数字，只输出严格 JSON。
                 实测 7 个模型 6 个过 ⇒ **区分度低**，但极便宜，必跑（抓格式坏）。
T2 dedup_md     markdown 数字去重 + 不新增 + 保留标题 + 不裹代码块。
                 实测 space-bunny-free 输 2007B（把规则复述了一遍）判 FAIL，
                 三个弱模型 ~190B 判 PASS ⇒ **有区分度**，且判据全部可机器判定。

⚠️ 已知局限（别过度解读输出）
  · T1 区分度低是事实，不是 bug。它的作用是抓 BADFMT（格式坏），不是排序。
  · T2 的 FAIL 必须看细项：`WRAPPED`/`LOST_HEADING` 是**没按格式答**，
    `DEDUP_FAIL`/`HALLUC_NUM` 才是**做错了**。两者都算不过，但含义不同。
  · 两道题都不是「写稿」。**过这两题不等于能写稿**，别把它当质量评级。
  · 探针本身会烧配额。默认**跳过 openrouter**（实测配额仅剩 13/50），要跑得显式 --all。

省配额是硬约束：探针要是每天把配额烧光，它自己就成了问题。
所以默认只跑 siliconflow / zen（配额不稀缺），openrouter 要显式开。

  python3 tools/cap_probe.py                     # 跑 siliconflow + zen（默认）
  python3 tools/cap_probe.py --all               # 含 openrouter（烧配额）
  python3 tools/cap_probe.py --models A,B        # 指定模型（逗号分隔）
  python3 tools/cap_probe.py --selftest          # 验判据函数本身对不对
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "state" / "cap_probe.jsonl"

FREE_KEYS = ("sf", "zen")        # 默认跑的 provider 凭据名（配额不稀缺）
OR_KEY = "openrouter"            # 要 --all 才跑

# ---------------- T1 ----------------
T1_Q = ('下面这段话里混着 3 个假数字和 1 个真数字。真数字是文中明确说"实际是"的那一个。'
        '只输出严格 JSON，不要解释：{"n": 数字}\n\n'
        '原文：他一开始说成本是 ¥0.45，后来又说前一天的 session-api 实际是 ¥3.49，'
        '比值差不多 8 倍。中间还有个 ¥2.68 的数，是四天均值里的 session 口径。')
T1_ANS = 3.49
T1_MAXTOK = 200

# ---------------- T2 ----------------
T2_SRC = """## 账单快照：一行数字
现金余额 **¥-32.51**（收入 ¥0 / 支出 ¥32.51）。额度剩 **¥28.40 / 60**，今天会话花销 **¥0.00**。
## 真问题
今天现金余额还是 **¥-32.51**，额度剩 **¥28.40 / 60**。两个数并排，谁也没救谁。
跑道按日均 ¥2.89 算，¥28.40 除下来是 9.8 天。¥2.89 是含全部类别的四天均值。"""
T2_RULE = """规则（必须全遵守）：
1. 文中每个数字只保留**第一次出现**的那一处，其余全部原样删掉（删数字，不改周围字）
2. 绝不新增任何数字
3. 两个 ## 标题一字不改地都保留
4. 只输出改写后的 markdown，不要任何解释、不要代码块包裹

""" + T2_SRC
T2_MAXTOK = 900

_NUM = re.compile(r"¥[\d.]+|\b\d+\b")


def _strip_fence(s):
    # 坑（09-27 selftest 抓到）：原来的正则只认 ``` 和 ```markdown，
    # 不认 ```json / ```md 这类带语言标签的围栏 ⇒ 模型答对了内容却被判 BADFMT。
    # 「裹围栏」是极常见的坏习惯，但**内容对就该算内容对**（判格式的事归 T2 管）。
    s = (s or "").strip()
    s = re.sub(r"^```[a-zA-Z0-9_+-]*[ \t]*\r?\n?", "", s)
    s = re.sub(r"\r?\n?```[ \t]*$", "", s)
    return s.strip()


def _dedup_expected():
    """按 T2 规则算出「标准答案」，用来自测判据（不能拿手写的期望串，那会自证）。
    规则：每个数字只保留第一次出现的那一处，其余删掉。"""
    seen, out, pos = set(), [], 0
    for m in _NUM.finditer(T2_SRC):
        out.append(T2_SRC[pos:m.start()])
        if m.group(0) not in seen:
            seen.add(m.group(0))
            out.append(m.group(0))
        pos = m.end()
    out.append(T2_SRC[pos:])
    return "".join(out)


def judge_t1(out):
    """→ (verdict, detail)。verdict ∈ PASS/FAIL/BADFMT/EMPTY"""
    if not (out or "").strip():
        return "FAIL", "空输出"
    try:
        d = json.loads(_strip_fence(out))
        v = float(d["n"])
    except Exception:
        return "BADFMT", "不是可解析的 JSON（格式坏）"
    if abs(v - T1_ANS) < 1e-9:
        return "PASS", f"n={v}"
    return "FAIL", f"取错值 n={v}（应为 {T1_ANS}）"


def judge_t2(out):
    """→ (verdict, detail)。区分『做错』和『没按格式答』——两者都不过，但含义不同。"""
    o = _strip_fence(out)
    if not o:
        return "FAIL", "空输出"
    if "```" in o:
        return "FAIL", "WRAPPED（裹了代码块）"
    if o.count("##") != T2_SRC.count("##"):
        return "FAIL", f"LOST_HEADING（标题数 {o.count('##')} ≠ {T2_SRC.count('##')}）"
    n_in, n_out = len(_NUM.findall(T2_SRC)), len(_NUM.findall(o))
    if n_out >= n_in:
        return "FAIL", f"DEDUP_FAIL（数字 {n_out} 个，没去重：输入 {n_in}）"
    new = set(_NUM.findall(o)) - set(_NUM.findall(T2_SRC))
    if new:
        return "FAIL", f"HALLUC_NUM（编出新数字 {sorted(new)[:3]}）"
    return "PASS", f"去重 -{n_in - n_out} 个数字，格式全对"


def _call(model, prompt, max_tok, timeout):
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "llm.py"),
                        "--tier", "free", "--model", model, "--no-thinking",
                        "--max-tokens", str(max_tok), "--temperature", "0",
                        "--timeout", str(timeout), prompt],
                       capture_output=True, text=True, timeout=timeout + 90, cwd=str(ROOT))
    return (r.stdout or ""), (r.stderr or ""), r.returncode


def probe(model, timeout=150):
    t0 = time.time()
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": model}

    def one(judge, prompt, max_tok, to):
        """→ ((verdict, detail), stderr_tail)。**先把「请求没成功」和「模型答不出」分开**。

        坑（09-27 实测踩到）：第一版不管 rc，space-bunny-free 明明 4.2s 能跑通，
        探针却报「空输出」→ 假阴性。假阴性比没探针更糟：它会把好模型拉黑。
        所以 rc!=0 一律记 ERROR（端点/参数/限流问题），不算模型的锅。
        每次只调一次（第一版在 rc==0 时把结果丢了又调一遍 = 双倍烧配额）。
        """
        try:
            o, e, rc = _call(model, prompt, max_tok, to)
        except subprocess.TimeoutExpired:
            return ("ERROR", "超时（客户端）"), ""
        except Exception as e:
            return ("ERROR", f"{type(e).__name__}"), ""
        if rc != 0:
            tail = (e or "").strip().splitlines()[-1:] or [""]
            return ("ERROR", f"rc={rc} {tail[0][:70]}"), (e or "")[-400:]
        return judge(o), (e or "")[-400:]

    row["t1"], row["t1_err"] = one(judge_t1, T1_Q, T1_MAXTOK, timeout)
    row["t2"], row["t2_err"] = one(judge_t2, T2_RULE, T2_MAXTOK, timeout + 120)

    vs = [row["t1"][0], row["t2"][0]]
    row["n_error"] = sum(1 for v in vs if v == "ERROR")
    # 判据：两道都要 PASS。T2 是区分题，T1 只抓格式坏。
    # **有 ERROR 时不判可用**——但也标成 errored，不当成「模型不行」（见 one() 注释）。
    row["usable"] = all(v == "PASS" for v in vs)
    row["errored"] = row["n_error"] > 0
    row["secs"] = round(time.time() - t0, 1)
    return row


def free_models(only_keys=FREE_KEYS, names=None):
    cfg = json.loads((ROOT / "config" / "llm.json").read_text(encoding="utf-8"))
    out = []
    for e in cfg.get("tiers", {}).get("free", []):
        if names and e["model"] not in names:
            continue
        if e.get("key") not in only_keys:
            continue
        out.append(e["model"])
    return out


def selftest():
    """验判据函数本身。别让「探针写错了」被误读成「模型不行」。"""
    good_t1 = '{"n": 3.49}'
    bad_t1 = '{"n": 0.45}'
    wrap_t1 = '```json\n{"n": 3.49}\n```'
    good_t2 = _dedup_expected()
    cases = [
        ("T1 正确值", judge_t1(good_t1), "PASS"),
        ("T1 取错值", judge_t1(bad_t1), "FAIL"),
        ("T1 带 json 围栏也认", judge_t1(wrap_t1), "PASS"),
        ("T1 空", judge_t1(""), "FAIL"),
        ("T2 空", judge_t2(""), "FAIL"),
        ("T2 裹代码块", judge_t2("```markdown\n" + T2_SRC + "\n```"), "FAIL"),
        ("T2 原样不动(没去重)", judge_t2(T2_SRC), "FAIL"),
        ("T2 标准答案", judge_t2(good_t2), "PASS"),
    ]
    ok = True
    for name, got, want in cases:
        good = got[0] == want
        ok &= good
        print(f"  {'✅' if good else '❌'} {name} → {got[0]}（期望 {want}）")
    print(f"\nselftest: {'全部通过' if ok else '有失败'}（{sum(1 for c in cases if c[1][0]==c[2])}/{len(cases)}）")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="含 openrouter（烧配额，默认跳过）")
    ap.add_argument("--models", default="", help="逗号分隔指定模型")
    ap.add_argument("--timeout", type=int, default=150)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    keys = list(FREE_KEYS) + ([OR_KEY] if a.all else [])
    names = [m.strip() for m in a.models.split(",") if m.strip()] or None
    models = free_models(keys, names)
    if not models:
        print("没有匹配的模型（检查 --models / --all）", file=sys.stderr)
        return 2
    if not a.all and not a.models:
        print(f"探针只跑 {len(models)} 个（{', '.join(FREE_KEYS)}）。"
              f"openrouter 实测配额仅剩 13/50，要跑请显式 --all。\n")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    import model_score
    sp = model_score.params()
    rows = []
    for m in models:
        r = probe(m, a.timeout)
        rows.append(r)
        # 自动记扣分（09-27 接上 model_score.py）——之前要人工灌，天天会忘。
        # 判定顺序：先看有没有 ERROR（请求失败），再看是否 ok，最后才看慢不慢
        # （classify 内部就是 error > fail > slow > ok 的优先级）。
        out_kind = model_score.classify(r["usable"], r.get("errored"), r.get("secs"), sp)
        model_score.record(m, out_kind, secs=r.get("secs"),
                           note=f"T1={r['t1'][0]} T2={r['t2'][0]}")
        r["score_kind"] = out_kind
        if r.get("errored"):
            mark = "⚠️ 请求失败"
        elif out_kind == "slow":
            mark = "🐢 慢(已扣分)"
        elif r["usable"]:
            mark = "✅可用"
        else:
            mark = "❌不可用"
        print(f"  {mark} {m:<44} T1={r['t1'][0]:<6}({r['t1'][1][:30]}) "
              f"T2={r['t2'][0]:<6}({r['t2'][1][:30]}) {r['secs']}s", flush=True)
        with OUT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    model_score.recompute()

    n_ok = sum(1 for r in rows if r["usable"])
    n_err = sum(1 for r in rows if r.get("errored"))
    n_bad = len(rows) - n_ok - n_err
    print(f"\n可用 {n_ok}/{len(rows)}（真不行 {n_bad} ｜ 请求失败 {n_err}）")
    if n_err:
        print(f"⚠️ 有 {n_err} 个是**请求失败**不是模型不行（端点/限流/参数），"
              f"别拿这个结果拉黑模型 —— 重跑或先查端点。")
    if n_bad and n_ok == 0:
        print("⚠️ 全部真不可用：也别急换，先看 verdict —— BADFMT 是格式问题，"
              "换模型未必能修；FAIL 才是真做错。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
