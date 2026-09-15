#!/usr/bin/env python3
"""
daily_quote 合并迁移 · 阶段 0（幂等，可重复执行）

目的
    a_daily_quote ∪ hk_daily_quote → 统一日线视图 v_daily_quote，
    作为 daily_quote 的统一替代「读接口」，支撑渐进式改造。

本阶段只做两件事（只增不改，零风险，可随时重跑）：
    1) 创建/刷新视图 v_daily_quote（DDL 从 sql/schema.sql 的 3.1.1 段读取，避免两处漂移）
       —— 含兼容期双命名别名：close↔last_price、amount↔turnover、
          open↔open_price、high↔high_price、low↔low_price；新增 market 列（'A'/'HK'）
    2) 回灌缺口：daily_quote 中「池表不存在」的 (stock_code, trade_date) 行 → 对应池表
       （不回灌则读方切到视图后读不到 daily_quote 独有的历史行）

明确不做（属后续阶段）：
    · 阶段 1 写方双写（get_quote.py / ticker_collector 同时写池表）
    · 阶段 2 读方逐个切到 v_daily_quote
    · 阶段 3/4 停写 daily_quote、改名 legacy、drop

回灌时自动发生的差异（符合池表口径，属预期）：
    · change_pct 精度：池表为 NUMERIC(12,4)（真实库口径），daily_quote 为 NUMERIC(8,4)
    · ps_ttm_ratio / pcf_ttm_ratio 两列已于 2026-09 从池表下线（派生值不落事实表），
      现由画像层 profiling.quantile.load_revenue_ttm + 总市值现算，无需回灌

用法
    python3 migrate_v_daily_quote.py            # 执行（幂等，可反复跑）
    python3 migrate_v_daily_quote.py --dry-run  # 只统计现状与缺口，不建视图、不写库
"""
import os
import sys
import logging

from db import get_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("migrate_v_daily_quote")


# ── 缺口回灌（按市场分别执行；列名映射：daily_quote 旧命名 → 池表命名）────────
# 说明：本 SQL 不含 % 字面量（用 LEFT 判前缀替代 LIKE），无 psycopg2 转义问题。
_GAP_SQL = """
INSERT INTO {pool} (
    stock_code, trade_date, update_time,
    open, high, low, close,
    prev_close, change_pct, volume, amount,
    turnover_rate, volume_ratio, high_52w, low_52w,
    total_market_val, circular_market_val,
    pe_ratio, pe_ttm_ratio, pb_ratio, dividend_ratio_ttm,
    created_at
)
SELECT
    d.stock_code, d.trade_date, d.update_time,
    d.open_price, d.high_price, d.low_price, d.last_price,
    d.prev_close, d.change_pct, d.volume, d.turnover,
    d.turnover_rate, d.volume_ratio, d.high_52w, d.low_52w,
    d.total_market_val, d.circular_market_val,
    d.pe_ratio, d.pe_ttm_ratio, d.pb_ratio, d.dividend_ratio_ttm,
    d.created_at
FROM daily_quote d
WHERE {market_filter}
  AND NOT EXISTS (
      SELECT 1 FROM {pool} p
      WHERE p.stock_code = d.stock_code
        AND p.trade_date = d.trade_date
  )
ON CONFLICT (stock_code, trade_date) DO NOTHING
"""

# (池表, market 过滤条件)：A 池仅收 SH./SZ. 前缀，HK 池仅收 HK. 前缀
_TARGETS = (
    ("a_daily_quote", "LEFT(d.stock_code, 2) IN ('SH', 'SZ')"),
    ("hk_daily_quote", "LEFT(d.stock_code, 2) = 'HK'"),
)

