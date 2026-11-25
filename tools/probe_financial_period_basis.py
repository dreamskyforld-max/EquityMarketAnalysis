#!/usr/bin/env python3
"""财务报告期口径重构 · P0 只读探查（doc/financial_period_basis_refactor.md §8）。

用途：为 P1 数据层改造与 P3 切点标定提供实测依据，全部输出打印到 stdout，
**不写库、不建表、不改任何文件**。覆盖 §8 P0 要求的六项：

  1. 分市场 / basis 的覆盖率与降级原因分布（D8）
  2. 单季同比 vs 累计同比差异分布（D3 / D10）
  3. 陈旧门禁阈值分布（D8 的 15 个月候选值）
  4. 分组回退比例（D7，按正确的 load_industry_map(level=1) 口径）
  5. `roe_floor_pct` 按 report_type 分设的候选阈值分布（D4 / D10 / §11 裁定 1）
  6. 单季现金转化率分布与双口径对照（D5，腾讯 Q3 对账）

资源约定（AGENTS.md 计算任务资源约定）：
  - 只读，所有查询带 report_date 下界 / 报告期类型过滤（R7）；
  - financial_indicator 约 36.5 万行，单次结果 <50 万行（R1）；
  - 结束打印峰值 RSS（R3）。

用法：APP_DIR=$(pwd) .venv/bin/python3 tools/probe_financial_period_basis.py
"""
from __future__ import annotations

import platform
import resource
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_conn

AS_OF = date.today()
STALE_MONTHS_CANDIDATE = 15
MIN_GROUP = 20
ABS_FLOOR = 5  # 现有 RULES 冻结值，用于对照


