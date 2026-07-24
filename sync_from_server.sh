#!/usr/bin/env bash
# ============================================================================
# 从服务器同步数据表到本机
# 用法:
#   ./sync_from_server.sh                          # 同步所有表（默认 HK.00700）
#   ./sync_from_server.sh tick_data                # 只同步 tick_data（默认 HK.00700）
#   ./sync_from_server.sh -s SH.600900              # 同步所有表（指定股票）
#   ./sync_from_server.sh -s SH.600900 tick_data    # 只同步 tick_data（指定股票）
# ============================================================================
# 策略:
#   - 小表有stock_code: DELETE WHERE stock_code + COPY (仅同步指定股票)
#   - 小表无stock_code (benchmark/northbound): TRUNCATE + pg_dump 全量
#   - 大表 (tick_data / trend_snapshot): 按日比对 + stock_code 过滤
# ============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── 参数解析 ──────────────────────────────────────────────────
STOCK_FILTER="HK.00700"          # 默认股票
TABLE_ARG=""                      # 可选：只同步指定表

while [[ $# -gt 0 ]]; do
    case "$1" in
        -s)
            STOCK_FILTER="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [-s STOCK_CODE] [TABLE_NAME]"
            echo "  -s STOCK_CODE   stock code, default HK.00700"
            echo "  TABLE_NAME      optional, sync single table (e.g. tick_data / daily_quote)"
            exit 0
            ;;
        *)
            TABLE_ARG="$1"
            shift
            ;;
    esac
done

# ── SSH ───────────────────────────────────────────────────────
SERVER_HOST="$(cd "$(dirname "$0")" && python3 -c "import config; print(config.val('sync_server','host','SERVER_HOST',''))")"
SERVER_USER="$(cd "$(dirname "$0")" && python3 -c "import config; print(config.val('sync_server','user','SERVER_USER','root'))")"

if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "$SERVER_USER@$SERVER_HOST" "echo ok" &>/dev/null; then
    echo "SSH key auth not configured, run:"
    echo "  ssh-copy-id ${SERVER_USER}@${SERVER_HOST}"
    exit 1
fi

# ── DB 配置 ───────────────────────────────────────────────────
LOCAL_HOST="localhost"
LOCAL_PORT="5432"
LOCAL_DB="market_db"
LOCAL_USER="market_user"

REMOTE_HOST="localhost"
REMOTE_PORT="5432"
REMOTE_DB="market_db"
REMOTE_USER="market_user"
REMOTE_PASS="$(cd "$(dirname "$0")" && python3 -c "import config; print(config.val('sync_server','password','REMOTE_PASS',''))")"

COMPARE_DAYS=90   # 大表比对最近多少天

# ── pgpass ────────────────────────────────────────────────────
PGPASS_LOCAL="$HOME/.pgpass_local"
rm -f "$PGPASS_LOCAL"
cat > "$PGPASS_LOCAL" << EOF
$LOCAL_HOST:$LOCAL_PORT:$LOCAL_DB:$LOCAL_USER:$REMOTE_PASS
EOF
chmod 600 "$PGPASS_LOCAL"

# ── 远程 psql (输出到 stdout) ─────────────────────────────────
# 使用 stdin 管道避免 SQL 中单引号导致 SSH 端 shell 解析错误
run_remote_sql() {
    local sql="$1"
    printf '%s\n' "$sql" | ssh "$SERVER_USER@$SERVER_HOST" \
        "PGPASSWORD='$REMOTE_PASS' psql -q -X -h '$REMOTE_HOST' -p '$REMOTE_PORT' -d '$REMOTE_DB' -U '$REMOTE_USER' -t" 2>/dev/null
}

# ── 本地 psql ────────────────────────────────────────────────
run_local_sql() {
    local sql="$1"
    PGPASSFILE="$PGPASS_LOCAL" psql -h "$LOCAL_HOST" -p "$LOCAL_PORT" -d "$LOCAL_DB" -U "$LOCAL_USER" -t -c "$sql" 2>/dev/null
}

# ── 远程 COPY TO STDOUT ──────────────────────────────────────
# 通过 stdin 传 SQL（避免 -c 的单引号嵌套问题）
run_remote_copy() {
    local sql="$1"
    printf '%s\n' "$sql" | ssh "$SERVER_USER@$SERVER_HOST" \
        "PGPASSWORD='$REMOTE_PASS' psql -q -X -h '$REMOTE_HOST' -p '$REMOTE_PORT' -d '$REMOTE_DB' -U '$REMOTE_USER'" 2>/dev/null
}