# 缺口统计：daily_quote 的 (stock, date) 在对应池表不存在
_GAP_STATS_SQL = """
SELECT d.stock_code, COUNT(*) AS miss, MIN(d.trade_date), MAX(d.trade_date)
FROM daily_quote d
LEFT JOIN hk_daily_quote h ON h.stock_code = d.stock_code AND h.trade_date = d.trade_date
LEFT JOIN a_daily_quote  a ON a.stock_code = d.stock_code AND a.trade_date = d.trade_date
WHERE (LEFT(d.stock_code, 2) = 'HK' AND h.stock_code IS NULL)
   OR (LEFT(d.stock_code, 2) IN ('SH', 'SZ') AND a.stock_code IS NULL)
GROUP BY d.stock_code
ORDER BY miss DESC
"""


def _load_view_ddl():
    """从 sql/schema.sql 提取 v_daily_quote 视图 DDL（单一真相源，避免两处漂移）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sql", "schema.sql")
    txt = open(path, encoding="utf-8").read()
    start = txt.find("CREATE OR REPLACE VIEW v_daily_quote AS")
    if start < 0:
        raise RuntimeError(f"{path} 未找到 v_daily_quote 定义（应有 3.1.1 段）")
    term = "FROM hk_daily_quote;"
    end = txt.find(term, start)
    if end < 0:
        raise RuntimeError(f"{path} 中 v_daily_quote 定义未正常结束（缺 {term}）")
    return txt[start:end + len(term)]


def run(dry=False):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM daily_quote")
            n_dq = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM a_daily_quote")
            n_a = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM hk_daily_quote")
            n_h = cur.fetchone()[0]

            cur.execute("SELECT 1 FROM information_schema.views WHERE table_name = 'v_daily_quote'")
            view_exists = cur.fetchone() is not None
            n_v = None
            if view_exists:
                cur.execute("SELECT COUNT(*) FROM v_daily_quote")
                n_v = cur.fetchone()[0]

            cur.execute(_GAP_STATS_SQL)
            gaps = cur.fetchall()
            miss = sum(g[1] for g in gaps)

    log.info("── 现状 ──────────────────────────────")
    log.info(f"daily_quote    : {n_dq:,} 行")
    log.info(f"a_daily_quote  : {n_a:,} 行")
    log.info(f"hk_daily_quote : {n_h:,} 行")
    log.info(f"v_daily_quote  : {'已存在 ' + format(n_v, ',') + ' 行' if n_v is not None else '不存在'}")
    log.info(f"池表缺口       : {miss:,} 行（{len(gaps)} 只）")
    for c, n, f, t in gaps:
        log.info(f"   {c:12s} {n:>5,} 行  {f} ~ {t}")

    if dry:
        log.info("dry-run：不建视图、不写库")
        return

    ddl = _load_view_ddl()
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 1) 建/刷新视图（DDL 从 schema.sql 读取）
            cur.execute(ddl)
            log.info("视图 v_daily_quote 已创建/刷新")

            # 2) 回灌缺口
            total = 0
            for pool, filt in _TARGETS:
                cur.execute(_GAP_SQL.format(pool=pool, market_filter=filt))
                n = cur.rowcount
                total += n
                log.info(f"回灌 {pool}: {n} 行")
            conn.commit()

            # 3) 复验
            cur.execute("SELECT COUNT(*) FROM v_daily_quote")
            n_v2 = cur.fetchone()[0]
            cur.execute(_GAP_STATS_SQL)
            miss2 = sum(g[1] for g in cur.fetchall())

    log.info("── 结果 ──────────────────────────────")
    log.info(f"回灌合计       : {total:,} 行")
    log.info(f"视图 v_daily_quote: {n_v2:,} 行（= a {n_a:,} + hk {n_h:,}" +
             (f" + 回灌 {total:,}" if total else "") + "）")
    log.info(f"剩余缺口       : {miss2} 行")
    if miss2:
        log.warning("仍有缺口：可能含非 SH/SZ/HK 前缀的代码，请人工检查")


if __name__ == "__main__":
    run(dry="--dry-run" in sys.argv)
