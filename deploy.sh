#!/usr/bin/env bash
# ============================================================================
# deploy.sh — EquityMarketAnalysis 本地推送脚本（在本机 macOS 运行）
# ----------------------------------------------------------------------------
# 解决 bootstrap.sh 的前置缺陷：代码要先"手动复制到服务器"。
# 模型不变：本机=开发源，服务器=运行环境。本脚本只负责【增量推送代码】，
# 服务器侧安装/初始化仍由 bootstrap.sh（幂等）完成。
#
# 传输方式: rsync 增量同步（只传有变更的文件），--delete 清理服务器上
#           已删除的旧代码；config.conf / .venv / log / trend_cache 数据
#           等服务器侧产物通过 exclude 保护，绝不会被覆盖或删除。
#
# 用法:
#   ./deploy.sh check            预览本次将同步哪些文件（dry-run，不改服务器）
#   ./deploy.sh sync             增量推送代码到服务器（不重启服务）
#   ./deploy.sh up               sync + 重启 4 个应用服务（日常发版用这个）
#   ./deploy.sh restart [svc…]   重启服务（默认全部，可指定单个）
#   ./deploy.sh status           查看服务运行状态
#   ./deploy.sh logs [svc]       查看服务日志（最近 50 行，-f 跟随用 logs -f）
#   ./deploy.sh bootstrap        交互式重跑服务器端 bootstrap.sh
#                                （requirements.txt / system/*.service /
#                                 sql/schema.sql 变更后才需要）
#   ./deploy.sh fix-perms        修复属主：代码归 deploy-sync（供下次增量覆盖），
#                                log/trend_cache/config.conf 归还 mkt
#
# 依赖: 两把密钥分权——
#   SSH_KEY   (~/.ssh/deploy_key)      仅授权 root@106.55.42.60（restart/bootstrap/fix-perms）
#   SYNC_KEY  (~/.ssh/deploy_sync_key) 仅授权 deploy-sync@106.55.42.60（rsync 推代码）
# 代码目录归 deploy-sync 属主，服务以 mkt 身份靠 other 读权限运行；
# .venv / log / trend_cache / config.conf 保持 mkt 属主（运行时需写）。
# ============================================================================
set -euo pipefail
export LC_ALL=C

# ── 配置区 ──────────────────────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/deploy_key}"        # root 通道密钥（管理操作）
SYNC_KEY="${SYNC_KEY:-$HOME/.ssh/deploy_sync_key}" # 推送通道密钥（rsync）
SYNC_USER="deploy-sync"
ROOT_HOST="106.55.42.60"                          # 生产服务器 IP
APP_DIR="/home/mkt/EquityMarketAnalysis"          # 服务器代码目录
SERVICES=(market-scheduler ticker-collector monitor-collector wecom-collector-a)
RSYNC_SSH="ssh -i $SYNC_KEY -o IdentitiesOnly=yes -o ConnectTimeout=15"

# rsync 排除清单 = 保护服务器侧产物 + 过滤本地垃圾。
# 注意: rsync 的 exclude 同时意味着"不传输也不删除"，即保护。
#   - config.conf / .env        服务器由 bootstrap.sh 渲染的密钥，绝不覆盖
#   - .venv/                    服务器 Python 环境（~589MB，mkt 属主）
#   - log/ trend_cache/         服务器运行时数据（mkt 写），整目录排除避免
#                               rsync -p 把目录权限改回去破坏 mkt 写权限
#   - system/ 不排除！          bootstrap.sh 需要它的 service 模板
RSYNC_EXCLUDES=(
  # 服务器侧敏感/运行时产物（保护，不覆盖不删除）
  --exclude 'config.conf' --exclude 'db_config.json' --exclude 'ticker_config.json'
  --exclude '.env' --exclude '.venv/' --exclude '.git/'
  --exclude 'log/' --exclude '*.log' --exclude 'logs/'
  --exclude 'trend_cache/'
  --exclude 'south_cache_*.json' --exclude 'margin_cache_*.json'
  --exclude '*_data.txt' --exclude '*.csv'
  # 本地垃圾（与 .gitignore 对齐）
  --exclude '__pycache__/' --exclude '*.pyc' --exclude '.pytest_cache/'
  --exclude '.idea/' --exclude '.workbuddy/' --exclude '.DS_Store'
  --exclude '.coverage' --exclude '*.md' --exclude '*.code-workspace'
  --exclude 'test.py' --exclude 'test_ticker.py'
)

