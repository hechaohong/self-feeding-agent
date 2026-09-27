#!/usr/bin/env bash
# 行动 tick：唤醒 agent 做一件有价值的小事。
# 免费档（¥0）优先并轮换；429/限流自动换下一个；全挂才降级到付费档。
#   tools/action.sh
#   ACTION_PROVIDER=opencode-go ACTION_MODEL=mimo-v2.5 tools/action.sh
set -uo pipefail
# 项目根目录从脚本位置推导（09-27 脱敏：原来写死绝对路径，公开仓库不发本机路径）
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || exit 1
mkdir -p logs
LOG="logs/action-$(date +%F).log"
LEFT=$(python3 -c "import sys;sys.path.insert(0,'tools');import common;print(common.budget()[0])" 2>/dev/null || echo 0)
# pi 的绝对路径：cron 的 PATH 里没有 ~/.npm-global/bin，裸调 "pi" = exit 127
# （09-15/09-16 两次 19:05 action tick 静默失败，日志里还写着 end ok=1）
PI="${PI_BIN:-$(command -v pi || true)}"
[ -x "${PI:-}" ] || PI="$HOME/.npm-global/bin/pi"
echo "==================== $(date '+%F %T') action tick start (budget_left=¥$LEFT) =====" >> "$LOG"

# 免费链：**从真实池生成，不手写**（09-27 改）
# 旧版这里是一份 09-21 手抄的 4 个模型（FREE_CHAIN 硬编码），已经烂掉两处：
#   · 链里的 nemotron-3-super 与当日实测活着的 nemotron-3.5-lightning 不是同一个模型
#   · 免费池是每天都在变的活物（09-21 一天少 4 个），手写链等于每天骗自己
# 现在交给 tools/free_chain.py：pi 自己的模型表 → 免费判定 → 探测记录 → 扣分 → 黑名单。
# **链为空就放弃这一 tick**（宁可不做，不用过期名单凑数；见日志）。
mapfile -t FREE_CHAIN < <(python3 tools/free_chain.py 2>>"$LOG")
echo "  free_chain: ${#FREE_CHAIN[@]} 个真实免费模型" >> "$LOG"

run_one() {  # $1 provider $2 model → 返回 0 表示这次真的跑成了（有工具调用或文本回复）
  local prov="$1" model="$2" sz out rc
  sz=$(stat -c%s "$LOG" 2>/dev/null || echo 0)
  echo "-------- try $prov/$model --------" >> "$LOG"
  timeout "${ACTION_TIMEOUT:-600}" "$PI" --provider "$prov" --model "$model" -p --no-session \
    --tools read,bash,edit,write --thinking low --mode json \
    "$(cat tools/prompts/action.md)" >> "$LOG" 2>&1
  # ⚠️ 坑（09-27 找到根因，修了 12 天）：原来写的是
  #     python3 tools/pi_cost.py ... | tail -1 ; return $?
  # 管道里 `$?` 是**最后一个命令**（tail）的退出码，而 tail 永远是 0
  #   ⇒ run_one 恒返回 0 ⇒ for 循环永远在第一个模型就 break
  #   ⇒ 不管模型干了什么，日志都写 end ok=1
  # 这正是 09-15/09-16 两次「action tick 静默失败，日志里还写着 end ok=1」的根因；
  # 当时只记了症状（journal/lessons），没找到这一行。
  # 现在把 pi_cost 的 verdict 输出和退出码**分开拿**（R27：判活看产出，pi_cost 的
  # 退出码就是按 toolcalls/texts 判的，不是看 pi 进程死没死）。
  out=$(python3 tools/pi_cost.py "$LOG" --from "$sz" --verdict 2>&1)
  rc=$?
  echo "$out" | tail -1
  return $rc
}

ok=0
if [ -n "${ACTION_PROVIDER:-}" ]; then
  run_one "$ACTION_PROVIDER" "${ACTION_MODEL:-mimo-v2.5}" && ok=1
elif [ "${#FREE_CHAIN[@]}" -eq 0 ]; then
  echo "  → 真实免费池里没选出可用模型（pi 表 × 免费判定 × 探测记录 × 扣分 全筛完为空）" >> "$LOG"
  echo "  → 本 tick 放弃。**不用过期名单凑数**（09-27 改）" >> "$LOG"
else
  for pair in "${FREE_CHAIN[@]}"; do
    run_one "${pair%%|*}" "${pair##*|}" && { ok=1; break; }
  done
  if [ "$ok" -eq 0 ]; then
    if python3 -c "import sys;sys.exit(0 if float('$LEFT')>0 else 1)"; then
      echo "  → 免费池全失败，降级付费档 mimo-v2.5" >> "$LOG"
      run_one opencode-go mimo-v2.5 && ok=1
    else
      echo "  → 免费池全失败且预算为 0：本 tick 放弃（等下一轮）" >> "$LOG"
    fi
  fi
fi

python3 tools/state.py journal "action tick ok=$ok" --tag action >/dev/null
echo "==================== $(date '+%F %T') action tick end ok=$ok =====" >> "$LOG"
[ "$ok" -eq 1 ] && rm -f state/NEED_ACTION
