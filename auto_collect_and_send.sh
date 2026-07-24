#!/bin/bash
# 自动采集并推送到企业微信群（Webhook key 统一从 config.conf 的 wecom_webhook.key 读取，由 send_webhook.py 处理）
STOCK_CODE="${1:-HK.00700}"
SCRIPTS_DIR="/home/hermes-agent/hermes-skills"
VENV_PYTHON="/home/hermes-agent/hermes-agent/venv/bin/python3"
OUTPUT_FILE="/tmp/auto_collect_result_$$.txt"

echo "数据采集: ${STOCK_CODE} ($(date '+%Y-%m-%d %H:%M:%S'))" > "$OUTPUT_FILE"
echo "" >> "$OUTPUT_FILE"

MODULES=(
    "行情快照:get_quote.py"
    "资金流向:get_realtime_order_size.py"
    "基准指数:get_benchmark.py"
    "超额收益:get_excess_return.py"
    "南向资金:get_south_flow.py"
    "牛熊证街货:get_cbbc.py"
    "历史K线:get_kline.py"
    "融资余额:get_margin_balance.py"
    "沽空数据:get_short_selling.py"
    "趋势数据:get_trend.py"
    "全日沽空数据:get_realtime_short_selling_fullday.py"
    "公司回购:get_buyback.py"
)

for MODULE in "${MODULES[@]}"; do
    NAME="${MODULE%%:*}"
    SCRIPT="${MODULE##*:}"
    echo "[${NAME}]" >> "$OUTPUT_FILE"
    ${VENV_PYTHON} "${SCRIPTS_DIR}/${SCRIPT}" "$STOCK_CODE" 2>/dev/null | grep -v "open_context_base.py" >> "$OUTPUT_FILE"
    echo "" >> "$OUTPUT_FILE"
done

echo "采集完成时间: $(date '+%Y-%m-%d %H:%M:%S')" >> "$OUTPUT_FILE"

# 发送
${VENV_PYTHON} /home/hermes-agent/hermes-skills/send_webhook.py "$OUTPUT_FILE"

rm -f "$OUTPUT_FILE"