# ── 本地 COPY FROM STDIN ─────────────────────────────────────
run_local_copy() {
    local sql="$1"
    PGPASSFILE="$PGPASS_LOCAL" psql -h "$LOCAL_HOST" -p "$LOCAL_PORT" -d "$LOCAL_DB" -U "$LOCAL_USER" -c "$sql" 2>&1
}

# ── 本地执行 SQL（非查询）─────────────────────────────────────
run_local_exec() {
    local sql="$1"
    PGPASSFILE="$PGPASS_LOCAL" psql -h "$LOCAL_HOST" -p "$LOCAL_PORT" -d "$LOCAL_DB" -U "$LOCAL_USER" -c "$sql" 2>/dev/null
}

# ==================================================================
# 小表（有 stock_code）：仅同步 HK.00700
# ==================================================================
SMALL_TABLES_FILTERED=(
    "daily_quote"
    "daily_cbbc"
    "daily_short_selling"
    "daily_buyback_event"
    "daily_ggt_hold"
    "daily_margin_balance"
    "daily_trend"
    "realtime_order_size"
    "collection_run_log"
)

# ==================================================================
# 小表（无 stock_code）：全量覆盖（市场级数据）
# ==================================================================
SMALL_TABLES_FULL=(
    "daily_benchmark"
    "daily_northbound_flow"
)

sync_small_table_filtered() {
    local table="$1"
    echo "=== Sync $table ($STOCK_FILTER only) === (start: $(date '+%H:%M:%S'))"

    echo -n "  Remote rows ($STOCK_FILTER): "
    run_remote_sql "SELECT COUNT(*) FROM ${table} WHERE stock_code='${STOCK_FILTER}';" | tr -d ' '

    # get local column names (exclude id to avoid conflicts)
    local cols
    cols=$(run_local_sql "SELECT string_agg(column_name, ',' ORDER BY ordinal_position) FROM information_schema.columns WHERE table_name='${table}' AND table_schema='public' AND column_name != 'id';" | tr -d ' ')

    echo "  Deleting local $STOCK_FILTER data ..."
    run_local_exec "DELETE FROM ${table} WHERE stock_code='${STOCK_FILTER}';"

    echo "  Transferring (COPY, $(echo "$cols" | tr ',' ' ' | wc -w | tr -d ' ') cols) ..."
    local t0
    t0=$(date +%s)

    run_remote_copy "\\COPY (SELECT ${cols} FROM ${table} WHERE stock_code='${STOCK_FILTER}') TO STDOUT CSV HEADER" \
        | run_local_copy "\\COPY ${table}(${cols}) FROM STDIN CSV HEADER" \
        | tail -3
    local elapsed
    elapsed=$(($(date +%s) - t0))

    # reset sequence (id auto-generated locally, MAX(id) may shift after COPY)
    run_local_exec "SELECT setval(pg_get_serial_sequence('${table}','id'), COALESCE((SELECT MAX(id) FROM ${table}), 1));" >/dev/null

    echo -n "  Local rows (after import): "
    run_local_sql "SELECT COUNT(*) FROM ${table} WHERE stock_code='${STOCK_FILTER}';" | tr -d ' '

    echo "  $table sync done (${elapsed}s)"
    echo ""
}

sync_small_table_full() {
    local table="$1"
    echo "=== Sync $table (full) === (start: $(date '+%H:%M:%S'))"

    echo -n "  Remote rows: "
    run_remote_sql "SELECT COUNT(*) FROM ${table};" | tr -d ' '

    echo "  Truncating local ..."
    run_local_exec "TRUNCATE TABLE ${table};"

    echo "  Transferring (pg_dump --column-inserts) ..."
    local t0
    t0=$(date +%s)

    # pg_dump --column-inserts: INSERT INTO table (col1,col2,...) VALUES (...)
    # column-name-aligned, not order-dependent; works even if schemas differ
    if ssh "$SERVER_USER@$SERVER_HOST" \
        "PGPASSWORD='$REMOTE_PASS' pg_dump \
            -h '$REMOTE_HOST' -p '$REMOTE_PORT' -d '$REMOTE_DB' -U '$REMOTE_USER' \
            --data-only --column-inserts --table='$table' --no-owner --no-privileges" 2>/dev/null \
        | PGPASSFILE="$PGPASS_LOCAL" psql \
            -h "$LOCAL_HOST" -p "$LOCAL_PORT" -d "$LOCAL_DB" -U "$LOCAL_USER" \
            -v ON_ERROR_STOP=1 2>&1 | tail -3; then
        local elapsed
        elapsed=$(($(date +%s) - t0))

        echo -n "  Local rows (after import): "
        run_local_sql "SELECT COUNT(*) FROM ${table};" | tr -d ' '

        echo "  $table sync done (${elapsed}s)"
    else
        echo "  FAIL $table sync failed"
    fi
    echo ""
}

