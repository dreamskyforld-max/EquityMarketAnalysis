#!/usr/bin/env python3
"""补 stock_info：缺失股票 + 上市日期 / 交易所类型 / 退市标记

背景
----
① 证券属性域需要上市日期（上市年限档标签）与交易所类型（板块标签），
而 stock_info 原本只有 stock_code / stock_name / market / symbol / currency / is_active。
同时 stock_info（8152 只）少于日线表实际覆盖（A股 5700 + 港股 2807 ≈ 8500+），
差的那批是已退市或从未登记过的股票——缺失它们会造成幸存者偏差。

数据源
------
富途 get_stock_basicinfo(Market, SecurityType.STOCK)：一次拉全市场，
返回 code / name / lot_size / listing_date / exchange_type / delisting / stock_child_type 等 17 个字段。
实测：SH 2380、SZ 2967、HK 3779（含已退市）。

注意
----
- 富途对很老的公司返回 1970-01-01 占位值（如 HK.00002 中电控股），视为缺失写 NULL。
- 新增的股票一律 is_active = FALSE。**刻意保守**：现有采集池由 config.conf 定义，
  补进来的历史/退市股票不应自动进入任何采集任务，避免改变既有采集行为。

用法：
    python3 -m profiling.run backfill-basic
"""
from __future__ import annotations

import logging
from typing import Any

from psycopg2 import extras

log = logging.getLogger(__name__)

# 富途占位值：listing_date 为 1970-01-01 表示真实上市日期不可得
_PLACEHOLDER_DATES = {"1970-01-01", "1970-01-01 00:00:00", ""}

_NEW_COLUMNS = [
    ("list_date", "DATE", "上市日期（富途 listing_date；1970-01-01 占位值记为 NULL）"),
    ("exchange_type", "VARCHAR(20)", "交易所/板块类型（富途：CN_SH / CN_SZ / HK_MAINBOARD / HK_GEM）"),
    ("delisting", "BOOLEAN", "是否已退市（富途 delisting）"),
]

_CURRENCY = {"SH": "元", "SZ": "元", "HK": "港元"}

_MARKETS = [("HK", "HK"), ("SH", "SH"), ("SZ", "SZ")]


