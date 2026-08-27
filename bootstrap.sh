#!/usr/bin/env bash
# ============================================================================
# EquityMarketAnalysis 一键部署脚本 (服务器侧)
# ----------------------------------------------------------------------------
# 模型: 本机=开发源, 服务器=运行环境。本脚本在目标 Linux 服务器上运行,
#       一切从官方源安装 (apt / PyPI / 富途官网), 不复制任何运行环境。
# 用法: 把项目代码(含本脚本)同步到目标服务器的 APP_DIR, 然后:
#       sudo bash bootstrap.sh
# 幂等: 每步已装/已存在则跳过, 可重复运行。
# 交互: 路径与密钥——有环境变量优先, 否则交互输入; 密钥绝不落进仓库。
# ============================================================================

set -euo pipefail

# ── 输出 ────────────────────────────────────────────────────────────────────
log(){  printf '\033[0;32m[+]\033[0m %s\n' "$*"; }
info(){ printf '\033[0;34m[i]\033[0m %s\n' "$*"; }
warn(){ printf '\033[0;33m[!]\033[0m %s\n' "$*" >&2; }
err(){  printf '\033[0;31m[x]\033[0m %s\n' "$*" >&2; }
confirm(){ local ans; read -r -p "$1 [y/N] " ans; [[ "$ans" =~ ^[Yy]$ ]]; }

# ── 交互/取值工具 ────────────────────────────────────────────────────────────
# 环境变量优先; 否则隐藏输入
prompt_secret(){
  local var="$1" msg="$2" val
  if [[ -n "${!var:-}" ]]; then
    info "$msg: 已提供 (环境变量/已有config), 跳过交互"
    return
  fi
  read -s -r -p "$msg: " val; echo
  printf -v "$var" '%s' "$val"
}
# 环境变量优先; 否则带默认值交互
prompt_default(){
  local var="$1" msg="$2" def="${3:-}" val
  if [[ -n "${!var:-}" ]]; then val="${!var}"
  else read -r -p "$msg [$def]: " val; val="${val:-$def}"; fi
  printf -v "$var" '%s' "$val"
}
# sed 替换值转义 (反斜杠 & 管道符)
escape_sed(){ local s="$1"; s="${s//\\/\\\\}"; s="${s//&/\\&}"; s="${s//|/\\|}"; printf '%s' "$s"; }

# ── 路径默认值 (脚本所在目录即项目代码目录) ──────────────────────────────────
APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")" && pwd)}"
APP_USER="${APP_USER:-mkt}"
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"
FUTU_USER="${FUTU_USER:-root}"
FUTU_OPEND_DIR="${FUTU_OPEND_DIR:-/opt/FutuOpenD}"
FUTU_LOGIN_ACCOUNT="${FUTU_LOGIN_ACCOUNT:-}"
DB_NAME="${DB_NAME:-market_db}"
DB_USER="${DB_USER:-market_user}"

# ── 前置检查 ────────────────────────────────────────────────────────────────
check_root(){
  [[ $EUID -eq 0 ]] || { err "请用 root 或 sudo 运行: sudo bash bootstrap.sh"; exit 1; }
}

print_banner(){
  cat <<'BANNER'
==============================================================
  EquityMarketAnalysis 一键部署
  目标: Linux 服务器 · 一切从官方源安装 (apt/PyPI/富途官网)
  幂等: 已装/已存在则跳过, 可重复运行
==============================================================
BANNER
}

# ── 路径确认 ────────────────────────────────────────────────────────────────
collect_paths(){
  info "将使用以下路径 (APP_DIR 固定为脚本所在目录, 代码应已同步到此):"
  echo "  APP_DIR         = $APP_DIR"
  echo "  APP_USER        = $APP_USER"
  echo "  VENV_DIR        = $VENV_DIR"
  echo "  FUTU_OPEND_DIR  = $FUTU_OPEND_DIR"
  echo "  DB_NAME/USER    = $DB_NAME / $DB_USER"
  confirm "使用以上路径?" || {
    prompt_default APP_USER        "APP_USER"        "$APP_USER"
    prompt_default VENV_DIR        "VENV_DIR"        "$VENV_DIR"
    prompt_default FUTU_OPEND_DIR  "FUTU_OPEND_DIR"  "$FUTU_OPEND_DIR"
    prompt_default DB_USER         "DB_USER"         "$DB_USER"
  }
  # 富途登录账号(牛牛号/手机号), service 非交互自动登录用; 环境变量优先
  prompt_default FUTU_LOGIN_ACCOUNT "富途登录账号(牛牛号/手机号)" "${FUTU_LOGIN_ACCOUNT:-}"
  echo
}

