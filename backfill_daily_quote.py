#!/usr/bin/env python3
"""
回填 daily_quote 历史数据（富途 API 版）

用法：
    python3 backfill_daily_quote.py                # 默认 SH.600900，60天
    python3 backfill_daily_quote.py SH.600900 90   # 指定股票+天数
    python3 backfill_daily_quote.py HK.00700 30

数据源：富途 OpenAPI request_history_kline
写入表：daily_quote（ON CONFLICT DO UPDATE，可重复执行）

相比腾讯API版，额外补齐：turnover(成交额)、turnover_rate(换手率)、volume_ratio(量比)、high_52w/low_52w
"""
import sys, logging
from datetime import date, datetime, timedelta
from db import get_conn, upsert

logging.basicConfig(level=logging.WARNING, format='%(message)s')
logger = logging.getLogger(__name__)


def backfill(stock_code, days=60):
    """回填指定股票的 daily_quote 历史数据"""
    from futu import OpenQuoteContext, RET_OK, KLType, AuType, KL_FIELD

    # 需要比 days 多取一些来算 52 周高低 + 首条的前收盘
    fetch_days = max(days, 260) + 5  # 250个交易日≈52周

    end = date.today().isoformat()
    start = (date.today() - timedelta(days=fetch_days)).isoformat()

    print(f"通过富途API获取 {stock_code} K线: {start} ~ {end}（含{250}天窗口算52周高低）")

    ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    try:
        # 显式指定需要 volume_ratio（默认 ALL 不包含）
        fields = [
            KL_FIELD.DATE_TIME, KL_FIELD.OPEN, KL_FIELD.CLOSE, KL_FIELD.HIGH, KL_FIELD.LOW,
            KL_FIELD.TRADE_VOL, KL_FIELD.TRADE_VAL, KL_FIELD.TURNOVER_RATE,
            KL_FIELD.CHANGE_RATE, KL_FIELD.LAST_CLOSE,
        ]
        ret, data, page = ctx.request_history_kline(
            stock_code, start=start, end=end,
            ktype=KLType.K_DAY, autype=AuType.QFQ,
            fields=fields, max_count=fetch_days,
        )
        if ret != RET_OK:
            print(f"富途API错误: {data}")
            return

        print(f"获取到 {len(data)} 条K线数据")
        data = data.sort_values('time_key').reset_index(drop=True)

        # 取最近 days 条写入（前面的用来算 52 周高低）
        write_data = data.tail(days)

        inserted = 0
        for idx, row in write_data.iterrows():
            time_key = row['time_key']
            if isinstance(time_key, str):
                trade_date = date.fromisoformat(time_key[:10])
            else:
                trade_date = time_key.date()

            close_price = float(row['close']) if row['close'] is not None else None
            open_price = float(row['open']) if row['open'] is not None else None
            high_price = float(row['high']) if row['high'] is not None else None
            low_price = float(row['low']) if row['low'] is not None else None
            volume = int(float(row['volume'])) if row['volume'] is not None else None
            turnover = float(row['turnover']) if row.get('turnover') is not None else None
            turnover_rate = float(row['turnover_rate']) if row.get('turnover_rate') is not None else None

            # volume_ratio: 显式请求的字段，可能为 None
            vol_ratio = row.get('volume_ratio')
            volume_ratio = float(vol_ratio) if vol_ratio is not None else None

            # 前收盘
            prev_close = float(row['last_close']) if row.get('last_close') is not None else None

            # 涨跌幅：富途 change_rate 是比例字段（0.008=0.8%），乘以100转为百分比
            change_rate = row.get('change_rate')
            change_pct = None
            if change_rate is not None and float(change_rate) != 0:
                change_pct = round(float(change_rate), 4)

            # 52周最高/最低：从全部数据中取最近250条计算
            high_52w = None
            low_52w = None
            window = data.iloc[max(0, idx + len(data) - days - 250): idx + len(data) - days + 1]
            if len(window) > 0:
                high_vals = window['high'].dropna()
                low_vals = window['low'].dropna()
                if len(high_vals) > 0:
                    high_52w = round(float(high_vals.max()), 4)
                if len(low_vals) > 0:
                    low_52w = round(float(low_vals.min()), 4)

            db_data = {
                "stock_code": stock_code,
                "trade_date": trade_date,
                "last_price": close_price,
                "open_price": open_price,
                "high_price": high_price,
                "low_price": low_price,
                "prev_close": prev_close,
                "change_pct": change_pct,
                "volume": volume,
                "turnover": turnover,
                "turnover_rate": turnover_rate,
                "volume_ratio": volume_ratio,
                "high_52w": high_52w,
                "low_52w": low_52w,
            }

            try:
                with get_conn() as conn:
                    upsert(conn, "daily_quote", db_data, conflict_cols=["stock_code", "trade_date"])
                inserted += 1
            except Exception as e:
                print(f"  [ERROR] {trade_date} 入库失败: {e}")

        print(f"完成：{inserted}/{len(write_data)} 条写入 daily_quote")

    finally:
        ctx.close()


if __name__ == "__main__":
    stock = sys.argv[1] if len(sys.argv) > 1 else "SH.600900"
    n_days = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    backfill(stock, n_days)
