#!/usr/bin/env python3
"""
南向资金采集 — AKShare 港股通持股明细，写入 daily_ggt_hold（常驻调用版）

数据源：东方财富 → AKShare stock_hsgt_individual_em
计算方式：估算净流入 = 持股数量变动 × 当日收盘价 / 1e8（亿港元）
常驻调用：run(codes, ctx)。__main__ 保留独立运行。
"""
import sys, logging, time
from datetime import date, datetime
import akshare as ak
from db import get_conn, bulk_upsert

# ---------- 配置 ----------
DAYS = 5                     # 返回最近几个交易日
FETCH_DAYS = 10              # 拉取天数（覆盖足够的历史）

# ---------- 日志 ----------
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("south_flow")
VERBOSE = "--verbose" in sys.argv or "-v" in sys.argv


def log(msg):
    if VERBOSE:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- 核心：AKShare 获取持股并计算净流入 + 持股明细 ----------
def fetch_south_flow(symbol="00700", fetch_days=FETCH_DAYS):
    try:
        df = ak.stock_hsgt_individual_em(symbol=symbol)
    except Exception as e:
        log(f"AKShare 接口异常: {e}")
        return []

    if df is None or df.empty or len(df) < 2:
        return []

    rows = df.sort_values('持股日期').tail(fetch_days + 1).reset_index(drop=True)

    result = []
    for i in range(1, len(rows)):
        curr = rows.iloc[i]
        prev = rows.iloc[i - 1]

        trade_date = curr['持股日期']
        if hasattr(trade_date, 'isoformat'):
            date_str = trade_date.isoformat()
        else:
            date_str = str(trade_date)

        hold_num = int(curr['持股数量'])
        hold_ratio = float(curr['持股数量占A股百分比'])
        prev_num = int(prev['持股数量'])
        prev_ratio = float(prev['持股数量占A股百分比'])
        hold_num_change = hold_num - prev_num
        hold_ratio_change = round(hold_ratio - prev_ratio, 4)

        close_price = curr.get('当日收盘价')
        close_price_val = float(close_price) if close_price else None
        change_pct = curr.get('当日涨跌幅')

        net_buy = None
        if hold_num_change is not None and close_price_val is not None:
            net_buy = round(hold_num_change * close_price_val / 1e8, 2)

        result.append({
            '交易日期': date_str,
            '净买入(亿港元)': net_buy,
            '持股数量': hold_num,
            '持股比例': hold_ratio,
            '持股数量变动': hold_num_change,
            '持股比例变动': hold_ratio_change,
            '收盘价': close_price_val,
            '涨跌幅': float(change_pct) if change_pct else None,
        })

    result.sort(key=lambda x: x['交易日期'], reverse=True)
    return result[:fetch_days]


# ---------- DB 写入 ----------
def save_ggt_hold_to_db(stock_code, data):
    if not data:
        return
    try:
        db_data = []
        for entry in data:
            trade_date = entry['交易日期']
            if hasattr(trade_date, 'isoformat'):
                trade_date = trade_date.isoformat()
            db_data.append({
                "stock_code": stock_code,
                "trade_date": date.fromisoformat(trade_date),
                "hold_num": entry.get('持股数量'),
                "hold_ratio": entry.get('持股比例'),
                "hold_num_change": entry.get('持股数量变动'),
                "hold_ratio_change": entry.get('持股比例变动'),
                "close_price": entry.get('收盘价'),
                "change_pct": entry.get('涨跌幅'),
                "est_net_inflow": entry.get('净买入(亿港元)'),
            })
        with get_conn() as conn:
            bulk_upsert(conn, "daily_ggt_hold", db_data, conflict_cols=["stock_code", "trade_date"])
    except Exception as e:
        print(f"[DB] 港股通持股入库失败 ({stock_code}): {e}")


# ---------- 输出 ----------
def format_output(data):
    if not data:
        print("南向资金数据获取失败")
        return

    print("南向资金 — 每日明细")
    print(f"{'日期':<12} {'收盘价':>8} {'涨跌幅':>8} {'持股(亿)':>10} {'持股比例':>8} {'变动(万股)':>12} {'估算净流入':>10}")
    print("-" * 75)
    for entry in data:
        hp = entry.get('持股数量'); hr = entry.get('持股比例')
        hc = entry.get('持股数量变动'); cp = entry.get('收盘价')
        cg = entry.get('涨跌幅'); nb = entry.get('净买入(亿港元)')

        hp_str = f"{hp/1e8:>8.2f}亿" if hp else "N/A"
        hr_str = f"{hr:>7.2f}%" if hr is not None else "N/A"
        hc_str = f"{hc/1e4:+>10.0f}" if hc is not None else "N/A"
        cp_str = f"{cp:>8.1f}" if cp else "N/A"
        cg_str = f"{cg:>7.2f}%" if cg is not None else "N/A"
        nb_str = f"{nb:>+8.2f}亿" if nb is not None else "N/A"

        print(f"{entry['交易日期']:<12} {cp_str:>8} {cg_str:>8} {hp_str:>10} {hr_str:>8} {hc_str:>12} {nb_str:>10}")

    recent = [d for d in data if d.get('净买入(亿港元)') is not None]
    if recent:
        recent3 = recent[:3]
        avg_net = sum(d['净买入(亿港元)'] for d in recent3) / len(recent3)
        direction = "净流入" if avg_net >= 0 else "净流出"
        print(f"近{len(recent3)}日南向估算{direction}均值(亿港元): {abs(avg_net):.2f}")
        latest = recent[0]
        hp = latest.get('持股数量'); hr = latest.get('持股比例')
        if hp and hr:
            print(f"最新持股: {hp:,} 股 / {hr:.2f}%")


# ---------- 主逻辑 ----------
def get_south_flow(symbol="00700", days=DAYS):
    stock_code = f"HK.{symbol}"
    log("通过 AKShare 获取南向资金数据...")
    t0 = time.time()
    fresh_data = fetch_south_flow(symbol=symbol, fetch_days=FETCH_DAYS)
    t1 = time.time()
    log(f"AKShare 获取完成, 耗时 {t1-t0:.1f}s, 共 {len(fresh_data)} 条")

    if not fresh_data:
        print("南向资金数据获取失败")
        return []

    format_output(fresh_data[:days])
    save_ggt_hold_to_db(stock_code, fresh_data)
    log(f"总耗时 {time.time()-t0:.1f}s")
    return fresh_data[:days]


def run(codes=None, ctx=None):
    """采集入口（常驻调用）。codes: [股票代码]；ctx 未使用（数据源为 AKShare）。"""
    arg = codes[0] if (codes and len(codes) > 0) else "HK.00700"
    if "." in arg:
        _, code = arg.split(".")
        code = code.strip()
    else:
        code = arg.strip()
    get_south_flow(symbol=code)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "HK.00700"
    run([arg])
