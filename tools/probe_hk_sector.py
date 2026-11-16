#!/usr/bin/env python3
"""第④层「行业配置」阶段 3（HK 行业层）数据闸门探测（**只读**：不写库、不建表、不改配置）。

对应 doc/industry_allocation_feasibility.md §2.2 D4 / §4.2 阶段 3。
在动代码前用本机真实库判定：

  ① HK 行业分类（HS_L3）的行业数 / 成分数 / 粒度，与 SECTOR.UP_RATIO 的 113 行业是否一致；
  ② hk_daily_quote 起点 / 有效日数 / 市值·估值·换手列覆盖度（决定 M8 加权列与指数起点）；
  ③ daily_ggt_hold 南向与行业成分的匹配率、est_net_inflow 覆盖；
  ④ sector_hierarchy 对 HK.LIST* 的覆盖（as_of 缺失现状）；
  ⑤ financial_indicator 是否覆盖 HK（决定 E 组能否做）；
  ⑥ 现有 sector_indicator_dict / sector_constituent 的双市场冲突面。

用法（项目根目录执行）：
    .venv/bin/python3 tools/probe_hk_sector.py

⚠ 所有 SQL 一律 cur.execute(sql) **不传 params**，故 LIKE 'HK.%' 里的 % 保持单写；
   若日后改成参数化执行，字面 % 必须双写成 %%（psycopg2 格式符坑）。
⚠ 结论不硬编码在脚本里：脚本只输出事实，判定与决策点写回设计文档。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from db import get_conn  # noqa: E402

# (标签, SQL)。全部为聚合 / 只读；大表查询带时间下界或实体过滤（R1/R7）。
QUERIES = [
    ("① stock_sector 港股 INDUSTRY 概览",
     "SELECT count(*) AS rows, count(DISTINCT stock_code) AS stocks, "
     "count(DISTINCT sector_code) AS sector_codes, "
     "count(DISTINCT sector_name) AS sector_names, "
     "count(*) FILTER (WHERE weight IS NOT NULL) AS weighted_rows, "
     "count(*) FILTER (WHERE source IS NOT NULL) AS sourced_rows "
     "FROM stock_sector WHERE sector_type = 'INDUSTRY' AND stock_code LIKE 'HK.%'"),

    ("① sector_code 前缀分布（确认是否只有 HK.LIST*）",
     "SELECT left(sector_code, 8) AS prefix, count(*) AS rows, "
     "count(DISTINCT sector_code) AS codes "
     "FROM stock_sector WHERE sector_type = 'INDUSTRY' AND stock_code LIKE 'HK.%' "
     "GROUP BY 1 ORDER BY 2 DESC LIMIT 15"),

    ("① 每行业成分数分布（含 ≥5 / ≥10 只的行业数）",
     "SELECT min(n) AS min_n, round(avg(n), 1) AS avg_n, max(n) AS max_n, "
     "count(*) AS sectors, count(*) FILTER (WHERE n >= 5) AS ge5, "
     "count(*) FILTER (WHERE n >= 10) AS ge10 "
     "FROM (SELECT sector_code, count(*) AS n FROM stock_sector "
     "      WHERE sector_type = 'INDUSTRY' AND stock_code LIKE 'HK.%' "
     "      GROUP BY 1) t"),

    ("② hk_daily_quote 概览",
     "SELECT min(trade_date) AS min_d, max(trade_date) AS max_d, "
     "count(DISTINCT trade_date) AS days, count(DISTINCT stock_code) AS stocks, "
     "count(*) AS rows FROM hk_daily_quote"),

    ("② hk_daily_quote 按年列覆盖率（%）—— 决定 M8 加权列与指数起点",
     "SELECT date_trunc('year', trade_date)::date AS yr, count(*) AS rows, "
     "round(100.0 * count(close) / count(*), 1) AS close_pct, "
     "round(100.0 * count(change_pct) / count(*), 1) AS chg_pct, "
     "round(100.0 * count(circular_market_val) / count(*), 1) AS cmv_pct, "
     "round(100.0 * count(total_market_val) / count(*), 1) AS tmv_pct, "
     "round(100.0 * count(pe_ttm_ratio) / count(*), 1) AS pe_pct, "
     "round(100.0 * count(pb_ratio) / count(*), 1) AS pb_pct, "
     "round(100.0 * count(turnover_rate) / count(*), 1) AS turn_pct, "
     "round(100.0 * count(amount) / count(*), 1) AS amt_pct "
     "FROM hk_daily_quote WHERE trade_date >= '2000-01-01' "
     "GROUP BY 1 ORDER BY 1"),

    ("② 最近交易日：行业成分命中 / 流动性门槛后的有效行业数",
     "WITH d AS (SELECT max(trade_date) AS d FROM hk_daily_quote), "
     "s AS (SELECT v.trade_date, ss.sector_code, v.stock_code, v.amount, "
     "             v.circular_market_val, v.pe_ttm_ratio "
     "      FROM hk_daily_quote v "
     "      JOIN stock_sector ss ON ss.stock_code = v.stock_code "
     "        AND ss.sector_type = 'INDUSTRY' AND ss.stock_code LIKE 'HK.%' "
     "      WHERE v.trade_date = (SELECT d FROM d)) "
     "SELECT (SELECT d FROM d) AS trade_date, count(*) AS rows, "
     "count(DISTINCT sector_code) AS sectors_all, "
     "count(DISTINCT sector_code) FILTER (WHERE amount >= 100000) AS sectors_liquid, "
     "count(DISTINCT sector_code) FILTER "
     "  (WHERE amount >= 100000 AND circular_market_val IS NOT NULL) AS sectors_cmv, "
     "count(DISTINCT sector_code) FILTER "
     "  (WHERE amount >= 100000 AND pe_ttm_ratio IS NOT NULL) AS sectors_pe, "
     "round(100.0 * count(*) FILTER (WHERE circular_market_val IS NOT NULL) "
     "       / nullif(count(*), 0), 1) AS cmv_row_pct, "
     "round(100.0 * count(*) FILTER (WHERE pe_ttm_ratio IS NOT NULL) "
     "       / nullif(count(*), 0), 1) AS pe_row_pct FROM s"),

    ("③ daily_ggt_hold 概览",
     "SELECT min(trade_date) AS min_d, max(trade_date) AS max_d, "
     "count(DISTINCT trade_date) AS days, count(DISTINCT stock_code) AS stocks, "
     "count(*) AS rows, "
     "count(*) FILTER (WHERE est_net_inflow IS NOT NULL) AS net_nonnull "
     "FROM daily_ggt_hold"),

    ("③ 南向标的与港股行业映射的匹配率",
     "WITH g AS (SELECT DISTINCT stock_code FROM daily_ggt_hold), "
     "s AS (SELECT DISTINCT stock_code FROM stock_sector "
     "      WHERE sector_type = 'INDUSTRY' AND stock_code LIKE 'HK.%') "
     "SELECT (SELECT count(*) FROM g) AS ggt_stocks, "
     "(SELECT count(*) FROM s) AS mapped_stocks, "
     "count(*) FILTER (WHERE s.stock_code IS NOT NULL) AS both_stocks "
     "FROM g LEFT JOIN s USING (stock_code)"),

    ("④ sector_hierarchy 概览（当前无 as_of）",
     "SELECT count(*) AS rows, "
     "count(*) FILTER (WHERE sector_code LIKE 'HK.%') AS hk_rows, "
     "count(DISTINCT parent_code) AS parents, "
     "count(DISTINCT parent_code) FILTER (WHERE sector_code LIKE 'HK.%') AS hk_parents, "
     "max(updated_at) AS last_update FROM sector_hierarchy"),

    ("⑤ financial_indicator 的港股覆盖（决定 E 组能否做）",
     "SELECT count(*) FILTER (WHERE stock_code LIKE 'HK.%') AS hk_rows, "
     "count(DISTINCT stock_code) FILTER (WHERE stock_code LIKE 'HK.%') AS hk_stocks, "
     "count(*) AS total_rows FROM financial_indicator"),

    ("⑥ sector_indicator_dict 现状（PK 只有 indicator_code → 双市场会冲突）",
     "SELECT count(*) AS rows, count(DISTINCT market_scope) AS markets, "
     "count(DISTINCT classification) AS classifications, "
     "min(indicator_code) AS min_code, max(indicator_code) AS max_code, "
     "count(DISTINCT params_hash) AS hashes "
     "FROM industry.indicator_dict"),

    ("⑥ sector_constituent 现状",
     "SELECT market, classification, count(*) AS rows, "
     "count(DISTINCT sector_code) AS sectors, count(DISTINCT stock_code) AS stocks, "
     "min(eff_from) AS min_eff, max(eff_from) AS max_eff, "
     "count(*) FILTER (WHERE eff_to IS NOT NULL) AS closed "
     "FROM industry.constituent GROUP BY 1, 2 ORDER BY 1, 2"),

    ("⑥ 输出表现有 market / classification / biz_code 分布",
     "SELECT 'indicator_value' AS tbl, market, classification, '' AS biz_code, count(*) AS rows "
     "FROM industry.indicator_value GROUP BY 1, 2, 3 "
     "UNION ALL "
     "SELECT 'business_value', market, classification, biz_code, count(*) "
     "FROM industry.business_value GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4"),
]


def main() -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        for label, sql in QUERIES:
            print("\n### " + label)
            try:
                cur.execute(sql)
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
                print(" | ".join(cols))
                for r in rows[:40]:
                    print(" | ".join("NULL" if v is None else str(v) for v in r))
                if len(rows) > 40:
                    print(f"... 共 {len(rows)} 行（仅显示前 40 行）")
            except Exception as exc:  # 探测失败本身是有效结论，不中断后续查询
                conn.rollback()
                print(f"ERROR: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