def _ensure_columns(conn: Any) -> list[str]:
    """补列（幂等）。返回本次新增的列名。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'stock_info'"
        )
        existing = {r[0] for r in cur.fetchall()}

        added = []
        for col, typ, comment in _NEW_COLUMNS:
            if col in existing:
                continue
            cur.execute(f"ALTER TABLE stock_info ADD COLUMN {col} {typ}")
            cur.execute(f"COMMENT ON COLUMN stock_info.{col} IS '{comment}'")
            added.append(col)
        if added:
            log.info("stock_info 新增列: %s", added)
        return added


def _fetch_futu() -> list[dict[str, Any]]:
    """从富途拉三个市场的全量基本信息。"""
    from futu import Market, SecurityType
    from collector_runtime import get_shared_ctx

    # get_shared_ctx() 返回的是 _LockedCtx 动态代理（__getattr__ 转发到 futu ctx），
    # 静态无法推导其方法签名，显式标注为 Any 避免类型检查误报
    ctx: Any = get_shared_ctx()
    futu_market = {"HK": Market.HK, "SH": Market.SH, "SZ": Market.SZ}

    rows = []
    for code, _ in _MARKETS:
        ret, data = ctx.get_stock_basicinfo(futu_market[code], SecurityType.STOCK)
        if ret != 0:
            log.error("富途 get_stock_basicinfo(%s) 失败: %s", code, data)
            continue
        keep = data[data["stock_type"] == "STOCK"] if "stock_type" in data.columns else data
        # 逐列取值再 zip：比 itertuples 快，且避免中文列名的属性访问
        for c, nm, ld, et, dl in zip(
            list(keep["code"]),
            list(keep["name"]),
            list(keep["listing_date"]),
            list(keep["exchange_type"]),
            list(keep["delisting"]),
        ):
            rows.append(
                {
                    "stock_code": c,
                    "stock_name": str(nm or "").strip(),
                    "market": code,
                    "symbol": c.split(".", 1)[-1],
                    "list_date": None if str(ld) in _PLACEHOLDER_DATES else ld,
                    "exchange_type": str(et or "").strip() or None,
                    "delisting": bool(dl),
                }
            )
        log.info("富途 %s 市场返回 %d 只（STOCK %d 只）", code, len(data), len(keep))
    return rows


def _fetch_bj_akshare() -> list[dict[str, Any]]:
    """北交所股票（富途 Market 无 BJ，走 AKShare）。

    AKShare stock_info_bj_name_code 返回 340 只，含证券简称与上市日期。
    注意：北交所股票在本库中的代码前缀是 SH.（如 SH.920000），与 AKShare 的纯数字对齐。
    """
    try:
        import akshare as ak

        df = ak.stock_info_bj_name_code()
    except Exception as e:
        log.warning("AKShare 北交所列表获取失败（跳过）：%s: %s", type(e).__name__, e)
        return []

    rows: list[dict[str, Any]] = []
    for code, name, list_date in zip(
        df["证券代码"].tolist(), df["证券简称"].tolist(), df["上市日期"].tolist()
    ):
        rows.append(
            {
                "stock_code": f"SH.{code}",
                "stock_name": str(name).strip(),
                "market": "SH",
                "symbol": str(code),
                "list_date": list_date,
                "exchange_type": "CN_BJ",
                "delisting": False,
            }
        )
    log.info("AKShare 北交所返回 %d 只", len(rows))
    return rows


def _fill_from_quotes(conn) -> int:
    """兜底：把有行情/财报数据但未登记在 stock_info 的股票补进去。

    这类股票主要是富途已不返回的**已退市股**（约 17 只）。只补代码与市场，
    **不猜上市日期**（行情表最早日不代表上市日，猜了会污染上市年限档标签），
    名称留空待后续数据源补。is_active=FALSE，不进入任何采集池。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO stock_info (stock_code, market, symbol, currency, is_active)
            SELECT DISTINCT q.stock_code,
                   split_part(q.stock_code, '.', 1),
                   split_part(q.stock_code, '.', 2),
                   CASE WHEN split_part(q.stock_code, '.', 1) = 'HK' THEN '港元' ELSE '元' END,
                   FALSE
            FROM (
                SELECT DISTINCT stock_code FROM a_daily_quote
                UNION SELECT DISTINCT stock_code FROM hk_daily_quote
                UNION SELECT DISTINCT stock_code FROM financial_indicator
            ) q
            WHERE q.stock_code NOT IN (SELECT stock_code FROM stock_info)
            ON CONFLICT (stock_code) DO NOTHING
            """
        )
        return cur.rowcount


def run(conn: Any = None) -> dict[str, Any]:
    """执行补采。返回统计字典。"""
    rows = _fetch_futu() + _fetch_bj_akshare()
    if not rows:
        return {"fetched": 0, "added": 0, "updated": 0, "message": "未取到任何数据，检查 OpenD 是否运行"}

    close_conn = conn is None
    if conn is None:
        from db import get_conn

        conn_cm = get_conn()
        conn = conn_cm.__enter__()
    else:
        conn_cm = None

    try:
        added_cols = _ensure_columns(conn)

        with conn.cursor() as cur:
            cur.execute("SELECT stock_code FROM stock_info")
            existing = {r[0] for r in cur.fetchall()}

        payload = [
            (
                r["stock_code"], r["stock_name"], r["market"], r["symbol"],
                _CURRENCY.get(r["market"], "元"), r["list_date"], r["exchange_type"],
                r["delisting"],
            )
            for r in rows
        ]

        with conn.cursor() as cur:
            extras.execute_values(
                cur,
                """
                INSERT INTO stock_info
                    (stock_code, stock_name, market, symbol, currency,
                     list_date, exchange_type, delisting, is_active)
                VALUES %s
                ON CONFLICT (stock_code) DO UPDATE SET
                    list_date     = EXCLUDED.list_date,
                    exchange_type = EXCLUDED.exchange_type,
                    delisting     = EXCLUDED.delisting,
                    stock_name    = CASE WHEN EXCLUDED.stock_name <> ''
                                         THEN EXCLUDED.stock_name ELSE stock_info.stock_name END,
                    symbol        = CASE WHEN COALESCE(stock_info.symbol, '') = ''
                                         THEN EXCLUDED.symbol ELSE stock_info.symbol END,
                    updated_at    = NOW()
                """,
                [(*p, False) for p in payload],
            )

        n_new = len({r["stock_code"] for r in rows} - existing)
        n_backfill = _fill_from_quotes(conn)
        stats = {
            "fetched": len(rows),
            "added": n_new,
            "updated": len(rows) - n_new,
            "backfilled_from_quotes": n_backfill,
            "new_columns": added_cols,
            "message": "新增股票 is_active=FALSE（不进入现有采集池），如需纳入请手动开启",
        }
        log.info("stock_info 补齐完成: %s", stats)
        return stats
    finally:
        if conn_cm is not None:
            conn_cm.__exit__(None, None, None)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    print(run())
