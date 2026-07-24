#!/usr/bin/env python3
"""
逐笔成交 tick_data 按股数固定阈值划分：大机构 / 一般机构 / 游资 / 散户
（阈值与招商证券口径一致，按股票代码独立配置）
-------------------------------------------------------------------
阈值分类（按单笔股数）:
  特大（大机构）: ≥ V_SUPER 股
  大单（一般机构）: ≥ V_BIG 且 < V_SUPER 股
  中单（游资）:    ≥ V_MID 且 < V_BIG 股
  小单（散户）:    < V_MID 股

当前已配置股票:
  HK.00700 (腾讯 ~500HKD): V_SUPER=30000, V_BIG=9000, V_MID=900
    散户 <900股 ≈ <45万HKD（1-9手）
    游资 900~9000股 ≈ 45~450万HKD
    一般机构 9000~3万股 ≈ 450~1500万HKD
    大机构 ≥3万股 ≈ ≥1500万HKD

用法:
    python3 analyze_tick_distribution.py                          # 默认 HK.00700
    python3 analyze_tick_distribution.py HK.00001                  # 指定股票
    python3 analyze_tick_distribution.py HK.00700 2026-06-13       # 指定日期

新增股票阈值: 在 STOCK_THRESHOLDS 字典中添加即可，格式见 HK.00700
"""
import sys
from db import get_conn

# 按股票代码配置阈值（V_SUPER, V_BIG, V_MID），未配置的股票使用默认值
STOCK_THRESHOLDS = {
    'HK.00700': (5000, 1000, 300),   # 腾讯 ~500HKD
}
_DEFAULT_THRESHOLDS = (50000, 10000, 1000)  # 通用默认值，待各股票校准后替换


def get_thresholds(stock_code):
    """获取指定股票的阈值，未配置则返回默认值"""
    return STOCK_THRESHOLDS.get(stock_code, _DEFAULT_THRESHOLDS)


