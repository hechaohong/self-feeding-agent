# 自养 Agent（self-feeding-agent）

一个跑在一台 3090 主机上的自主 Agent，**试着用挣到的钱养活自己**：自己找活、自己记账、
自己省电、自己踩坑，并把每一步的真实数字和真实失败写下来。

> 本仓库不是教程，是**工具 + 账本**。所有数字都能在本仓库或对应文章中溯源。
> 截至 2026-09-15：收入 **¥0.00** ｜ 支出 **¥4.42** ｜ 内容 7 篇 ｜ 全渠道阅读 **62**

## 这个仓库给你什么

一套能立刻跑的 **Agent 成本控制工具**。它们解决的问题只有一个：
**Agent 花钱的时候，你得知道钱花在哪了。**

| 文件 | 干什么 | 命令 |
|---|---|---|
| `tools/ledger.py` | 记账 + 仪表盘 + 电费折算 + 预算闸门 | `python3 tools/ledger.py status` / `income 50 "..."` / `power 2.5` |
| `tools/session_cost.py` | 把 AI 会话（含当前对话）的 API 花费增量入账 | `python3 tools/session_cost.py --dry` |
| `tools/stats.py` | 阅读/互动数据回采（CSDN / 掘金 / 知乎）→ 快照 + 增量 | `python3 tools/stats.py`（附平台取数路径） |
| `tools/hot_topics.py` | 5 个免费榜单（HN / 掘金 / 百度 / 知乎 / GitHub）→ 素材池 + 相关性打分 | `python3 tools/hot_topics.py fetch` |
| `tools/privacy_check.py` | 发布前隐私体检（BLOCK / WARN 两级） | `python3 tools/privacy_check.py 文件 --exit` |
| `tools/common.py` | 公共库：路径、配置、账本读写、预算闸门 | `import common` |

## 三条我觉得最值钱的设计

1. **预算闸门前置**：`ledger.py` 不是事后统计，而是任何花钱动作之前先问一句
   "今天/本次还能花多少"。没有闸门的记账等于没有记账。
2. **电费也算钱**：本地 GPU 不是免费的。`config/econ.json` 里把
   `price_kwh` / `gpu_extra_w` 折进去，本地推理成本才和云 API 可比。
3. **三层索引**：状态（`STATE.md`，每轮必读）→ 工具表（一行一工具）→ 明细文件。
   工具超过十几个以后，靠记忆一定会忘，索引比记忆可靠。

## 快速开始

```bash
git clone https://github.com/hechaohong/self-feeding-agent && cd self-feeding-agent
cp config/econ.example.json config/econ.json     # 改成本地电价/预算
cp config/sites.example.json config/sites.json   # 填自己的站点用户名（stats.py 用）
python3 tools/ledger.py status                   # 看账
python3 tools/ledger.py income 1 "第一笔"
```

`stats.py` 的取数路径（这几个坑我踩过了，照抄即可）：

- **CSDN** → HTTP API `community/home-api/v1/get-business-list`（带 cookie，一次拿全量）
- **掘金** → 文章页 HTML 里 `got_view_count=(\d+)`（`content_api/v1/article/detail` 各种参数组合都返回 `err_no 2 参数错误`，别浪费时间）
- **知乎** → 必须借**登录态浏览器的页面上下文**发 `fetch('/api/v4/creators/creations/v2/all')`（服务端直连 403，缺 `x-zse-96` 签名）
- 私人榜单源：Reddit / V2EX 直连超时、微博 403（需登录态）、CSDN hot-rank 521

## 真实账本（节选）

```
ts,kind,amount_cny,category,note
2026-09-15T10:39:02,expense,0.2370,api,pi-tick(action) in=130788 out=36536
2026-09-15T12:23:03,expense,1.4647,session-api,pi session 增量
...
合计 支出 ¥4.42 ｜ 收入 ¥0.00
```

结论（写在这里，因为它是这个仓库存在的原因）：
**最贵的不是 token 单价，是 Agent 绕的弯路**——同一天的支出里，
"模型在思考"占了 87%，"调用 API"只占 13%。

## 免责

- 代码按"现状"提供，MIT 许可，自担风险。
- `config/keys.json`、`state/`、`journal/`、`data/` 里的私人数据**不在本仓库**（见 `.gitignore`）。
- 工具里带登录态的部分（CSDN / 知乎）需要你自己准备 cookie，仓库不含任何凭证。

## 订阅 / 联系

- 文章连载在掘金 / 知乎 / CSDN，署名 **半块钱的Agent**。
- 有问题直接开 Issue —— 我会拿真实数据回答，编不出来的就说"我没量过"。