# ── 复用已有 config.conf 的密钥 (幂等重跑不重复交互) ─────────────────────────
load_existing_config(){
  local cfg="$APP_DIR/config.conf"
  [[ -f "$cfg" ]] || return 0
  info "检测到已有 config.conf, 复用其密钥 (不重新交互)"
  local k v
  while IFS='=' read -r k v; do
    [[ -z "$k" ]] && continue
    case "$k" in
      DB_PASSWORD)          : "${DB_PASSWORD:=$v}" ;;
      FRED_API_KEY)         : "${FRED_API_KEY:=$v}" ;;
      WECOM_BOT_A_ID)       : "${WECOM_BOT_A_ID:=$v}" ;;
      WECOM_BOT_A_SECRET)   : "${WECOM_BOT_A_SECRET:=$v}" ;;
      WECOM_BOT_B_ID)       : "${WECOM_BOT_B_ID:=$v}" ;;
      WECOM_BOT_B_SECRET)   : "${WECOM_BOT_B_SECRET:=$v}" ;;
      WECOM_WEBHOOK_KEY)    : "${WECOM_WEBHOOK_KEY:=$v}" ;;
      SYNC_SERVER_HOST)     : "${SYNC_SERVER_HOST:=$v}" ;;
      SYNC_SERVER_PASSWORD) : "${SYNC_SERVER_PASSWORD:=$v}" ;;
    esac
  done < <(python3 -c '
import configparser,sys
c=configparser.ConfigParser(); c.read(sys.argv[1])
def g(s,k):
    try: return c.get(s,k)
    except: return ""
for var,sec,key in [("DB_PASSWORD","database","password"),("FRED_API_KEY","fred","api_key"),("WECOM_BOT_A_ID","wecom","bot_a_id"),("WECOM_BOT_A_SECRET","wecom","bot_a_secret"),("WECOM_BOT_B_ID","wecom","bot_b_id"),("WECOM_BOT_B_SECRET","wecom","bot_b_secret"),("WECOM_WEBHOOK_KEY","wecom_webhook","key"),("SYNC_SERVER_HOST","sync_server","host"),("SYNC_SERVER_PASSWORD","sync_server","password")]:
    print(var+"="+g(sec,key))
' "$cfg" 2>/dev/null)
}

# ── 密钥收集 (环境变量/已有config优先, 否则交互; 不落仓库) ───────────────────
collect_secrets(){
  load_existing_config
  info "以下密钥: 环境变量或已有config.conf则复用, 否则交互输入 (回车=留空跳过)"
  prompt_secret DB_PASSWORD          "PostgreSQL market_user 密码 (必填)"
  prompt_secret FRED_API_KEY         "FRED API key (可空)"
  prompt_secret WECOM_BOT_A_ID       "企微 Bot A id (可空)"
  prompt_secret WECOM_BOT_A_SECRET  "企微 Bot A secret (可空)"
  prompt_secret WECOM_BOT_B_ID       "企微 Bot B id (可空)"
  prompt_secret WECOM_BOT_B_SECRET  "企微 Bot B secret (可空)"
  prompt_secret WECOM_WEBHOOK_KEY    "企微 webhook key (可空)"
  prompt_secret SYNC_SERVER_HOST     "同步服务器 host (可空)"
  prompt_secret SYNC_SERVER_PASSWORD "同步服务器密码 (可空)"
  echo
  [[ -n "$DB_PASSWORD" ]] || { err "DB_PASSWORD 不能为空"; exit 1; }
}

