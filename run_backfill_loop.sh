#!/usr/bin/env bash
#
# backfill_market_turnover.py 的「自动断点续采循环」包装脚本。
#
# 作用：
#   用小内存友好的 --resume --limit 分批跑 backfill_market_turnover.py，
#   每轮结束自动重算全市场总成交额并打印进度，进度达到 100% 即停止。
#   进程被 OOM kill / 网络抖动导致中断都没关系：下一轮 --resume 会跳过
#   已完成股票、并从中断处那只重采（upsert 幂等），因此可放心重复执行。
#
# 用法：
#   ./run_backfill_loop.sh                 # 默认近 3 年，每轮 300 只
#   START=2023-01-01 BATCH=500 ./run_backfill_loop.sh
#   MEM_LIMIT=300 ./run_backfill_loop.sh   # 用 systemd-run 限制 300MB（默认不限制）
#
set -u

# ---------- 可调参数 ----------
START="${START:-}"            # 回溯起始日期，空=近 3 年（脚本默认）
BATCH="${BATCH:-300}"         # 每轮采集多少只
MAX_ROUNDS="${MAX_ROUNDS:-100}"   # 最多轮次（防御性上限，正常不会到）
SLEEP_BETWEEN="${SLEEP_BETWEEN:-3}"  # 轮次间隔秒数
MEM_LIMIT="${MEM_LIMIT:-}"    # 非空则包 systemd-run --scope -p MemoryMax=${MEM_LIMIT}M
# --------------------------------

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
PY="${VIRTUAL_ENV:+${VIRTUAL_ENV}/bin/python3}"
PY="${PY:-python3}"

# 构造基础命令
BASE_ARGS=()
if [ -n "$START" ]; then
  BASE_ARGS+=(--start "$START")
else
  BASE_ARGS+=(--years 3)
fi

# 进度解析：从日志行 "进度：n/total 只已采集（pct%）"
parse_progress() {
  # 返回 "n total pct" 三个值
  "$PY" backfill_market_turnover.py "${BASE_ARGS[@]}" --aggregate-only 2>&1 \
    | grep -E "进度：" \
    | tail -1 \
    | sed -E 's/.*进度：([0-9]+)\/([0-9]+) 只已采集（([0-9.]+)%）。*/\1 \2 \3/'
}

run_one_round() {
  local args=(backfill_market_turnover.py "${BASE_ARGS[@]}" --resume --limit "$BATCH")
  if [ -n "$MEM_LIMIT" ]; then
    # 限制内存 + 禁 swap，避免拖死同机常驻进程（ticker_collector / postgres）
    systemd-run --scope -p MemoryMax="${MEM_LIMIT}M" -p MemorySwapMax=0 \
      "$PY" "${args[@]}"
  else
    "$PY" "${args[@]}"
  fi
}

echo "=== backfill 循环启动 | start=${START:-3y} batch=$BATCH mem=${MEM_LIMIT:-unlimited} ==="

round=0
while [ "$round" -lt "$MAX_ROUNDS" ]; do
  round=$((round + 1))
  echo ""
  echo "----- 第 $round 轮（limit=$BATCH）-----"

  if ! run_one_round; then
    echo "[warn] 第 $round 轮非零退出（可能被 kill / 网络抖动），下一轮 --resume 续采"
  fi

  # 解析进度
  prog="$(parse_progress)"
  n="$(echo "$prog" | awk '{print $1}')"
  total="$(echo "$prog" | awk '{print $2}')"
  pct="$(echo "$prog" | awk '{print $3}')"

  if [ -z "$total" ] || [ "$total" = "0" ]; then
    echo "[error] 无法获取进度（可能 DB 连不上），中止。"
    exit 1
  fi

  echo ">>> 进度：$n / $total 只（${pct}%）"

  # 数值比较（pct 可能带小数，用 awk）
  done_pct="$(awk "BEGIN{print ($n/$total)*100}")"
  if awk "BEGIN{exit !($done_pct >= 100)}"; then
    echo ""
    echo "=== 全部采集完成（100%），循环结束 ==="
    exit 0
  fi

  echo ">>> 未到 100%，${SLEEP_BETWEEN}s 后进入下一轮..."
  sleep "$SLEEP_BETWEEN"
done

echo "[warn] 达到 MAX_ROUNDS=$MAX_ROUNDS 仍未 100%，请检查日志或提高上限后重跑。"
exit 2
