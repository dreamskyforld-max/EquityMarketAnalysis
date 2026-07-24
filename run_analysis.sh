#!/bin/bash
# 腾讯控股(00700.HK) 一键综合分析脚本（重构版）

SKILLS_DIR="/home/hermes-agent/hermes-skills"
VENV_DIR="/home/hermes-agent/hermes-agent/venv"

source "${VENV_DIR}/bin/activate"

echo "=========================================="
echo "  腾讯控股(00700.HK) 综合数据采集"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

run_module() {
    local name="$1"
    local script="$2"
    echo ""
    echo "【${name}】"
    if [ -f "${SKILLS_DIR}/${script}" ]; then
        cd "${SKILLS_DIR}"
        python3 "${script}" 2>&1
    else
        echo "  ❌ 脚本未找到: ${script}"
    fi
}

TMPFILE=$(mktemp)

{
    echo "以下是腾讯控股(00700.HK) $(date '+%Y-%m-%d') 的最新多维数据汇总："
    echo ""

    # --- Mac 同步过来的模块（直接 cat 文件） ---
    if [ -f "${SKILLS_DIR}/quote_data.txt" ]; then
        echo "【模块1：基础行情（富途API）】"
        cat "${SKILLS_DIR}/quote_data.txt"
    else
        echo "【模块1：基础行情】❌ 数据文件未找到，请先在 Mac 上运行 sync_to_server.sh"
    fi

    if [ -f "${SKILLS_DIR}/money_flow_data.txt" ]; then
        echo ""
        echo "【模块2+3：资金流向与分布（富途API）】"
        cat "${SKILLS_DIR}/money_flow_data.txt"
    else
        echo ""
        echo "【模块2+3：资金流向】❌ 数据文件未找到，请先在 Mac 上运行 sync_to_server.sh"
    fi

    if [ -f "${SKILLS_DIR}/benchmark_data.txt" ]; then
        echo ""
        echo "【模块5：基准指数（富途API）】"
        cat "${SKILLS_DIR}/benchmark_data.txt"
    else
        echo ""
        echo "【模块5：基准指数】❌ 数据文件未找到，请先在 Mac 上运行 sync_to_server.sh"
    fi

    # --- 服务器本地模块（保持不变） ---
    run_module "模块4：南向资金个股" "get_south_flow.py"
    run_module "模块6：牛熊证街货分布" "get_cbbc.py"
    run_module "模块7：事件标记" "get_events.py"
    run_module "历史K线（近10日）" "get_kline.py"

    echo ""
    echo "--- 数据汇总完毕 ---"
} > "$TMPFILE"

# 显示采集到的原始数据
cat "$TMPFILE"

# 将数据喂给 Hermes Agent
echo ""
echo "=========================================="
echo "  Hermes Agent 综合分析报告"
echo "=========================================="

hermes -z "你是一位资深的港股分析师。以下是腾讯控股(00700.HK)今日的全面数据：
$(cat "$TMPFILE")

请你基于以上数据，从以下几个维度对腾讯控股进行综合分析：
1. 今日行情概览（涨跌幅、成交量、与恒指对比的相对强弱）
2. 南向资金动向（近期净买入趋势、对股价的影响）
3. 资金流向与分布（主力/大/中/小单动向，主动性买卖盘分析）
4. 牛熊证街货分布（市场情绪倾向、关键支撑/压力位）
5. 近期关键事件（财报日期、CFIUS等）
6. 综合研判与风险提示

请用中文输出，逻辑清晰，数据引用准确。"

rm -f "$TMPFILE"

echo ""
echo "=========================================="
echo "  分析完成 - $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="