# ── [1/8] 系统依赖 (dnf · OpenCloudOS 9) ───────────────────────────────────
step_apt(){
  log "[1/8] 安装系统依赖 (dnf · OpenCloudOS 9)"
  dnf install -y epel-release >/dev/null 2>&1 || true
  # -devel 包常在 CRB 仓库(RHEL9系), 默认未启用
  dnf install -y dnf-plugins-core >/dev/null 2>&1 || true
  dnf config-manager --set-enabled crb >/dev/null 2>&1 || true
  dnf install -y \
    postgresql-server postgresql-contrib \
    python3 python3-devel \
    gcc make \
    libpq-devel libxml2-devel libxslt-devel \
    wget unzip >/dev/null
  log "系统依赖就绪"
}

# ── [2/8] 应用用户 ────────────────────────────────────────────────────────────
step_user(){
  log "[2/8] 确保用户 $APP_USER"
  if id -u "$APP_USER" >/dev/null 2>&1; then
    log "用户 $APP_USER 已存在"
  else
    useradd -m -s /bin/bash "$APP_USER"
    log "已创建用户 $APP_USER"
  fi
  chown -R "$APP_USER":"$APP_USER" "$APP_DIR" 2>/dev/null || true
  # 家目录本身也要归用户(futu 库写 $HOME/.com.futunn 日志); 手动建的 mkt 家目录 owner 可能仍是 root
  chown -R "$APP_USER":"$APP_USER" "/home/$APP_USER" 2>/dev/null || true
  # market_scheduler 每天 05:00 需 sudo restart FutuOpenD(缓解长时间运行泄漏), 给限定命令免密 sudo
  echo "$APP_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart FutuOpenD.service" \
    > /etc/sudoers.d/equity-futu && chmod 440 /etc/sudoers.d/equity-futu
}

# ── [3/8] PostgreSQL 建库建用户 + 导入 schema ────────────────────────────────
step_pg(){
  log "[3/8] PostgreSQL 建库建用户"
  # CentOS: postgresql-server 需先 initdb 再 start (已初始化则 start 直接成功)
  systemctl start postgresql >/dev/null 2>&1 || {
    postgresql-setup --initdb >/dev/null 2>&1 || postgresql-setup initdb >/dev/null 2>&1 || true
    systemctl start postgresql >/dev/null 2>&1 || true
  }
  systemctl enable postgresql >/dev/null 2>&1 || true
  sleep 1
  if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
    sudo -u postgres psql -qc "CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD';"
    log "已创建 PG 用户 $DB_USER"
  else
    sudo -u postgres psql -qc "ALTER USER $DB_USER WITH PASSWORD '$DB_PASSWORD';" >/dev/null
    log "PG 用户 $DB_USER 已存在 (已重设密码)"
  fi
  if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
    sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"
    log "已创建库 $DB_NAME"
  else
    log "库 $DB_NAME 已存在"
  fi
  if [[ -f "$APP_DIR/sql/schema.sql" ]]; then
    sudo -u postgres psql -d "$DB_NAME" -qf "$APP_DIR/sql/schema.sql" >/dev/null 2>&1 || true
    sudo -u postgres psql -d "$DB_NAME" -qc \
      "GRANT ALL ON ALL TABLES IN SCHEMA public TO $DB_USER;
       GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO $DB_USER;" >/dev/null
    log "schema.sql 导入完成"
  else
    warn "缺 $APP_DIR/sql/schema.sql, 跳过建表"
  fi
  # CentOS 系 PG 默认 host 行 ident 认证(OS用户==DB用户), mkt 用户连 market_user 库用户需密码认证 → 改 md5
  local hba
  hba=$(sudo -u postgres psql -tAc "SHOW hba_file" | tr -d '[:space:]')
  if [[ -n "$hba" && -f "$hba" ]] && grep -qE 'ident[[:space:]]*$' "$hba"; then
    cp "$hba" "$hba.bak"
    sed -i 's/ident[[:space:]]*$/md5/' "$hba"
    systemctl restart postgresql >/dev/null 2>&1 || true
    log "pg_hba.conf host 认证已由 ident 改 md5 (密码认证)"
  fi
}

