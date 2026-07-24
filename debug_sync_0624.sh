#!/bin/bash
set -uo pipefail

SERVER_HOST="$(cd "$(dirname "$0")" && python3 -c "import config; print(config.val('sync_server','host','SERVER_HOST',''))")"
REMOTE_PASS="$(cd "$(dirname "$0")" && python3 -c "import config; print(config.val('sync_server','password','REMOTE_PASS',''))")"

# 创建 pgpass
PGPASS_FILE="$HOME/.pgpass_local"
cat > "$PGPASS_FILE" << EOF
localhost:5432:market_db:market_user:$REMOTE_PASS
EOF
chmod 600 "$PGPASS_FILE"

local_psql() {
    PGPASSFILE="$PGPASS_FILE" psql -h localhost -p 5432 -d market_db -U market_user "$@"
}

COLS="stock_code,tick_time,price,volume,turnover,ticker_direction,sequence,tick_type,created_at"
WHERE_0624="tick_time::DATE='2026-06-24' AND stock_code='HK.00700'"
WHERE_0625="tick_time::DATE='2026-06-25' AND stock_code='HK.00700'"

echo "=== 1. 本地测试导入前 5 行（看完整错误）==="
ssh root@$SERVER_HOST "PGPASSWORD='$REMOTE_PASS' psql -q -X -h localhost -p 5432 -d market_db -U market_user -c \"\\COPY (SELECT $COLS FROM tick_data WHERE $WHERE_0624 ORDER BY sequence LIMIT 5) TO STDOUT CSV HEADER\"" 2>/dev/null \
  | local_psql -c "\COPY tick_data($COLS) FROM STDIN CSV HEADER" 2>&1

echo ""
echo "=== 2. 用已下载的 6/24 文件导入 ==="
if [[ -f /tmp/tick_0624.csv ]]; then
    local_psql -c "\COPY tick_data($COLS) FROM '/tmp/tick_0624.csv' CSV HEADER" 2>&1
    echo "  验证:"
    local_psql -t -c "SELECT COUNT(*) FROM tick_data WHERE $WHERE_0624;"
else
    echo "  文件不存在，重新下载..."
    ssh root@$SERVER_HOST "PGPASSWORD='$REMOTE_PASS' psql -q -X -h localhost -p 5432 -d market_db -U market_user -c \"\\COPY (SELECT $COLS FROM tick_data WHERE $WHERE_0624 ORDER BY sequence) TO '/tmp/tick_0624.csv' CSV HEADER\"" 2>&1
    scp root@$SERVER_HOST:/tmp/tick_0624.csv /tmp/tick_0624.csv 2>&1
    local_psql -c "\COPY tick_data($COLS) FROM '/tmp/tick_0624.csv' CSV HEADER" 2>&1
    local_psql -t -c "SELECT COUNT(*) FROM tick_data WHERE $WHERE_0624;"
fi

echo ""
echo "=== 3. 同样处理 6/25 ==="
ssh root@$SERVER_HOST "PGPASSWORD='$REMOTE_PASS' psql -q -X -h localhost -p 5432 -d market_db -U market_user -c \"\\COPY (SELECT $COLS FROM tick_data WHERE $WHERE_0625 ORDER BY sequence) TO '/tmp/tick_0625.csv' CSV HEADER\"" 2>&1
scp root@$SERVER_HOST:/tmp/tick_0625.csv /tmp/tick_0625.csv 2>&1
local_psql -c "\COPY tick_data($COLS) FROM '/tmp/tick_0625.csv' CSV HEADER" 2>&1
local_psql -t -c "SELECT COUNT(*) FROM tick_data WHERE $WHERE_0625;"

echo ""
echo "完成"