# ==================================================================
# 大表：按日比对增量
# 格式: "表名|日期列表达式|复制列(默认*)|冲突列"
#   - tick_data: sequence 全局唯一，排除 id 后自动生成
#   - trend_snapshot: snapshot_date 是生成列需排除，(stock_code,snapshot_time) 唯一
# ==================================================================
LARGE_TABLES=(
    "tick_data|tick_time::DATE|*|sequence"
    "trend_snapshot|snapshot_date|stock_code,snapshot_time,price,super_in_net,big_in_net,small_in_net,buy_sell_ratio,excess_return_pct,buy_levels_str,sell_levels_str,created_at,mid_in_net,volume,turnover|stock_code,snapshot_time"
)

sync_large_table() {
    local table="$1"
    local date_col="$2"
    local orig_cols="${3:-*}"
    local conflict_col="$4"
    echo "=== Sync $table (by-day diff) === (start: $(date '+%H:%M:%S'))"

    # get local column names (exclude id to avoid conflicts)
    local copy_cols col_spec
    if [[ "$orig_cols" == "*" ]]; then
        copy_cols=$(run_local_sql "SELECT string_agg(column_name, ',' ORDER BY ordinal_position) FROM information_schema.columns WHERE table_name='${table}' AND table_schema='public' AND column_name != 'id';" | tr -d ' ')
        col_spec="($copy_cols)"
    else
        copy_cols="$orig_cols"
        col_spec="($copy_cols)"
    fi

    # 1. remote daily row counts (filtered by stock_code)
    echo "  Querying remote last ${COMPARE_DAYS} days ($STOCK_FILTER) ..."
    local remote_raw
    remote_raw=$(run_remote_sql \
        "SELECT ${date_col}, COUNT(*) FROM ${table} WHERE stock_code='${STOCK_FILTER}' GROUP BY ${date_col} ORDER BY ${date_col} DESC LIMIT ${COMPARE_DAYS};")
    remote_raw=$(echo "$remote_raw" | sed '/^$/d' | sed 's/ //g')

    # 2. local daily row counts (filtered by stock_code)
    echo "  Querying local last ${COMPARE_DAYS} days ($STOCK_FILTER) ..."
    local local_raw
    local_raw=$(run_local_sql \
        "SELECT ${date_col}, COUNT(*) FROM ${table} WHERE stock_code='${STOCK_FILTER}' GROUP BY ${date_col} ORDER BY ${date_col} DESC LIMIT ${COMPARE_DAYS};")
    local_raw=$(echo "$local_raw" | sed '/^$/d' | sed 's/ //g')

    # 3. temp files + awk diff (compatible with bash 3.x, no associative arrays)
    local tmp_remote
    tmp_remote=$(mktemp /tmp/sync_remote_XXXXXX)
    local tmp_local
    tmp_local=$(mktemp /tmp/sync_local_XXXXXX)
    echo "$remote_raw" > "$tmp_remote"
    echo "$local_raw" > "$tmp_local"

    # awk: outputs lines "date|remote_count|local_count" for mismatched days
    local mismatch_list
    mismatch_list=$(awk -F'|' '
        NR==FNR { remote[$1]=$2; next }
        { local_cnt[$1]=$2 }
        END {
            for (d in remote) {
                lc = local_cnt[d] + 0
                if (remote[d] != lc) printf "%s|%s|%s\n", d, remote[d], lc
            }
        }
    ' "$tmp_remote" "$tmp_local")

    rm -f "$tmp_remote" "$tmp_local"

    if [[ -z "$mismatch_list" ]]; then
        echo "  OK - data consistent, skip"
        echo ""
        return
    fi

    local mismatch_count
    mismatch_count=$(echo "$mismatch_list" | wc -l | tr -d ' ')
    echo "  ${mismatch_count} days differ:"

    # 4. overwrite day by day
    local synced=0
    while IFS='|' read -r d rc lc; do
        [[ -z "$d" ]] && continue
        echo "    $d : remote=$rc  local=$lc -> overwriting..."

        local t0
        t0=$(date +%s)

        # delete local data for that day + stock
        run_local_exec "DELETE FROM ${table} WHERE ${date_col} = '$d' AND stock_code='${STOCK_FILTER}';"

        # export CSV from server to temp file
        local csv_file
        csv_file=$(mktemp /tmp/sync_${table}_XXXXXX.csv) || {
            echo "    ERROR: mktemp failed for $d, skip"
            continue
        }
        run_remote_copy "\\COPY (SELECT ${copy_cols} FROM ${table} WHERE ${date_col} = '$d' AND stock_code='${STOCK_FILTER}') TO STDOUT CSV HEADER" > "$csv_file" 2>/dev/null

        local csv_lines
        csv_lines=$(wc -l < "$csv_file" | tr -d ' ')
        if [[ "$csv_lines" -le 1 ]]; then
            echo "    No server data, skip"
            rm -f "$csv_file"
            continue
        fi

        # import via temp table, ON CONFLICT for idempotent upsert
        local import_result
        import_result=$(PGPASSFILE="$PGPASS_LOCAL" psql -h "$LOCAL_HOST" -p "$LOCAL_PORT" -d "$LOCAL_DB" -U "$LOCAL_USER" << EOF 2>&1
BEGIN;
CREATE TEMP TABLE _sync_tmp (LIKE ${table} INCLUDING DEFAULTS);
ALTER TABLE _sync_tmp DROP COLUMN id;
\\COPY _sync_tmp(${copy_cols}) FROM '${csv_file}' CSV HEADER
INSERT INTO ${table}(${copy_cols}) SELECT ${copy_cols} FROM _sync_tmp ON CONFLICT (${conflict_col}) DO NOTHING;
SELECT 'inserted' AS status, COUNT(*) AS cnt FROM _sync_tmp;
DROP TABLE _sync_tmp;
COMMIT;
EOF
        )
        rm -f "$csv_file"

        local t1
        t1=$(date +%s)
        local verify_cnt
        verify_cnt=$(run_local_sql "SELECT COUNT(*) FROM ${table} WHERE ${date_col} = '$d' AND stock_code='${STOCK_FILTER}';" | tr -d ' ')
        echo "    $d : local rows after import=$verify_cnt ($((t1-t0))s)"
        synced=$((synced + 1))
    done <<< "$mismatch_list"

    # 5. reset sequence (id auto-generated locally, MAX(id) shifted after COPY)
    if [[ "$synced" -gt 0 ]]; then
        run_local_exec "SELECT setval(pg_get_serial_sequence('${table}','id'), COALESCE((SELECT MAX(id) FROM ${table}), 1));" >/dev/null
    fi

    echo "  $table sync done (overwrote ${synced} days)"
    echo ""
}

# ==================================================================
# 查找表是否在列表中
# ==================================================================
find_and_sync() {
    local target="$1"

    for t in "${SMALL_TABLES_FILTERED[@]}"; do
        if [[ "$t" == "$target" ]]; then
            sync_small_table_filtered "$t"
            return 0
        fi
    done

    for t in "${SMALL_TABLES_FULL[@]}"; do
        if [[ "$t" == "$target" ]]; then
            sync_small_table_full "$t"
            return 0
        fi
    done

    for entry in "${LARGE_TABLES[@]}"; do
        local t="${entry%%|*}"
        local rest="${entry#*|}"
        local col="${rest%%|*}"
        rest="${rest#*|}"
        local maybe_cols="${rest%%|*}"
        local conflict="${rest#*|}"
        [[ "$maybe_cols" == "$rest" ]] && maybe_cols="*"
        [[ "$conflict" == "$rest" ]] && conflict=""
        if [[ "$t" == "$target" ]]; then
            sync_large_table "$t" "$col" "$maybe_cols" "$conflict"
            return 0
        fi
    done

    echo "Unknown table: $target"
    return 1
}

# ==================================================================
# 主流程
# ==================================================================
OVERALL_START=$(date +%s)

# 清理旧残留 temp 文件，避免 mktemp 冲突
rm -f /tmp/sync_tick_data_*.csv /tmp/sync_trend_snapshot_*.csv /tmp/sync_remote_* /tmp/sync_local_* 2>/dev/null

if [[ -n "$TABLE_ARG" ]]; then
    # 只同步指定表
    find_and_sync "$TABLE_ARG" || exit 1
else
    # 全部同步
    echo "========== Tables with stock_code ($STOCK_FILTER only) =========="
    for t in "${SMALL_TABLES_FILTERED[@]}"; do
        sync_small_table_filtered "$t"
    done

    echo "========== Market-level tables (full sync) =========="
    for t in "${SMALL_TABLES_FULL[@]}"; do
        sync_small_table_full "$t"
    done

    echo "========== Large tables (by-day diff, $STOCK_FILTER only) =========="
    for entry in "${LARGE_TABLES[@]}"; do
        t="${entry%%|*}"
        rest="${entry#*|}"
        col="${rest%%|*}"
        rest="${rest#*|}"
        maybe_cols="${rest%%|*}"
        conflict="${rest#*|}"
        [[ "$maybe_cols" == "$rest" ]] && maybe_cols="*"
        [[ "$conflict" == "$rest" ]] && conflict=""
        sync_large_table "$t" "$col" "$maybe_cols" "$conflict"
    done
fi

echo "All syncs done (total $(($(date +%s) - OVERALL_START))s)"
rm -f "$PGPASS_LOCAL"