# ── [4/8] Python venv + pip install ────────────────────────────────────────────
step_venv(){
  log "[4/8] Python venv + 依赖"
  if [[ ! -x "$VENV_DIR/bin/python3" ]]; then
    sudo -H -u "$APP_USER" python3 -m venv "$VENV_DIR"
    log "已创建 venv $VENV_DIR"
  else
    log "venv 已存在"
  fi
  if [[ -f "$APP_DIR/requirements.txt" ]]; then
    # 国内 PyPI 镜像(腾讯云内网最快); install 用 -i 命令行指定(优先级最高, 不依赖 config set 成败)
    local idx="${PIP_INDEX_URL:-https://mirrors.cloud.tencent.com/pypi/simple}"
    local th="${PIP_INDEX_HOST:-mirrors.cloud.tencent.com}"
    # 顺手写 venv 级配置(以后手动 pip install 也走镜像), 失败不致命
    sudo -H -u "$APP_USER" "$VENV_DIR/bin/pip" config set global.index-url "$idx" >/dev/null 2>&1 || true
    sudo -H -u "$APP_USER" "$VENV_DIR/bin/pip" config set global.trusted-host "$th" >/dev/null 2>&1 || true
    log "PyPI 镜像: $idx"
    # --no-cache-dir 避开 /home/mkt/.cache/pip 权限告警, 镜像内网快无需缓存
    sudo -H -u "$APP_USER" "$VENV_DIR/bin/pip" install --upgrade pip \
      -i "$idx" --trusted-host "$th" --no-cache-dir >/dev/null 2>&1 || true
    sudo -H -u "$APP_USER" "$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt" \
      -i "$idx" --trusted-host "$th" --no-cache-dir
    log "pip 依赖安装完成"
  else
    warn "缺 requirements.txt, 跳过"
  fi
}

# ── [5/8] FutuOpenD (富途官方渠道下载解压, 登录需手动) ────────────────────────
step_futu(){
  log "[5/8] FutuOpenD (富途官方)"
  if [[ -x "$FUTU_OPEND_DIR/FutuOpenD" ]]; then
    log "FutuOpenD 已存在于 $FUTU_OPEND_DIR, 跳过下载"
  else
    mkdir -p "$FUTU_OPEND_DIR"
    local tmpdir; tmpdir=$(mktemp -d)
    local url="https://www.futunn.com/download/fetch-lasted-link?name=opend-centos"
    info "下载 FutuOpenD CentOS 版 (约 445MB, 自动取最新, 请耐心等待) ..."
    if ! wget --show-progress -O "$tmpdir/opend.pkg" "$url"; then
      warn "自动下载失败。请手动从富途官网下载 CentOS 版解压到 $FUTU_OPEND_DIR"
      warn "下载页: https://openapi.futunn.com/futu-api-doc/opend/opend-cmd.html"
      rm -rf "$tmpdir"
      return 1
    fi
    tar -xzf "$tmpdir/opend.pkg" -C "$tmpdir" 2>/dev/null \
      || unzip -o -q "$tmpdir/opend.pkg" -d "$tmpdir" 2>/dev/null || true
    local bin; bin=$(find "$tmpdir" -name FutuOpenD -type f 2>/dev/null | head -1)
    if [[ -n "$bin" ]]; then
      # 复制二进制所在目录全部内容(含 .so 库如 libf3clogin.so / xml / dat); 只 cp 3 个文件会缺库
      cp -a "${bin%/*}"/. "$FUTU_OPEND_DIR/"
      chmod +x "$FUTU_OPEND_DIR/FutuOpenD"
      log "FutuOpenD 已安装到 $FUTU_OPEND_DIR"
    else
      warn "解压后未找到 FutuOpenD 二进制, 请检查 $tmpdir 手动处理"
      rm -rf "$tmpdir"
      return 1
    fi
    rm -rf "$tmpdir"
  fi
  warn "FutuOpenD 首次需手动交互登录 (账号密码, 选记住密码):"
  warn "  cd $FUTU_OPEND_DIR && ./FutuOpenD"
  warn "登录成功并记住密码后, 可由 systemd 常驻启动 (见步骤 8)。"
}

