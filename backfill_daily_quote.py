#!/usr/bin/env python3
"""
回填 daily_quote 历史数据（富途 API 版，支持大跨度自动分页）

用法：
    python3 backfill_daily_quote.py                   # 默认 SH.600900，60天
    python3 backfill_daily_quote.py HK.00700 365      # 指定股票+天数
    python3 backfill_daily_quote.py HK.00700 2000     # 大跨度自动分页补录

数据源：富途 OpenAPI request_history_kline（主）；A 股额度不足/空数据时自动回退
      akshare stock_zh_a_daily（新浪源，无额度限制，仅 SH./SZ. 适用）
写入表：daily_quote（ON CONFLICT DO UPDATE，可重复执行）

相比腾讯API版，额外补齐：turnover(成交额)、turnover_rate(换手率)、volume_ratio(量比)、high_52w/low_52w
"""
import sys, logging
from datetime import date, datetime, timedelta
from db import get_conn, upsert

logging.basicConfig(level=logging.WARNING, format='%(message)s')
logger = logging.getLogger(__name__)

# 富途单次请求最大 K 线条数（避免超限）
FUTU_MAX_BARS = 800


def _fetch_batch(ctx, stock_code, start_date: str, end_date: str, fields) -> list:
    """单批拉取 K 线，返回 DataFrame 或空列表。"""
    from futu import RET_OK, KLType, AuType
    ret, data, page = ctx.request_history_kline(
        stock_code, start=start_date, end=end_date,
        ktype=KLType.K_DAY, autype=AuType.QFQ,
        fields=fields, max_count=FUTU_MAX_BARS,
    )
    if ret != RET_OK:
        print(f"  富途API错误 ({start_date}~{end_date}): {data}")
        return []
    return data.sort_values('time_key').reset_index(drop=True) if len(data) > 0 else []


def _is_a_share(stock_code: str) -> bool:
    """是否为 A 股（SH./SZ.）。港股仅富途源，不适用 akshare 新浪回退。"""
    return stock_code.upper().startswith(("SH.", "SZ."))


def _akshare_symbol(stock_code: str) -> str:
    """SH.688825 -> sh688825（akshare 新浪源代码形态）。"""
    market, _, sym = stock_code.partition(".")
    return market.lower() + sym


def _fetch_from_akshare(stock_code: str, days: int):
    """
    A 股回退数据源：akshare stock_zh_a_daily（新浪源，无富途额度限制）。

    返回一个与富途 _fetch_batch 同构的 DataFrame（列名对齐 time_key/open/close/
    high/low/volume/last_close/change_rate），以便直接复用下游写入逻辑。
    不会抛“额度不足”，但可能因网络/接口失败抛异常（由调用方捕获后退出码置 1）。
    """
    import akshare as ak
    import pandas as pd

    # 多取 ~300 自然日用于 52 周高低窗口 + 边界
    start = (date.today() - timedelta(days=days + 300)).isoformat()
    end = date.today().isoformat()
    symbol = _akshare_symbol(stock_code)
    print(f"  富途额度不足/空数据，回退 akshare 新浪源获取 {symbol}（{start}~{end}）")
    df = ak.stock_zh_a_daily(symbol=symbol, adjust="qfq",
                             start_date=start, end_date=end)
    if df is None or len(df) == 0:
        print("  akshare 新浪源也未返回数据")
        return []

    df = df.rename(columns={"date": "time_key"})
    df["time_key"] = pd.to_datetime(df["time_key"]).dt.strftime("%Y-%m-%d")
    # 复权后涨跌幅：用前一日收盘价推算（首行无前值，置 None）
    prev_close = df["close"].shift(1)
    df["last_close"] = prev_close
    df["change_rate"] = ((df["close"] - prev_close) / prev_close * 100).round(4)
    # 新浪源缺失字段置 None（下游写入会落入 daily_quote 对应空列）
    df["turnover"] = None
    df["turnover_rate"] = None
    df["volume_ratio"] = None
    # 仅保留下游写入逻辑消费的列
    keep = ["time_key", "open", "close", "high", "low", "volume",
            "turnover", "turnover_rate", "volume_ratio", "last_close", "change_rate"]
    return df[keep].sort_values("time_key").reset_index(drop=True)