def _hdr(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def _q(cur, sql: str, params=None):
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def basis_of(rt: str) -> str:
    return "quarterly" if rt in ("Q1", "Q3") else ("semi_annual" if rt == "H1" else "annual")


def main() -> None:
    with get_conn() as conn, conn.cursor() as cur:
        # ---------- 1. 最近一期分布 + 降级原因 ----------
        _hdr("1. 每票最近一期报告期类型 / basis 分布与降级原因")
        rows = _q(cur, """
            WITH r AS (
              SELECT DISTINCT ON (stock_code) stock_code, report_type, report_date,
                     revenue, net_profit
              FROM financial_indicator
              WHERE report_type IN ('Q1','H1','Q3','annual')
                AND report_date <= %s
              ORDER BY stock_code, report_date DESC
            )
            SELECT CASE WHEN stock_code LIKE 'HK.%%' THEN 'HK' ELSE 'A' END mkt,
                   report_type, count(*) n
            FROM r GROUP BY 1,2 ORDER BY 1,2
        """, (AS_OF,))
        for r in rows:
            print(f"  {r['mkt']:<3} {r['report_type']:<8} {r['n']:>6}  basis={basis_of(r['report_type'])}")

        # ---------- 2. 单季化可行性 + 去年同季可比 + 陈旧天数 ----------
        _hdr("2. 单季化可行性 / 去年同季可比率 / 数据陈旧天数（D8 降级判定）")
        rows = _q(cur, """
            WITH ord AS (
              SELECT stock_code, report_date, report_type, revenue::float8 rev,
                     LAG(report_type) OVER w prev_t, LAG(revenue) OVER w prev_rev,
                     ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY report_date DESC) rn
              FROM financial_indicator
              WHERE report_type IN ('Q1','H1','Q3','annual')
                AND report_date <= %s
              WINDOW w AS (PARTITION BY stock_code,
                           COALESCE(fiscal_year_start, DATE_TRUNC('year',report_date)::date)
                           ORDER BY report_date)
            ), latest AS (
              SELECT * FROM ord WHERE rn = 1
            ), single AS (
              SELECT stock_code, report_date, report_type,
                     CASE WHEN report_type = 'Q1' THEN rev
                          WHEN report_type = 'H1' AND prev_t = 'Q1' AND prev_rev IS NOT NULL
                            THEN rev - prev_rev
                          WHEN report_type = 'Q3' AND prev_t = 'H1' AND prev_rev IS NOT NULL
                            THEN rev - prev_rev
                          WHEN report_type = 'annual' AND prev_t = 'Q3' AND prev_rev IS NOT NULL
                            THEN rev - prev_rev
                     END AS rev_q
              FROM latest
            ), py AS (
              SELECT s.stock_code, p.revenue::float8 AS py_rev_q, s.report_date
              FROM single s LEFT JOIN v_financial_quarterly p
                ON p.stock_code = s.stock_code
               AND p.report_type = s.report_type
               AND p.report_date = (s.report_date - INTERVAL '1 year')::date
            )
            SELECT CASE WHEN s.stock_code LIKE 'HK.%%' THEN 'HK' ELSE 'A' END mkt,
                   count(*) n,
                   count(s.rev_q) FILTER (WHERE s.rev_q IS NOT NULL) splittable,
                   count(p.py_rev_q) FILTER (WHERE s.rev_q IS NOT NULL AND p.py_rev_q IS NOT NULL) has_prior_year,
                   count(*) FILTER (WHERE s.rev_q IS NULL AND p.py_rev_q IS NOT NULL) false_positive,
                   percentile_cont(0.5) WITHIN GROUP (
                     ORDER BY (%s::date - s.report_date)) med_age_days,
                   percentile_cont(0.9) WITHIN GROUP (
                     ORDER BY (%s::date - s.report_date)) p90_age_days,
                   count(*) FILTER (WHERE (%s::date - s.report_date) > %s) stale_gt15m
            FROM single s LEFT JOIN py p ON p.stock_code = s.stock_code
            GROUP BY 1 ORDER BY 1
        """, (AS_OF, AS_OF, AS_OF, AS_OF, STALE_MONTHS_CANDIDATE * 30))
        for r in rows:
            print(f"  {r['mkt']:<3} n={r['n']:>5}  可拆单季={r['splittable']:>5} "
                  f"且去年同季可比={r['has_prior_year']:>5}  报告期中位龄={r['med_age_days']:.0f}天 "
                  f"P90={r['p90_age_days']:.0f}天  超{STALE_MONTHS_CANDIDATE}月={r['stale_gt15m']:>4}")
        print(f"  注：陈旧阈值候选 {STALE_MONTHS_CANDIDATE} 月（按 30 天/月近似）；见 §8 P0 闸门。")

        # ---------- 3. 单季同比 vs 累计同比 ----------
        _hdr("3. 单季同比 vs 累计同比差异分布（D3 / D10 切点重标定依据）")
        rows = _q(cur, """
            WITH s AS (
              SELECT stock_code, report_date, report_type, revenue::float8 rev
              FROM v_financial_quarterly
              WHERE report_type IN ('Q1','Q2','Q3','Q4')
                AND report_date >= %s
            ), cur AS (
              SELECT DISTINCT ON (stock_code) * FROM s
              ORDER BY stock_code, report_date DESC
            ), j AS (
              SELECT c.stock_code, c.report_type, c.report_date,
                     c.rev cur_q, p.rev prev_q, f.revenue_yoy::float8 yoy_cum,
                     (c.rev / NULLIF(p.rev, 0) - 1) * 100 yoy_q
              FROM cur c
              LEFT JOIN s p ON p.stock_code = c.stock_code
                           AND p.report_type = c.report_type
                           AND p.report_date = (c.report_date - INTERVAL '1 year')::date
              LEFT JOIN financial_indicator f ON f.stock_code = c.stock_code
                           AND f.report_date = c.report_date
              WHERE p.rev > 0
            )
            SELECT CASE WHEN stock_code LIKE 'HK.%%' THEN 'HK' ELSE 'A' END mkt,
                   count(*) n,
                   round(avg(abs(yoy_q - yoy_cum))::numeric, 2) mean_abs,
                   round((percentile_cont(0.5) WITHIN GROUP (
                     ORDER BY abs(yoy_q - yoy_cum)))::numeric, 2) med_abs,
                   round((percentile_cont(0.9) WITHIN GROUP (
                     ORDER BY abs(yoy_q - yoy_cum)))::numeric, 2) p90_abs,
                   count(*) FILTER (WHERE abs(yoy_q - yoy_cum) > 5) gt5,
                   count(*) FILTER (WHERE yoy_q > 200 OR yoy_q < -90) extreme
            FROM j GROUP BY 1 ORDER BY 1
        """, (date(AS_OF.year - 3, 1, 1),))
        for r in rows:
            print(f"  {r['mkt']:<3} n={r['n']:>5} 平均绝对差={r['mean_abs']}% "
                  f"中位={r['med_abs']}% P90={r['p90_abs']}%  >5pp={r['gt5']} "
                  f"极端值={r['extreme']}")

        # ---------- 4. 分组回退比例（正确一级行业口径） ----------
        _hdr(f"4. 分组回退比例（一级行业，最小样本 {MIN_GROUP}）")
        rows = _q(cur, """
            WITH r AS (
              SELECT DISTINCT ON (stock_code) stock_code, report_type
              FROM financial_indicator WHERE report_type IN ('Q1','H1','Q3','annual')
                AND report_date <= %s
              ORDER BY stock_code, report_date DESC
            ), b AS (
              SELECT stock_code,
                     CASE WHEN report_type IN ('Q1','Q3') THEN 'quarterly'
                          WHEN report_type = 'H1' THEN 'semi_annual' ELSE 'annual' END basis,
                     CASE WHEN stock_code LIKE 'HK.%%' THEN 'HK' ELSE 'A' END mkt
              FROM r
            ), ind AS (
              SELECT m.stock_code, 'A' mkt, l1.sw_code code
              FROM regime.stock_sw_industry_mapping m
              JOIN regime.sw_industry_classification l3c ON l3c.sw_code = m.sw_code_l3
              JOIN regime.sw_industry_classification l2 ON l2.sw_code = l3c.parent_code
              JOIN regime.sw_industry_classification l1 ON l1.sw_code = l2.parent_code
              WHERE l1.level = 1
              UNION ALL
              SELECT m.stock_code, 'HK' mkt, c.industry_code
              FROM regime.stock_hsics_industry_mapping m
              JOIN regime.hsics_industry_classification c ON c.subsector_code = m.hsics_code_l3
              WHERE c.industry_code IS NOT NULL
            ), g AS (
              SELECT b.stock_code, b.basis, b.mkt, COALESCE(i.code,'NA') ind1
              FROM b LEFT JOIN ind i ON i.stock_code = b.stock_code AND i.mkt = b.mkt
            ), cnt AS (
              SELECT mkt, basis, ind1, count(*) n
              FROM g GROUP BY 1,2,3
            )
            SELECT mkt, basis, count(*) grp_n,
                   sum(n) FILTER (WHERE n >= %s) industry_level,
                   sum(n) FILTER (WHERE n >= 5 AND n < %s) market_fallback,
                   sum(n) FILTER (WHERE n < 5) no_publish
            FROM cnt GROUP BY 1,2 ORDER BY 1,2
        """, (AS_OF, MIN_GROUP, MIN_GROUP))
        for r in rows:
            print(f"  {r['mkt']:<3} {r['basis']:<13} 行业组={r['grp_n']:>3} "
                  f"行业级={r['industry_level'] or 0:>5} 回退市场级={r['market_fallback'] or 0:>5} "
                  f"不发布={r['no_publish'] or 0:>4}")

        # ---------- 5. roe_floor_pct 按 report_type 分设候选 ----------
        _hdr("5. ROE 原值按 report_type 分布（roe_floor_pct 分设候选，§11 裁定 1）")
        rows = _q(cur, """
            WITH r AS (
              SELECT DISTINCT ON (stock_code, report_type) stock_code, report_type, roe::float8 roe
              FROM financial_indicator
              WHERE report_type IN ('Q1','H1','Q3','annual')
                AND report_date <= %s AND roe IS NOT NULL AND roe > 0
              ORDER BY stock_code, report_type, report_date DESC
            )
            SELECT report_type, count(*) n,
                   round((percentile_cont(0.25) WITHIN GROUP (ORDER BY roe))::numeric, 3) p25,
                   round((percentile_cont(0.30) WITHIN GROUP (ORDER BY roe))::numeric, 3) p30,
                   round((percentile_cont(0.35) WITHIN GROUP (ORDER BY roe))::numeric, 3) p35,
                   round((percentile_cont(0.5) WITHIN GROUP (ORDER BY roe))::numeric, 3) p50,
                   count(*) FILTER (WHERE roe >= %s) ge_current_floor
            FROM r GROUP BY 1 ORDER BY 1
        """, (AS_OF, ABS_FLOOR))
        print(f"  {'report_type':<10}{'n':>7}{'P25':>9}{'P30':>9}{'P35':>9}{'P50':>9}{'现行>=5%':>10}")
        for r in rows:
            print(f"  {r['report_type']:<10}{r['n']:>7}{r['p25']:>9}{r['p30']:>9}"
                  f"{r['p35']:>9}{r['p50']:>9}{r['ge_current_floor']:>10}")
        print("  注：P25–P35 为 §11 裁定的候选区间；冻结前仍需熵比标定确认无死档。")

        # ---------- 6. 单季现金转化率分布 + 腾讯对账 ----------
        _hdr("6. 单季现金转化率分布与双口径对照（D5）")
        rows = _q(cur, """
            WITH q AS (
              SELECT stock_code, report_date, report_type,
                     operating_cash_flow::float8 ocf, net_profit::float8 np
              FROM v_financial_quarterly
              WHERE report_type IN ('Q1','Q2','Q3','Q4')
                AND report_date >= %s AND net_profit > 0
            ), c AS (
              SELECT stock_code, report_date, report_type,
                     operating_cash_flow::float8 ocf, net_profit::float8 np
              FROM financial_indicator
              WHERE report_type IN ('Q1','H1','Q3','annual')
                AND report_date >= %s AND net_profit > 0
            ), m AS (
              SELECT '单季' src, count(*) n,
                     round((percentile_cont(0.1) WITHIN GROUP (
                       ORDER BY ocf/np))::numeric, 3) p10,
                     round((percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY ocf/np))::numeric, 3) p50,
                     round((percentile_cont(0.9) WITHIN GROUP (
                       ORDER BY ocf/np))::numeric, 3) p90,
                     count(*) FILTER (WHERE ocf < 0) neg
              FROM q UNION ALL
              SELECT '累计', count(*),
                     round((percentile_cont(0.1) WITHIN GROUP (ORDER BY ocf/np))::numeric,3),
                     round((percentile_cont(0.5) WITHIN GROUP (ORDER BY ocf/np))::numeric,3),
                     round((percentile_cont(0.9) WITHIN GROUP (ORDER BY ocf/np))::numeric,3),
                     count(*) FILTER (WHERE ocf < 0) FROM c
            )
            SELECT * FROM m
        """, (date(AS_OF.year - 2, 1, 1), date(AS_OF.year - 2, 1, 1)))
        print(f"  {'口径':<6}{'n':>7}{'P10':>9}{'P50':>9}{'P90':>9}{'OCF<0':>8}")
        for r in rows:
            print(f"  {r['src']:<6}{r['n']:>7}{r['p10']:>9}{r['p50']:>9}{r['p90']:>9}{r['neg']:>8}")

        rows = _q(cur, """
            SELECT '单季' src, report_date::text, report_type,
                   round((operating_cash_flow::float8 / NULLIF(net_profit::float8,0) * 100)::numeric,2) pct
            FROM v_financial_quarterly
            WHERE stock_code='HK.00700' AND report_date >= %s
            UNION ALL
            SELECT '累计', report_date::text, report_type,
                   round((operating_cash_flow::float8 / NULLIF(net_profit::float8,0) * 100)::numeric,2)
            FROM financial_indicator
            WHERE stock_code='HK.00700' AND report_date >= %s
            ORDER BY report_date DESC, src
        """, (date(AS_OF.year - 1, 1, 1), date(AS_OF.year - 1, 1, 1)))
        print("  腾讯 HK.00700 净现比（%）：")
        for r in rows:
            print(f"    {r['src']:<4} {r['report_date']} {r['report_type']:<6} {r['pct']}")

    print(f"\n[{platform.system()}] 峰值 RSS: "
          f"{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024**2 if platform.system()=='Darwin' else 1024):.1f} MB")
    print("P0 探查完成（全程只读，未写库）。")


if __name__ == "__main__":
    main()