# ── [6/8] 渲染 config.conf (密钥外置, 不落仓库) ───────────────────────────────
render_config(){
  log "[6/8] 渲染 config.conf"
  local tpl="$APP_DIR/config.example.conf"
  local out="$APP_DIR/config.conf"
  [[ -f "$tpl" ]] || { err "找不到模板 $tpl"; return 1; }
  if [[ -f "$out" ]]; then
    warn "$out 已存在"
    confirm "覆盖?" || { log "跳过 config 渲染"; return 0; }
  fi
  sed \
    -e "s|YOUR_DB_USER|$(escape_sed "$DB_USER")|g" \
    -e "s|YOUR_DB_PASSWORD|$(escape_sed "$DB_PASSWORD")|g" \
    -e "s|YOUR_FRED_API_KEY|$(escape_sed "${FRED_API_KEY:-}")|g" \
    -e "s|YOUR_WECOM_BOT_A_ID|$(escape_sed "${WECOM_BOT_A_ID:-}")|g" \
    -e "s|YOUR_WECOM_BOT_A_SECRET|$(escape_sed "${WECOM_BOT_A_SECRET:-}")|g" \
    -e "s|YOUR_WECOM_BOT_B_ID|$(escape_sed "${WECOM_BOT_B_ID:-}")|g" \
    -e "s|YOUR_WECOM_BOT_B_SECRET|$(escape_sed "${WECOM_BOT_B_SECRET:-}")|g" \
    -e "s|YOUR_WECOM_WEBHOOK_KEY|$(escape_sed "${WECOM_WEBHOOK_KEY:-}")|g" \
    -e "s|YOUR_SYNC_SERVER_HOST|$(escape_sed "${SYNC_SERVER_HOST:-}")|g" \
    -e "s|YOUR_SYNC_SERVER_PASSWORD|$(escape_sed "${SYNC_SERVER_PASSWORD:-}")|g" \
    "$tpl" > "$out"
  chmod 600 "$out"
  chown "$APP_USER":"$APP_USER" "$out" 2>/dev/null || true
  log "config.conf 已生成 (权限 600)"
}

# ── [7/8] 渲染 service 模板 → /etc/systemd/system ─────────────────────────────
render_services(){
  log "[7/8] 渲染 service 模板"
  local svcdir=/etc/systemd/system
  local services=(FutuOpenD market-scheduler ticker-collector monitor-collector wecom-collector-a)
  for s in "${services[@]}"; do
    local tpl="$APP_DIR/system/$s.service"
    [[ -f "$tpl" ]] || { warn "缺 $tpl, 跳过 $s"; continue; }
    sed \
      -e "s|{{APP_USER}}|$(escape_sed "$APP_USER")|g" \
      -e "s|{{APP_DIR}}|$(escape_sed "$APP_DIR")|g" \
      -e "s|{{VENV_DIR}}|$(escape_sed "$VENV_DIR")|g" \
      -e "s|{{FUTU_USER}}|$(escape_sed "$FUTU_USER")|g" \
      -e "s|{{FUTU_OPEND_DIR}}|$(escape_sed "$FUTU_OPEND_DIR")|g" \
      -e "s|{{FUTU_LOGIN_ACCOUNT}}|$(escape_sed "$FUTU_LOGIN_ACCOUNT")|g" \
      "$tpl" > "$svcdir/$s.service"
    log "  已渲染 $s.service → $svcdir"
  done
  systemctl daemon-reload
}

# ── [8/8] enable 服务 ────────────────────────────────────────────────────────
enable_services(){
  log "[8/8] 启用服务"
  local app_svcs=(market-scheduler ticker-collector monitor-collector wecom-collector-a)
  for s in "${app_svcs[@]}"; do
    systemctl enable --now "$s" >/dev/null 2>&1 \
      && log "  $s 已启用" \
      || warn "  $s 启用失败 (检查 FutuOpenD 是否已登录 / 日志 journalctl -u $s)"
  done
  # FutuOpenD 需先手动登录
  if [[ -x "$FUTU_OPEND_DIR/FutuOpenD" ]]; then
    if confirm "FutuOpenD 是否已完成首次登录并启动? (若否则先 cd $FUTU_OPEND_DIR && ./FutuOpenD)"; then
      systemctl enable --now FutuOpenD >/dev/null 2>&1 \
        && log "  FutuOpenD 已启用" \
        || warn "  FutuOpenD 启用失败"
    else
      warn "FutuOpenD 暂未启动, 登录后执行: systemctl enable --now FutuOpenD"
    fi
  else
    warn "FutuOpenD 二进制缺失, 跳过其服务启用"
  fi
}