def analyze(cursor, stock_code, trade_date=None):
    v_super, v_big, v_mid = get_thresholds(stock_code)
    date_filter = "AND tick_time::date = %(trade_date)s" if trade_date else ""
    params = {"stock_code": stock_code, "trade_date": trade_date}

    category_sql = f"""
        CASE WHEN volume >= {v_super}                             THEN '特大(大机构)'
             WHEN volume >= {v_big}   AND volume < {v_super}      THEN '大单(一般机构)'
             WHEN volume >= {v_mid}   AND volume < {v_big}        THEN '中单(游资)'
             ELSE '小单(散户)' END"""

    # ———— 表1: 按股数划分，含统计明细 ————
    cursor.execute(f"""
        WITH categorized AS (
            SELECT volume, turnover, {category_sql} AS category
            FROM tick_data
            WHERE stock_code = %(stock_code)s
              AND turnover IS NOT NULL AND turnover > 0
              {date_filter}
        )
        SELECT category,
               COUNT(*)                                                  AS cnt,
               ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1)       AS pct,
               ROUND(SUM(turnover) / SUM(SUM(turnover)) OVER () * 100, 1) AS tov_pct,
               MIN(volume)::INT          AS v_min,
               ROUND(AVG(volume))::INT   AS v_avg,
               PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY volume)::INT AS v_med,
               MAX(volume)::INT          AS v_max,
               ROUND(MIN(turnover))::INT AS t_min,
               ROUND(AVG(turnover))::INT AS t_avg,
               PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY turnover)::INT AS t_med,
               ROUND(MAX(turnover))::INT AS t_max
        FROM categorized
        GROUP BY category
        ORDER BY CASE category
            WHEN '特大(大机构)' THEN 1 WHEN '大单(一般机构)' THEN 2
            WHEN '中单(游资)' THEN 3 ELSE 4 END
    """, params)

    print(f'\n{"=" * 110}')
    print(f'  tick_data 成交分布分析 — {stock_code}', end='')
    if trade_date:
        print(f' ({trade_date})', end='')
    print()
    print(f'{"=" * 110}')
    print(f'  一、按股数划分（{stock_code} 阈值）')
    print(f'     大机构≥{v_super}股  一般机构≥{v_big}股  游资≥{v_mid}股  散户<{v_mid}股')
    print(f'{"─" * 110}')
    header = (
        f'{"类别":<16} {"笔数":>6} {"占比":>7} {"成交额占比":>9} '
        f'{"股数min":>8} {"股数avg":>8} {"股数med":>8} {"股数max":>8} '
        f'{"成交额min":>10} {"成交额avg":>10} {"成交额med":>10} {"成交额max":>10}'
    )
    print(header)
    print('-' * len(header))
    for row in cursor.fetchall():
        print(f'{row[0]:<16} {row[1]:>6} {row[2]:>5}%  {row[3]:>7}%  '
              f'{row[4]:>8} {row[5]:>8} {row[6]:>8} {row[7]:>8} '
              f'{row[8]:>10} {row[9]:>10} {row[10]:>10} {row[11]:>10}')

    # ———— 各档 volume 分桶 ————
    cursor.execute(f"""
        WITH categorized AS (
            SELECT volume, {category_sql} AS category,
                   CASE WHEN volume <= 100    THEN '0~100'
                        WHEN volume <= 200    THEN '100~200'
                        WHEN volume <= 500    THEN '200~500'
                        WHEN volume <= 1000   THEN '500~1K'
                        WHEN volume <= 2000   THEN '1K~2K'
                        WHEN volume <= 5000   THEN '2K~5K'
                        WHEN volume <= 10000  THEN '5K~1W'
                        ELSE '1W+' END AS bucket
            FROM tick_data
            WHERE stock_code = %(stock_code)s
              AND turnover IS NOT NULL AND turnover > 0
              {date_filter}
        )
        SELECT category, bucket, COUNT(*) AS cnt
        FROM categorized
        GROUP BY category, bucket
        ORDER BY CASE category
            WHEN '特大(大机构)' THEN 1 WHEN '大单(一般机构)' THEN 2
            WHEN '中单(游资)' THEN 3 ELSE 4 END,
            CASE bucket
                WHEN '0~100' THEN 1 WHEN '100~200' THEN 2 WHEN '200~500' THEN 3
                WHEN '500~1K' THEN 4 WHEN '1K~2K' THEN 5 WHEN '2K~5K' THEN 6
                WHEN '5K~1W' THEN 7 ELSE 8 END
    """, params)

    print(f'\n{"─" * 60}')
    print('  各档股数分布（volume 分桶）')
    print(f'{"─" * 60}')
    cur_cat = None
    for row in cursor.fetchall():
        if row[0] != cur_cat:
            cur_cat = row[0]
            print(f'  [{cur_cat}]')
        bar = '█' * min(row[2], 80)
        print(f'    {row[1]:<10} {row[2]:<6} {bar}')

    # ———— volume 原始分布（GROUP BY volume，按volume排序） ————
    cursor.execute(f"""
        SELECT CASE WHEN volume < 100 THEN -1 ELSE volume END AS vol, COUNT(*)::INT AS cnt
        FROM tick_data
        WHERE stock_code = %(stock_code)s
          AND turnover IS NOT NULL AND turnover > 0
          {date_filter}
        GROUP BY CASE WHEN volume < 100 THEN -1 ELSE volume END
        ORDER BY vol
    """, params)

    print(f'\n{"─" * 40}')
    print(f'  三、volume 原始分布（按股数排序，<100股归为碎股）')
    print(f'{"─" * 40}')
    print(f'{"volume":>8}  {"笔数":>6}')
    print('-' * 20)
    for volume, cnt in cursor.fetchall():
        label = '碎股' if volume == -1 else str(volume)
        print(f'{label:>8}  {cnt:>6}')
    print()

    # ———— 表2（原）: 按股数划分 — 流入/流出 ————
    cursor.execute(f"""
        WITH categorized AS (
            SELECT volume, turnover, ticker_direction, {category_sql} AS category
            FROM tick_data
            WHERE stock_code = %(stock_code)s
              AND turnover IS NOT NULL AND turnover > 0
              {date_filter}
        )
        SELECT category,
               SUM(CASE WHEN ticker_direction = 'BUY'  THEN 1 ELSE 0 END)  AS buy_cnt,
               SUM(CASE WHEN ticker_direction = 'BUY'  THEN volume ELSE 0 END)::BIGINT AS buy_vol,
               ROUND(SUM(CASE WHEN ticker_direction = 'BUY'  THEN turnover ELSE 0 END))::BIGINT AS buy_amt,
               SUM(CASE WHEN ticker_direction = 'SELL' THEN 1 ELSE 0 END)  AS sell_cnt,
               SUM(CASE WHEN ticker_direction = 'SELL' THEN volume ELSE 0 END)::BIGINT AS sell_vol,
               ROUND(SUM(CASE WHEN ticker_direction = 'SELL' THEN turnover ELSE 0 END))::BIGINT AS sell_amt,
               SUM(CASE WHEN ticker_direction = 'BUY'  THEN volume ELSE 0 END)
               - SUM(CASE WHEN ticker_direction = 'SELL' THEN volume ELSE 0 END)::BIGINT AS net_vol,
               ROUND(SUM(CASE WHEN ticker_direction = 'BUY'  THEN turnover ELSE 0 END)
                   - SUM(CASE WHEN ticker_direction = 'SELL' THEN turnover ELSE 0 END))::BIGINT AS net_amt
        FROM categorized
        GROUP BY category
        ORDER BY CASE category
            WHEN '特大(大机构)' THEN 1 WHEN '大单(一般机构)' THEN 2
            WHEN '中单(游资)' THEN 3 ELSE 4 END
    """, params)

    print(f'\n{"─" * 120}')
    print(f'  四、按股数划分 — 流入/流出（BUY=主动买入, SELL=主动卖出）')
    print(f'{"─" * 120}')
    h2 = (
        f'{"类别":<16} '
        f'{"BUY笔数":>7} {"流入股数":>12} {"流入金额":>14}  '
        f'{"SELL笔数":>8} {"流出股数":>12} {"流出金额":>14}  '
        f'{"净流入股数":>12} {"净流入金额":>14}'
    )
    print(h2)
    print('-' * len(h2))

    def h(num):
        if abs(num) >= 1_0000_0000:
            return f'{num/1_0000_0000:.1f}亿'
        elif abs(num) >= 1_0000:
            return f'{num/1_0000:.1f}万'
        return str(num)

    for row in cursor.fetchall():
        print(f'{row[0]:<16} '
              f'{row[1]:>7} {h(row[2]):>12} {h(row[3]):>14}  '
              f'{row[4]:>8} {h(row[5]):>12} {h(row[6]):>14}  '
              f'{h(row[7]):>12} {h(row[8]):>14}')
    print()


def main():
    stock_code = sys.argv[1] if len(sys.argv) > 1 else 'HK.00700'
    trade_date = sys.argv[2] if len(sys.argv) > 2 else None

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM tick_data WHERE stock_code=%s LIMIT 1", (stock_code,))
        if cur.fetchone() is None:
            print(f"未找到 {stock_code} 的 tick_data，请检查股票代码或日期")
            return
        analyze(cur, stock_code, trade_date)


if __name__ == '__main__':
    main()
