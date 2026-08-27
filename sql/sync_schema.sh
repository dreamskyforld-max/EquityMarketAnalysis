#!/usr/bin/env bash
# ============================================================================
# sync_schema.sh — 本机运行：通过 ssh -t 在目标服务器上执行数据库增量同步
# ----------------------------------------------------------------------------
# 用法:
#   ./sync_schema.sh <ROOT_HOST> [SSH_KEY] [APP_DIR]
#   # 例:
#   ./sql/sync_schema.sh 106.55.42.60
#   ./sql/sync_schema.sh 106.55.42.60 ~/.ssh/deploy_key /home/mkt/EquityMarketAnalysis
#
# 与 deploy.sh 的 root_ssh 完全同构: 内置 root 账号 + 默认 deploy_key 密钥，
# 显式 -i 指定私钥（不依赖 ~/.ssh/config 的 Host 匹配）。这样与生产机
# deploy.sh 的认证方式一致，可直接连上。
#
# 参数优先级: 命令行参数 > 环境变量 > 默认值
#   ROOT_HOST  目标服务器地址（必填，参数第1位或环境变量）
#   SSH_KEY    SSH 私钥路径（参数第2位或环境变量，默认 ~/.ssh/deploy_key）
#   APP_DIR    服务器上的项目目录（参数第3位或环境变量，默认 /home/mkt/EquityMarketAnalysis）
#
# 行为:
#   1. rsync 已由 deploy.sh 把最新 sql/schema.sql 推到服务器
#   2. 本脚本 ssh -t 到服务器，运行 APP_DIR/sql/_schema_diff.py
#      （-t 保证交互式输入 tty 可用）
#   3. 脚本内部比对 schema.sql 与真实库，交互式应用增量 DDL
# 退出码: 0 成功 / 非0 失败（deploy.sh 据此决定是否继续重启）
# ============================================================================
set -euo pipefail

# ── 参数解析: 命令行优先，环境变量兜底 ──────────────────────────────────────
ROOT_HOST="${1:-${ROOT_HOST:-}}"
SSH_KEY="${2:-${SSH_KEY:-$HOME/.ssh/deploy_key}}"
APP_DIR="${3:-${APP_DIR:-/home/mkt/EquityMarketAnalysis}}"

if [[ -z "$ROOT_HOST" ]]; then
  echo "用法: $0 <ROOT_HOST> [SSH_KEY] [APP_DIR]" >&2
  echo "  ROOT_HOST 必填（命令行第1参数或环境变量）" >&2
  exit 2
fi

# 要求 SSH_KEY 参数版本（与最初能连的版本一致）。注意: 不带 IdentitiesOnly，
# 这样当传入的密钥文件不可用时，ssh 会回退尝试 ssh-agent/默认密钥链中目标机
# 认可的密钥（当时传 ~/.ssh/deployy 不存在却仍能连上的原因）。
SSH_OPTS=(-i "$SSH_KEY" -o ConnectTimeout=15 -o LogLevel=ERROR)

echo ">>> [sync_schema] 在 $ROOT_HOST 上比对并同步数据库结构..."
echo ">>> [sync_schema] SSH_KEY=$SSH_KEY  APP_DIR=$APP_DIR"

# 用 -t 分配伪终端，保证 _schema_diff.py 的 input() 可交互
# 内置 root@（与 deploy.sh 一致），把 APP_DIR 透传到远端环境，
# 使 _schema_diff.py 能定位任意项目目录下的 config.conf / sql/schema.sql
ssh -t "${SSH_OPTS[@]}" "root@${ROOT_HOST}" \
  "export APP_DIR='${APP_DIR}'; cd '${APP_DIR}' && python3 '${APP_DIR}/sql/_schema_diff.py'"

echo ">>> [sync_schema] 数据库结构同步结束。"
