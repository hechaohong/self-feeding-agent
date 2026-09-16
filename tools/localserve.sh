#!/usr/bin/env bash
# 本地推理服务（夜间 / GPU 空闲时启用；白天 GPU 可能被占用，先看 status）
#   tools/localserve.sh list
#   tools/localserve.sh start [qwen27b|ornith35b|glmflash|qwen9b]   # 默认 qwen27b
#   tools/localserve.sh stop      # 停机并把电费记进账本
#   tools/localserve.sh status
set -uo pipefail
cd "$(dirname "$0")/.."   # 本脚本所在仓库根目录
BIN="${LLAMA_BIN:-$HOME/app/llama.cpp/build/bin/llama-server}"   # 改成你自己的 llama.cpp 编译产物路径
PORT=8080
PIDF=state/llama.pid
STARTF=state/llama.start
declare -A MODELS=(
  [qwen27b]="${MODEL_DIR:-$HOME/models/gguf}/qwen3.8-27b.gguf"
  [qwen27bq4]="${MODEL_DIR:-$HOME/models/gguf}/Qwen3.8-27B-Q4_K_M.gguf"
  [ornith35b]="${MODEL_DIR:-$HOME/models/gguf}/ornith-1.5-35b/model.gguf"
  [qwen9b]="${MODEL_DIR:-$HOME/models/gguf}/Qwen3.5-9B-UD-Q5_K_XL.gguf"
  [glmflash]="${MODEL_DIR:-$HOME/models/gguf}/GLM-4.7-Flash-Q4_K_M.gguf"
  [gemma26b]="${MODEL_DIR:-$HOME/models/gguf}/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf"
)

case "${1:-status}" in
  list) for k in "${!MODELS[@]}"; do printf "%-10s %s\n" "$k" "${MODELS[$k]}"; done ;;
  start)
    key="${2:-qwen27b}"; m="${MODELS[$key]:-}"
    [ -z "$m" ] && { echo "未知模型 $key"; exit 1; }
    [ -f "$PIDF" ] && kill -0 "$(cat $PIDF)" 2>/dev/null && { echo "已在运行 pid=$(cat $PIDF)"; exit 0; }
    # 回退链：27B 显存不够时自动退 9B（夜间没人守着，不能因为一张卡被占就整晚空转）
    CHAIN="$key"
    [ "$key" = qwen27b ] && CHAIN="qwen27b qwen9b"
    for k in $CHAIN; do
      mm="${MODELS[$k]:-}"; [ -z "$mm" ] && continue
      util=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits)
      echo "GPU 当前占用 ${util}MiB；启动 $k ..."
      # 坑（09-16）: 新版 llama.cpp 默认 -fit，会和用户显式 -ngl 99 冲突
      #            （failed to fit params to free device memory ... abort → HTTP 503）
      #            → 显式 -fit off；并用 q4 KV + parallel 1 把显存压下来
      nohup "$BIN" -m "$mm" --host 0.0.0.0 --port $PORT -ngl 99 -fit off \
        --ctx-size "${NIGHT_CTX:-32768}" --parallel 1 --batch-size 1024 --ubatch-size 512 \
        --threads 8 --flash-attn on --cache-type-k q4_0 --cache-type-v q4_0 --jinja \
        > "logs/llama-$k.log" 2>&1 &
      echo $! > "$PIDF"; date +%s > "$STARTF"
      ready=0
      for i in $(seq 1 90); do
        sleep 2
        code=$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health")
        [ "$code" = 200 ] && { echo "$k 就绪（$((i*2))s）"; ready=1; break; }
      done
      [ "$ready" = 1 ] && break
      echo "❌ $k 未就绪（health 非 200），看 logs/llama-$k.log，尝试下一个"
      kill "$(cat $PIDF)" 2>/dev/null; rm -f "$PIDF"
    done
    [ "$ready" = 1 ] || { rm -f "$STARTF"; echo "全部模型启动失败"; exit 1; } ;;
  stop)
    if [ -f "$PIDF" ]; then kill "$(cat $PIDF)" 2>/dev/null; rm -f "$PIDF"; fi
    if [ -f "$STARTF" ]; then
      h=$(python3 -c "import time;print(f'{(time.time()-$(cat $STARTF))/3600:.2f}')")
      python3 tools/ledger.py power "$h"
      echo "$(date '+%F %T') localserve 停机，运行 ${h}h 已记电费" >> logs/guard.log
      rm -f "$STARTF"
    fi ;;
  status)
    if [ -f "$PIDF" ] && kill -0 "$(cat $PIDF)" 2>/dev/null; then
      echo "运行中 pid=$(cat $PIDF)"; curl -s -m 3 -o /dev/null -w "health=%{http_code}\n" "http://127.0.0.1:$PORT/health"
    else echo "未运行"; fi ;;
esac