# ── [可选] 从 pg_dump 文件迁移历史数据 ──────────────────────────
restore_db(){
  local dump="${DUMP_FILE:-/tmp/market_db.dump}"
  [[ -f "$dump" ]] || { log "未发现 dump 文件 $dump, 跳过数据迁移 (可用环境变量 DUMP_FILE 指定)"; return 0; }
  info "检测到 dump 文件: $dump"
  confirm "是否从中迁移历史数据? (将清空并重建 $DB_NAME)" || { log "跳过数据迁移"; return 0; }

  log "[迁移] 停服务 → 删库重建 → restore → 授权"
  # 1. 停服务(避免 restore 时写入冲突)
  systemctl stop market-scheduler ticker-collector monitor-collector wecom-collector-a 2>/dev/null || true
  # 2. 删库重建(最干净, 避开 --clean 竞态); 有残留连接则先 terminate
  if ! sudo -u postgres dropdb --force "$DB_NAME" 2>/dev/null; then
    sudo -u postgres psql -d postgres -qc \
      "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$DB_NAME' AND pid <> pg_backend_pid();" 2>/dev/null || true
    sudo -u postgres dropdb "$DB_NAME" 2>/dev/null || true
  fi
  sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"
  # 3. restore (--jobs 并行加速, 仅 -Fc 格式支持)
  sudo -u postgres pg_restore -d "$DB_NAME" --jobs 4 "$dump"
  # 4. 重授权(源库部分表 owner 是 postgres)
  sudo -u postgres psql -d "$DB_NAME" -qc \
    "GRANT ALL ON ALL TABLES IN SCHEMA public TO $DB_USER;
     GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO $DB_USER;" >/dev/null
  log "[迁移] 数据迁移完成 (服务将在最后统一启动)"
}

# ── 完成 ────────────────────────────────────────────────────────────────────
summary(){
  echo
  log "部署完成。验证:"
  echo "  systemctl status FutuOpenD market-scheduler ticker-collector monitor-collector wecom-collector-a"
  echo "  journalctl -u market-scheduler -f --since '5 min ago'"
  echo "  sudo -u postgres psql -d $DB_NAME -c 'SELECT COUNT(*) FROM stock_info;'"
  echo
  info "关键路径:"
  echo "  项目代码   $APP_DIR"
  echo "  venv       $VENV_DIR"
  echo "  config     $APP_DIR/config.conf (600, 密钥已渲染)"
  echo "  FutuOpenD  $FUTU_OPEND_DIR"
  echo "  services   /etc/systemd/system/*.service"
  echo
  warn "迁移历史数据(可选): 把旧库 dump 放到 /tmp/market_db.dump 后重跑本脚本即可自动迁移,"
  echo "  或用环境变量 DUMP_FILE=/path/to.dump 指定; 脚本会自动停服务→删库重建→restore→授权。"
  echo "  旧库导出: sudo -u postgres pg_dump -Fc -d $DB_NAME -f /tmp/market_db.dump"
}

# ── 主流程 ──────────────────────────────────────────────────────────────────
main(){
  print_banner
  check_root
  collect_paths
  collect_secrets
  # 仅运行指定步骤：deploy.sh up 用 STEP=venv 只重装依赖，跳过其余交互/下载步骤
  if [[ -n "${STEPS:-}" ]]; then
    local IFS=',' step
    for step in $STEPS; do
      case "$step" in
        apt)        step_apt ;;
        user)       step_user ;;
        pg)         step_pg ;;
        restore)    restore_db ;;
        venv)       step_venv ;;
        futu)       step_futu || warn "FutuOpenD 安装未完成, 可后续手动补" ;;
        config)     render_config || warn "config.conf 渲染失败" ;;
        services)   render_services ;;
        enable)     enable_services ;;
        *)          warn "未知步骤: $step (忽略)" ;;
      esac
    done
    summary_skip
    return 0
  fi
  step_apt
  step_user
  step_pg
  restore_db
  step_venv
  step_futu || warn "FutuOpenD 安装未完成, 可后续手动补"
  render_config || warn "config.conf 渲染失败"
  render_services
  enable_services
  summary
}

# 仅跑部分步骤时的精简结束语（不打印首次部署的迁移提示）
summary_skip(){
  log "指定步骤执行完成。"
}

main "$@"