# ── 输出工具 ────────────────────────────────────────────────────────────────
log()  { printf '\033[0;32m[+]\033[0m %s\n' "$*"; }
info() { printf '\033[0;34m[i]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[!]\033[0m %s\n' "$*" >&2; }

root_ssh() {  # root_ssh <远程命令>
  ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o ConnectTimeout=15 "root@$ROOT_HOST" "$@"
}

# ── 交易时段保护: HK/A 股交易时段内重启会中断采集 ──────────────────────────
in_trading_hours() {
  local dow hhmm
  dow=$(date +%u); hhmm=$(date +%H%M)
  [[ $dow -ge 1 && $dow -le 5 ]] || return 1
  # 强制十进制（0900 等以 0 开头的会被 bash 误判为八进制而报错）
  hhmm=$((10#$hhmm))
  [[ $hhmm -ge 900 && $hhmm -le 1630 ]]
}

# ── rsync 核心（DRY_RUN=1 时仅预览）；变更清单写入全局 CHANGES ──────────────
CHANGES=""
do_rsync() {
  local dry="$1" out nflag=()
  if [[ "$dry" == "1" ]]; then nflag=(-n); fi
  # -rlptDz：不用 -o/-g（不把 macOS uid/gid 带过去）；-O 省去目录时间戳设置
  # （mkt 属主的目录如 trend_cache 已被 exclude，但保险起见仍加 -O 避免 exit 23）
  out=$(rsync -rlptDOz --delete --itemize-changes ${nflag[@]+"${nflag[@]}"} \
    -e "$RSYNC_SSH" "${RSYNC_EXCLUDES[@]}" \
    "$PROJECT_DIR/" "$SYNC_USER@$ROOT_HOST:$APP_DIR/" 2>&1)
  CHANGES=$(printf '%s\n' "$out" | grep -vE '^$|^\.d' || true)
  printf '%s\n' "$out"
}

show_changes_summary() {
  local n
  n=$(printf '%s' "$CHANGES" | grep -c . || true)
  if [[ "$n" -eq 0 ]]; then
    log "服务器代码已是最新，无文件变更"
  else
    log "本次涉及 $n 个文件/条目:"
    printf '%s\n' "$CHANGES" | head -20 | sed 's/^/    /'
    if [[ $n -gt 20 ]]; then info "… 及另外 $((n-20)) 个条目"; fi
  fi
}

# 基础设施文件变更检测（这些变更需要重跑 bootstrap）
# 注意: sql/schema.sql 已不在此列 —— 它由 ./deploy.sh up 的 sync_schema 自动
# 增量同步，不再需要（也不应该）靠 bootstrap 全量重建。
infra_changed() {
  [[ -z "$CHANGES" ]] && return 1
  printf '%s' "$CHANGES" | grep -qE 'requirements\.txt|(^|[^ ]/)system/|(^|[^ ]/)bootstrap\.sh$' \
    || return 1
  return 0
}

# ── 子命令 ──────────────────────────────────────────────────────────────────
cmd_check() {
  info "预览模式（dry-run，不修改服务器）"
  do_rsync 1 >/dev/null
  show_changes_summary
  if infra_changed; then
    warn "检测到 requirements.txt / system/ / bootstrap.sh 变更"
    warn "同步后 ./deploy.sh up 会自动重装依赖；如仅用 sync 需手动: ./deploy.sh bootstrap"
  fi
}

cmd_sync() {
  log "增量同步 $PROJECT_DIR → $SYNC_USER@$ROOT_HOST:$APP_DIR"
  do_rsync 0 >/dev/null
  show_changes_summary
  # 代码文件留 deploy-sync 属主（下次 rsync 增量覆盖需要）；
  # 仅把运行时需写的目录与密钥文件归还 mkt
  cmd_fix_perms_quiet
  if infra_changed; then
    warn "检测到 requirements.txt / system/ / bootstrap.sh 变更"
    warn "用 ./deploy.sh up 会自动重装依赖；如仅 sync 需手动 ./deploy.sh bootstrap"
  else
    log "代码推送完成（服务未重启，用 ./deploy.sh up 可同步并重启）"
  fi
}

cmd_fix_perms_quiet() {
  # 模型: 代码归 deploy-sync（rsync 增量覆盖需要写权限）；仅运行时数据归 mkt
  root_ssh "chown -R mkt:mkt '$APP_DIR/log' '$APP_DIR/trend_cache' 2>/dev/null; \
            chown mkt:mkt '$APP_DIR/config.conf' 2>/dev/null; \
            chmod 600 '$APP_DIR/config.conf' 2>/dev/null; true" >/dev/null
}

cmd_fix_perms() {
  log "修复属主：log/trend_cache/config.conf → mkt；代码留 deploy-sync"
  cmd_fix_perms_quiet
  root_ssh "stat -c '%n → %U:%G %a' '$APP_DIR/config.conf' '$APP_DIR/log' '$APP_DIR/trend_cache' 2>/dev/null"
}

cmd_install_reqs() {
  # 在目标机只重装 Python 依赖（bootstrap.sh STEP=venv）：跳过下载/交互步骤
  log "目标机重装依赖: bootstrap.sh STEP=venv（pip install -r requirements.txt）"
  root_ssh "cd '$APP_DIR' && STEPS=venv bash bootstrap.sh" || \
    warn "依赖安装失败，请手动在目标机执行: cd $APP_DIR && STEPS=venv bash bootstrap.sh"
}

cmd_up() {
  cmd_sync
  # ── 依赖安装：requirements.txt 变更时，sync 后自动在目标机重装 ────────────
  # 与 bootstrap 全量流程相比只跑 STEP=venv（venv+pip），不触碰 config/服务渲染，
  # 避免重复交互；幂等（pip 对未变包是 no-op）。
  if infra_changed; then
    cmd_install_reqs
  fi
  # ── 数据库增量同步（在重启服务之前）──────────────────────────────────────
  # 把最新 sql/schema.sql 已随 cmd_sync rsync 到服务器；这里交互式比对并应用
  # 新增表/列/索引/视图，破坏性变更需显式 yes。数据库改完再重启服务，避免
  # 服务启动时表结构不匹配报错。
  # 与 deploy.sh 的 root_ssh 通道一致：内置 root 账号 + 默认 deploy_key 密钥。
  if [[ -f "$PROJECT_DIR/sql/sync_schema.sh" ]]; then
    bash "$PROJECT_DIR/sql/sync_schema.sh" "$ROOT_HOST" "$SSH_KEY" "$APP_DIR"
  else
    warn "未找到 sql/sync_schema.sh，跳过数据库同步；请手动核对 schema.sql 变更"
  fi
  if in_trading_hours; then
    warn "当前处于交易时段（周一至五 09:00-16:30），重启会中断采集"
    read -r -p "仍然重启? [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || { warn "已跳过重启（代码已推送，稍后手动 ./deploy.sh restart）"; return 0; }
  fi
  cmd_restart
}

cmd_restart() {
  local svcs=("$@")
  [[ ${#svcs[@]} -eq 0 ]] && svcs=("${SERVICES[@]}")
  log "重启服务: ${svcs[*]}"
  root_ssh "systemctl restart ${svcs[*]} && systemctl is-active ${svcs[*]}"
  log "已重启并确认 active"
}

cmd_status() {
  log "服务状态（$ROOT_HOST）"
  root_ssh "for s in ${SERVICES[*]}; do printf '%-22s %s\n' \"\$s\" \"\$(systemctl is-active \$s)\"; done"
}

cmd_logs() {
  local svc="${1:-market-scheduler}" follow="${2:-}"
  local extra=(-n 50 --no-pager)
  [[ "$follow" == "-f" || "$follow" == "--follow" ]] && extra=(-f --no-pager)
  log "日志: $svc"
  root_ssh "journalctl -u '$svc' ${extra[*]}"
}

cmd_bootstrap() {
  log "交互式重跑服务器端 bootstrap.sh（幂等；复用已有 config.conf 密钥）"
  info "提示: 路径确认回答 y；config.conf 覆盖问询回答 N（保留服务器密钥）"
  ssh -t -i "$SSH_KEY" -o IdentitiesOnly=yes "root@$ROOT_HOST" \
    "cd '$APP_DIR' && bash bootstrap.sh"
}

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
}

case "${1:-}" in
  check)      cmd_check;;
  sync)       cmd_sync;;
  up)         cmd_up;;
  restart)    shift; cmd_restart "$@";;
  status)     cmd_status;;
  logs)       cmd_logs "${2:-market-scheduler}" "${3:-}";;
  bootstrap)  cmd_bootstrap;;
  fix-perms)  cmd_fix_perms;;
  help|-h|--help|"") usage;;
  *) warn "未知子命令: $1"; usage; exit 1;;
esac