def backfill(stock_code, days=60):
    """回填指定股票的 daily_quote 历史数据（自动分页支持大跨度）。"""
    from futu import OpenQuoteContext, KL_FIELD
    import pandas as pd

    # 52 周高低需要往前多取 ~250 个交易日
    pad_days = 260 + 5
    fetch_calendar_days = days + pad_days

    end = date.today()
    start = end - timedelta(days=fetch_calendar_days)

    fields = [
        KL_FIELD.DATE_TIME, KL_FIELD.OPEN, KL_FIELD.CLOSE, KL_FIELD.HIGH, KL_FIELD.LOW,
        KL_FIELD.TRADE_VOL, KL_FIELD.TRADE_VAL, KL_FIELD.TURNOVER_RATE,
        KL_FIELD.CHANGE_RATE, KL_FIELD.LAST_CLOSE,
    ]

    print(f"通过富途API获取 {stock_code} K线: {start} ~ {end}（目标{end}前{days}个自然日）")

    ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    try:
        # ── 大跨度自动分页 ──
        all_data_parts = []
        batch_end = end
        remaining_days = fetch_calendar_days
        while remaining_days > 0:
            batch_days = min(remaining_days, FUTU_MAX_BARS)
            batch_start = batch_end - timedelta(days=batch_days)
            part = _fetch_batch(ctx, stock_code,
                                batch_start.isoformat(), batch_end.isoformat(), fields)
            if len(part) == 0:
                # 空结果直接跳过（可能该段无交易）
                pass
            elif len(all_data_parts) > 0:
                # 去重叠：本批第一条可能=上批最后一条
                last_prev = all_data_parts[-1]['time_key'].iloc[-1]
                first_this = part['time_key'].iloc[0]
                if str(last_prev)[:10] == str(first_this)[:10]:
                    part = part.iloc[1:]
                if len(part) > 0:
                    all_data_parts.append(part)
            else:
                all_data_parts.append(part)
            remaining_days -= batch_days
            batch_end = batch_start

        if not all_data_parts:
            # ── A 股回退：富途额度不足/空数据时改用 akshare 新浪源 ──
            if _is_a_share(stock_code):
                try:
                    data = _fetch_from_akshare(stock_code, days)
                except Exception as e:
                    print(f"  akshare 回退失败: {e}")
                    data = []
                if data is not None and len(data) > 0:
                    print(f"获取到 {len(data)} 条K线数据（akshare 新浪源）")
                    write_data = data.tail(min(days, len(data)))
                    _write_rows(stock_code, data, write_data)
                    print("daily_quote 回填完成（akshare 新浪源）")
                    return
            print("未获取到任何K线数据")
            sys.exit(1)

        data = pd.concat(all_data_parts, ignore_index=True)
        data = data.sort_values('time_key').reset_index(drop=True)
        print(f"获取到 {len(data)} 条K线数据")

        # 取最近 days 条写入（前面的用来算 52 周高低）
        write_data = data.tail(min(days, len(data)))
        _write_rows(stock_code, data, write_data)
        print("daily_quote 回填完成")

    finally:
        ctx.close()


def _write_rows(stock_code: str, data, write_data) -> None:
    """将 K 线 DataFrame 写入 daily_quote（富途/akshare 共用）。data 用于算 52 周高低窗口。"""
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

        # 52周最高/最低：取该行之前最近250个交易日
        high_52w = None
        low_52w = None
        # idx 是 write_data 在原 data 中的位置；取其前 250 行
        pos_in_data = data.index.get_loc(idx)
        win_start = max(0, pos_in_data - 250)
        window = data.iloc[win_start:pos_in_data]
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
    # 全部写入失败 → 退出码非 0，避免被上游 setup_new_stock.py 误判为成功
    if inserted == 0:
        sys.exit(1)


if __name__ == "__main__":
    stock = sys.argv[1] if len(sys.argv) > 1 else "SH.600900"
    n_days = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    backfill(stock, n_days)
