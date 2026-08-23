#!/bin/bash
cd ~/mkt/EquityMarketAnalysis || exit
source .venv/bin/activate

# 可传入股票代码，默认 HK.00700
STOCK="${1:-HK.00700}"

python3 integrated_results.py "$STOCK"

# 服务器 IP 从 config.conf 的 [sync_server] host 读取
SERVER_IP="$(cd "$(dirname "$0")" && python3 -c "import config; print(config.val('sync_server','host','SERVER_IP',''))")"
scp quote_data.txt money_flow_data.txt benchmark_data.txt hermes-agent@${SERVER_IP}:/home/hermes-agent/hermes-skills/

echo "✅ 数据已同步到服务器"