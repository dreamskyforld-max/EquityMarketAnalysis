"""
订单行为分析工具 (Order Behavior Analyzer)
===========================================
将 trend_segment 与 tick_data 联合分析，按 小/中/大/特大单 四档分类，
分析每档订单在不同节奏/方向段中的行为特征。

维度：
  1. 节奏-订单画像（笔数密度、金额密度）
  2. 方向一致性（BUY比例 vs 段方向）
  3. 大资金热点（任意订单类型的段级分布 + 时段分布）
  4. 价格冲击（前后5笔价格变化，bps）
  5. 综合画像表（每段一行）
  6. 顺势/逆势分类（涨段: 推升/出货, 跌段: 吸筹/做空）

用法：
  python3 order_behavior_analyzer.py [--stock HK.00700] [--date 2026-06-18]

依赖：psycopg2, numpy
"""
import psycopg2
import sys
import numpy as np
from collections import defaultdict
from dataclasses import dataclass, field

# ============================================================
# 配置
# ============================================================

ORDER_TYPES = [
    ("小单",   0,        150000),     # <15万: 散户(300股以内)
    ("中单",   150000,   3000000),    # 15~300万: 中型体量
    ("大单",   3000000,  10000000),   # 300~1000万: 主力大单
    ("特大单", 10000000, float("inf")), # ≥1000万: 孤立巨单
]


def classify_order(turnover):
    for label, lo, hi in ORDER_TYPES:
        if lo <= turnover < hi:
            return label
    return "未知"


# 顺势/逆势标签
# 上涨段: BUY=追涨, SELL=高抛
# 下跌段: SELL=杀跌, BUY=低吸
# 横盘段: BUY=看多建仓, SELL=主动减仓
STRATEGY_LABELS = {
    "上涨": {"BUY": "追涨", "SELL": "高抛"},
    "下跌": {"BUY": "低吸", "SELL": "杀跌"},
    "横盘": {"BUY": "看多建仓", "SELL": "主动减仓"},
}

STRATEGY_ORDER = ["追涨", "低吸", "杀跌", "高抛", "看多建仓", "主动减仓"]


def get_db_conn():
    """复用项目统一数据库连接（config.conf，由 db.py 加载）"""
    from db import DB_CONFIG
    return psycopg2.connect(**DB_CONFIG)


def next_date(d):
    """简单+1天，仅用于 BETWEEN 查询"""
    from datetime import datetime, timedelta
    dt = datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


# ============================================================
# 分析器类
# ============================================================

class OrderBehaviorAnalyzer:
    def __init__(self, stock="HK.00700", date="2026-06-18"):
        self.stock = stock
        self.date = date
        self.next_date = next_date(date)
        self.conn = get_db_conn()
        self.cur = self.conn.cursor()

    def close(self):
        self.cur.close()
        self.conn.close()

    # ================================================================
    # 维度1: 节奏-订单画像
    # ================================================================
    def dim1_rhythm_order_profile(self):
        """每种节奏段中各订单类型的每分钟笔数、金额密度"""
        rhythm_ticks, rhythm_dur, rhythm_order = self._load_dim1_data()

        # ── 打印 ──
        cat_names = [c[0] for c in ORDER_TYPES]
        print("=" * 120)
        print("维度1: 节奏-订单画像（每分钟笔数 / 每分钟金额(万)）")
        print("=" * 120)
        header = f"{'节奏':<8} {'段数':>4} {'时长':>5}"
        for label, _, _ in ORDER_TYPES:
            header += f" | {label:>10}"
        header += f" | {'合计':>10}"
        print(header)
        print("-" * 120)

        for rhythm in rhythm_order:
            total_min, seg_cnt = rhythm_dur.get(rhythm, (1, 1))
            vals = rhythm_ticks.get(rhythm, [])
            row = f"{rhythm:<8} {seg_cnt:>4} {total_min:>4}min"
            for label, lo, hi in ORDER_TYPES:
                cat_vals = [v for v in vals if lo <= v < hi]
                cnt_pm = len(cat_vals) / total_min if total_min > 0 else 0
                row += f" | {cnt_pm:>4.1f}笔"
            all_pm = len(vals) / total_min if total_min > 0 else 0
            row += f" | {all_pm:>4.1f}笔"
            print(row)
            row2 = f"{'':<8} {'':>4} {'':>5}"
            for label, lo, hi in ORDER_TYPES:
                cat_vals = [v for v in vals if lo <= v < hi]
                amt_pm = sum(cat_vals) / total_min / 1e4 if total_min > 0 else 0
                row2 += f" | {amt_pm:>8.0f}万"
            all_amt_pm = sum(vals) / total_min / 1e4 if total_min > 0 else 0
            row2 += f" | {all_amt_pm:>8.0f}万"
            print(row2)
        print()

    def _load_dim1_data(self):
        """加载维度1原始数据（无打印，供 to_dict 复用）"""
        self.cur.execute("""
            SELECT ts.l2_rhythm, ts.l2_global_idx, ts.duration_min, td.turnover
            FROM trend_segment ts
            JOIN tick_data td ON td.tick_time >= ts.start_time AND td.tick_time < ts.end_time
            WHERE ts.stock_code = %s AND ts.trade_date = %s
              AND td.stock_code = %s
              AND td.ticker_direction IN ('BUY','SELL')
        """, (self.stock, self.date, self.stock))

        rhythm_ticks = defaultdict(list)
        for rhythm, seg_id, dur, turnover in self.cur.fetchall():
            rhythm_ticks[rhythm].append(float(turnover) if turnover is not None else 0)

        self.cur.execute("""
            SELECT l2_rhythm, SUM(duration_min), COUNT(*)
            FROM trend_segment
            WHERE stock_code = %s AND trade_date = %s
            GROUP BY l2_rhythm
        """, (self.stock, self.date))
        rhythm_dur = {r[0]: (r[1], r[2]) for r in self.cur.fetchall()}
        rhythm_order = sorted(rhythm_dur.keys(), key=lambda r: rhythm_dur.get(r, (0, 0))[1], reverse=True)

        return rhythm_ticks, rhythm_dur, rhythm_order

    # ================================================================
    # 维度2: 方向一致性
    # ================================================================
    def dim2_direction_consistency(self):
        """每段内各订单类型 BUY 比例，按段方向（上涨/下跌）汇总"""
        consistency, rhythm_order = self._load_dim2_data()
        cat_names = [c[0] for c in ORDER_TYPES]

        print("=" * 100)
        print("维度2: 方向一致性（BUY笔数占比）")
        print("=" * 100)
        header = f"{'节奏':<8} {'段方向':<6}"
        for cat in cat_names:
            header += f" | {cat:>8}"
        print(header)
        print("-" * 80)

        for rhythm in rhythm_order:
            for l1_dir in ["下跌", "上涨", "横盘"]:
                key = (rhythm, l1_dir)
                if key not in consistency:
                    continue
                d = consistency[key]
                row = f"{rhythm:<8} {l1_dir:<6}"
                for cat in cat_names:
                    b = d[cat]["BUY"]
                    t = d[cat]["total"]
                    ratio = b / t if t > 0 else 0
                    row += f" | {ratio:>7.3f}"
                print(row)

        print("-" * 80)
        total_agg = defaultdict(lambda: defaultdict(lambda: {"BUY": 0, "total": 0}))
        for (rhythm, l1_dir), d in consistency.items():
            for cat in cat_names:
                total_agg[l1_dir][cat]["BUY"] += d[cat]["BUY"]
                total_agg[l1_dir][cat]["total"] += d[cat]["total"]

        for l1_dir in ["下跌", "上涨", "横盘"]:
            row = f"{'合计':<8} {l1_dir:<6}"
            d = total_agg[l1_dir]
            for cat in cat_names:
                b = d[cat]["BUY"]
                t = d[cat]["total"]
                ratio = b / t if t > 0 else 0
                row += f" | {ratio:>7.3f}"
            print(row)

        print()

    def _load_dim2_data(self):
        """加载维度2原始数据（无打印），返回 (consistency, rhythm_order)"""
        self.cur.execute("""
            SELECT ts.l2_rhythm, ts.l1_direction, td.ticker_direction, td.turnover
            FROM trend_segment ts
            JOIN tick_data td ON td.tick_time >= ts.start_time AND td.tick_time < ts.end_time
            WHERE ts.stock_code = %s AND ts.trade_date = %s
              AND td.stock_code = %s
              AND td.ticker_direction IN ('BUY','SELL')
        """, (self.stock, self.date, self.stock))

        consistency = defaultdict(lambda: defaultdict(lambda: {"BUY": 0, "total": 0}))
        for rhythm, l1_dir, tick_dir, turnover in self.cur.fetchall():
            cat = classify_order(turnover)
            key = (rhythm, l1_dir)
            consistency[key][cat]["total"] += 1
            if tick_dir == "BUY":
                consistency[key][cat]["BUY"] += 1

        rhythm_order = sorted(set(k[0] for k in consistency.keys()))
        return consistency, rhythm_order

    # ================================================================
    # 维度3: 大资金热点（可针对任意订单类型）
    # ================================================================
    def dim3_heatmaps(self, focus_order=None):
        """检测各订单类型在段级、时段的分布。
        focus_order: 若指定，只分析该类型；否则分析全部。
        """
        focus_labels = [focus_order] if focus_order else [c[0] for c in ORDER_TYPES]

        for target in focus_labels:
            lo, hi = None, None
            for label, l, h in ORDER_TYPES:
                if label == target:
                    lo, hi = l, h
                    break
            if lo is None:
                continue

            print("=" * 80)
            print(f"维度3: {target} 热点分布")
            print("=" * 80)

            self.cur.execute("""
                SELECT ts.l2_global_idx, ts.l2_rhythm, ts.l1_direction,
                       ts.change_pct, ts.duration_min,
                       COUNT(*) AS cnt,
                       SUM(td.turnover)::float / 1e4 AS amt_wan,
                       AVG(td.turnover)::float / 1e4 AS avg_wan,
                       AVG(td.price)::float AS avg_price
                FROM trend_segment ts
                JOIN tick_data td ON td.tick_time >= ts.start_time AND td.tick_time < ts.end_time
                WHERE ts.stock_code = %s AND ts.trade_date = %s
                  AND td.stock_code = %s
                  AND td.ticker_direction IN ('BUY','SELL')
                  AND td.turnover >= %s AND td.turnover < %s
                GROUP BY ts.l2_global_idx, ts.l2_rhythm, ts.l1_direction,
                         ts.change_pct, ts.duration_min
                ORDER BY cnt DESC
            """, (self.stock, self.date, self.stock, lo, hi))

            hotspots = self.cur.fetchall()
            total = sum(r[5] for r in hotspots)
            seg_total = len(hotspots)

            print(f"总计: {total}笔，分布在 {seg_total} 段中，均 {total/max(seg_total,1):.1f} 笔/段")

            # Top-5
            print(f"\nTop-5 热点段:")
            for i, r in enumerate(hotspots[:5]):
                print(f"  #{r[0]:<3} {r[1]:<8} {r[2]:<4} {r[3]:>+7.3f}%  {r[5]:>5}笔  {r[6]:>10.0f}万  均{r[7]:>8.0f}万  均{r[8]:>8.2f}元")

            # 时段分布
            self.cur.execute("""
                SELECT CASE
                    WHEN EXTRACT(HOUR FROM td.tick_time) = 9 AND EXTRACT(MINUTE FROM td.tick_time) < 35 THEN '09:30-09:35'
                    WHEN EXTRACT(HOUR FROM td.tick_time) = 9 THEN '09:35-09:59'
                    WHEN EXTRACT(HOUR FROM td.tick_time) = 10 THEN '10:00-10:59'
                    WHEN EXTRACT(HOUR FROM td.tick_time) = 11 THEN '11:00-11:59'
                    WHEN EXTRACT(HOUR FROM td.tick_time) = 13 THEN '13:00-13:59'
                    WHEN EXTRACT(HOUR FROM td.tick_time) = 14 THEN '14:00-14:59'
                    WHEN EXTRACT(HOUR FROM td.tick_time) = 15 THEN '15:00-16:00'
                END AS time_bucket,
                COUNT(*) AS cnt,
                SUM(td.turnover)::float / 1e4 AS amt_wan
                FROM tick_data td
                WHERE td.stock_code = %s
                  AND td.tick_time >= %s AND td.tick_time < %s
                  AND td.ticker_direction IN ('BUY','SELL')
                  AND td.turnover >= %s AND td.turnover < %s
                GROUP BY time_bucket
                ORDER BY time_bucket
            """, (self.stock, self.date, self.next_date, lo, hi))

            print(f"\n时段分布:")
            for bucket, cnt, amt in self.cur.fetchall():
                bar = "█" * int(cnt / max(1, total) * 40)
                print(f"  {bucket:<12} {cnt:>5}笔 {amt:>10.0f}万 {bar}")

            # 按节奏汇总
            print(f"\n按节奏分布:")
            rhythm_cnt = defaultdict(lambda: 0)
            rhythm_amt = defaultdict(lambda: 0.0)
            for r in hotspots:
                rhythm_cnt[r[1]] += r[5]
                rhythm_amt[r[1]] += r[6]
            for rhythm in sorted(rhythm_cnt.keys(), key=lambda x: rhythm_cnt[x], reverse=True):
                print(f"  {rhythm:<8} {rhythm_cnt[rhythm]:>5}笔  {rhythm_amt[rhythm]:>10.0f}万")

            print()

        # 跨类型对比
        print("=" * 80)
        print("维度3+: 所有订单类型 热点对比")
        print("=" * 80)
        print(f"{'订单类型':<8} {'总笔数':>8} {'关联段数':>8} {'覆盖率':>8}")
        print("-" * 36)
        for label, lo, hi in ORDER_TYPES:
            self.cur.execute("""
                SELECT COUNT(*) FROM (
                    SELECT DISTINCT ts.l2_global_idx
                    FROM trend_segment ts
                    JOIN tick_data td ON td.tick_time >= ts.start_time AND td.tick_time < ts.end_time
                    WHERE ts.stock_code = %s AND ts.trade_date = %s
                      AND td.stock_code = %s
                      AND td.ticker_direction IN ('BUY','SELL')
                      AND td.turnover >= %s AND td.turnover < %s
                ) sub
            """, (self.stock, self.date, self.stock, lo, hi))
            seg_count = self.cur.fetchone()[0]
            self.cur.execute("""
                SELECT COUNT(DISTINCT l2_global_idx)
                FROM trend_segment
                WHERE stock_code = %s AND trade_date = %s
            """, (self.stock, self.date))
            total_segs = self.cur.fetchone()[0]
            self.cur.execute("""
                SELECT COUNT(*)
                FROM tick_data
                WHERE stock_code = %s AND tick_time >= %s AND tick_time < %s
                  AND ticker_direction IN ('BUY','SELL')
                  AND turnover >= %s AND turnover < %s
            """, (self.stock, self.date, self.next_date, lo, hi))
            total_cnt = self.cur.fetchone()[0]
            coverage = seg_count / total_segs * 100 if total_segs > 0 else 0.0
            print(f"{label:<8} {total_cnt:>8} {seg_count:>8}/{total_segs} {coverage:>7.1f}%")
        print()

    # ================================================================
    # 维度4: 价格冲击
    # ================================================================
    def dim4_price_impact(self):
        """每笔订单前后5笔价格的变动（bps）"""
        self.cur.execute("""
            SELECT ts.l2_global_idx, ts.l2_rhythm, ts.l1_direction,
                   td.tick_time, td.price, td.turnover
            FROM trend_segment ts
            JOIN tick_data td ON td.tick_time >= ts.start_time AND td.tick_time < ts.end_time
            WHERE ts.stock_code = %s AND ts.trade_date = %s
              AND td.stock_code = %s
              AND td.ticker_direction IN ('BUY','SELL')
            ORDER BY ts.l2_global_idx, td.tick_time
        """, (self.stock, self.date, self.stock))

        seg_ticks = defaultdict(list)
        for seg_id, rhythm, direction, tick_time, price, turnover in self.cur.fetchall():
            cat = classify_order(turnover)
            seg_ticks[seg_id].append({
                "rhythm": rhythm,
                "direction": direction,
                "price": float(price) if price is not None else None,
                "cat": cat,
            })

        impact_by_cat = defaultdict(list)
        impact_by_rhythm_cat = defaultdict(lambda: defaultdict(list))

        for seg_id, ticks in seg_ticks.items():
            prices = [t["price"] for t in ticks]
            for i, t in enumerate(ticks):
                impact = self._calc_impact(prices, i, window=5)
                if impact is not None:
                    impact_by_cat[t["cat"]].append(impact)
                    impact_by_rhythm_cat[t["rhythm"]][t["cat"]].append(impact)

        print("=" * 80)
        print("维度4: 价格冲击（前5笔→后5笔价格变化，bps）")
        print("=" * 80)

        cat_names = [c[0] for c in ORDER_TYPES]
        print(f"{'订单类型':<8} {'笔数':>7} {'均值(bps)':>10} {'中位(bps)':>10} {'正向%':>7} {'负向%':>7} {'STD':>8}")
        print("-" * 60)

        for cat in cat_names:
            vals = impact_by_cat.get(cat, [])
            if vals:
                arr = np.array(vals)
                pos_pct = np.mean(arr > 0) * 100
                neg_pct = np.mean(arr < 0) * 100
                print(f"{cat:<8} {len(arr):>7} {np.mean(arr)*100:>10.2f} {np.median(arr)*100:>10.2f} {pos_pct:>6.1f}% {neg_pct:>6.1f}% {np.std(arr)*100:>8.2f}")

        # 按节奏细分
        rhythm_order = sorted(impact_by_rhythm_cat.keys())
        print(f"\n按节奏-订单类型细分:")
        header = f"{'节奏':<8}"
        for cat in cat_names:
            header += f" {cat:>10}"
        print(header)
        print("-" * 50)

        for rhythm in rhythm_order:
            print(f"{rhythm:<8}", end="")
            for cat in cat_names:
                vals = impact_by_rhythm_cat[rhythm].get(cat, [])
                if vals:
                    bps = np.mean(vals) * 100
                    print(f" {bps:>9.2f}bp", end="")
                else:
                    print(f" {'-':>9}", end="")
            print()
        print()

        return impact_by_cat

    @staticmethod
    def _calc_impact(prices, i, window=5):
        pre = [p for p in prices[max(0, i - window):i] if p is not None]
        post = [p for p in prices[i + 1:i + 1 + window] if p is not None]
        if len(pre) == 0 or len(post) == 0:
            return None
        pre_avg = np.mean(pre)
        post_avg = np.mean(post)
        if pre_avg == 0:
            return None
        return (post_avg - pre_avg) / pre_avg  # 小数

    # ================================================================
    # 维度5: 综合画像表
    # ================================================================
    def dim5_segment_snapshot(self):
        """每段一行完整特征"""
        self.cur.execute("""
            SELECT ts.l2_global_idx, ts.l2_rhythm, ts.l1_direction,
                   ts.change_pct, ts.duration_min,
                   ts.start_price, ts.end_price,
                   COUNT(*) AS tick_cnt,
                   SUM(td.turnover)::float / 1e4 AS total_amt_wan,
                   AVG(td.price)::float AS avg_price,
                   SUM(CASE WHEN td.turnover < 50000 THEN 1 ELSE 0 END) AS small_cnt,
                   SUM(CASE WHEN td.turnover < 50000 THEN td.turnover ELSE 0 END)::float / 1e4 AS small_amt,
                   SUM(CASE WHEN td.turnover >= 50000 AND td.turnover < 500000 THEN 1 ELSE 0 END) AS mid_cnt,
                   SUM(CASE WHEN td.turnover >= 50000 AND td.turnover < 500000 THEN td.turnover ELSE 0 END)::float / 1e4 AS mid_amt,
                   SUM(CASE WHEN td.turnover >= 500000 AND td.turnover < 3000000 THEN 1 ELSE 0 END) AS big_cnt,
                   SUM(CASE WHEN td.turnover >= 500000 AND td.turnover < 3000000 THEN td.turnover ELSE 0 END)::float / 1e4 AS big_amt,
                   SUM(CASE WHEN td.turnover >= 3000000 THEN 1 ELSE 0 END) AS super_cnt,
                   SUM(CASE WHEN td.turnover >= 3000000 THEN td.turnover ELSE 0 END)::float / 1e4 AS super_amt,
                   COALESCE(SUM(CASE WHEN td.ticker_direction = 'BUY' AND td.turnover < 50000 THEN 1 ELSE 0 END)::float
                     / NULLIF(SUM(CASE WHEN td.turnover < 50000 THEN 1 ELSE 0 END), 0), 0) AS small_buy,
                   COALESCE(SUM(CASE WHEN td.ticker_direction = 'BUY' AND td.turnover >= 50000 AND td.turnover < 500000 THEN 1 ELSE 0 END)::float
                     / NULLIF(SUM(CASE WHEN td.turnover >= 50000 AND td.turnover < 500000 THEN 1 ELSE 0 END), 0), 0) AS mid_buy,
                   COALESCE(SUM(CASE WHEN td.ticker_direction = 'BUY' AND td.turnover >= 500000 AND td.turnover < 3000000 THEN 1 ELSE 0 END)::float
                     / NULLIF(SUM(CASE WHEN td.turnover >= 500000 AND td.turnover < 3000000 THEN 1 ELSE 0 END), 0), 0) AS big_buy,
                   COALESCE(SUM(CASE WHEN td.ticker_direction = 'BUY' AND td.turnover >= 3000000 THEN 1 ELSE 0 END)::float
                     / NULLIF(SUM(CASE WHEN td.turnover >= 3000000 THEN 1 ELSE 0 END), 0), 0) AS super_buy
            FROM trend_segment ts
            JOIN tick_data td ON td.tick_time >= ts.start_time AND td.tick_time < ts.end_time
            WHERE ts.stock_code = %s AND ts.trade_date = %s
              AND td.stock_code = %s
              AND td.ticker_direction IN ('BUY','SELL')
            GROUP BY ts.l2_global_idx, ts.l2_rhythm, ts.l1_direction,
                     ts.change_pct, ts.duration_min,
                     ts.start_price, ts.end_price
            ORDER BY ts.l2_global_idx
        """, (self.stock, self.date, self.stock))

        print("=" * 120)
        print("维度5: 综合画像表（每段一行）")
        print("=" * 120)

        header = (
            f"{'#':>3} {'节奏':<8} {'方向':<4} {'涨跌%':>8} {'时长':>4} "
            f"{'总笔':>6} {'总万':>10} "
            f"{'小笔':>5} {'小万':>9} {'中笔':>5} {'中万':>9} {'大笔':>4} {'大万':>9} {'特笔':>4} {'特万':>9} "
            f"{'小买比':>6} {'中买比':>6} {'大买比':>6} {'特买比':>6}"
        )
        print(header)
        print("-" * len(header))

        for r in self.cur.fetchall():
            (seg_id, rhythm, direction, chg, dur,
             start_price, end_price,
             tick_cnt, total_amt, avg_price,
             small_cnt, small_amt, mid_cnt, mid_amt, big_cnt, big_amt, super_cnt, super_amt,
             small_buy, mid_buy, big_buy, super_buy) = r
            print(
                f"{seg_id:>3} {rhythm:<8} {direction:<4} {chg:>+8.3f} {dur:>4}min "
                f"{tick_cnt:>6} {total_amt:>10.0f} "
                f"{small_cnt:>5} {small_amt or 0:>9.0f} "
                f"{mid_cnt:>5} {mid_amt or 0:>9.0f} "
                f"{big_cnt:>4} {big_amt or 0:>9.0f} "
                f"{super_cnt:>4} {super_amt or 0:>9.0f} "
                f"{small_buy or 0:>6.3f} {mid_buy or 0:>6.3f} {big_buy or 0:>6.3f} {super_buy or 0:>6.3f}"
            )
        print()

    # ================================================================
    # 共享：加载趋势段 × tick 博弈数据（dim6 / dim7 共用，不含打印）
    # ================================================================
    def _load_strategy_data(self):
        """
        返回 (strategy_data, total_strategy)
        strategy_data: rhythm → order_cat → strategy_label → {cnt, amt}
        total_strategy: order_cat → strategy_label → {cnt, amt}（跨节奏汇总）
        """
        self.cur.execute("""
            SELECT ts.l2_global_idx, ts.l2_rhythm, ts.l1_direction,
                   td.ticker_direction, td.turnover
            FROM trend_segment ts
            JOIN tick_data td ON td.tick_time >= ts.start_time AND td.tick_time < ts.end_time
            WHERE ts.stock_code = %s AND ts.trade_date = %s
              AND td.stock_code = %s
              AND td.ticker_direction IN ('BUY','SELL')
            ORDER BY ts.l2_global_idx, td.tick_time
        """, (self.stock, self.date, self.stock))

        strategy_data = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: {"cnt": 0, "amt": 0.0})))

        for seg_id, rhythm, l1_dir, tick_dir, turnover in self.cur.fetchall():
            cat = classify_order(turnover)
            label = STRATEGY_LABELS.get(l1_dir, {}).get(tick_dir, "未知")
            strategy_data[rhythm][cat][label]["cnt"] += 1
            strategy_data[rhythm][cat][label]["amt"] += float(turnover) if turnover is not None else 0

        # 跨节奏汇总
        total_strategy = defaultdict(lambda: defaultdict(lambda: {"cnt": 0, "amt": 0.0}))
        for rhythm, cat_dict in strategy_data.items():
            for cat, label_dict in cat_dict.items():
                for label, v in label_dict.items():
                    total_strategy[cat][label]["cnt"] += v["cnt"]
                    total_strategy[cat][label]["amt"] += v["amt"]

        return strategy_data, total_strategy

    # ================================================================
    # 维度6: 顺势/逆势分类（核心分析）
    # ================================================================
    def dim6_strategy_analysis(self):
        """
        将每笔订单按「段方向 + 订单方向」贴标签：
          上涨段 + BUY = 追涨
          上涨段 + SELL = 高抛
          下跌段 + SELL = 杀跌
          下跌段 + BUY = 低吸
          横盘段 + BUY = 看多建仓
          横盘段 + SELL = 主动减仓

        分层：按节奏（急跌/缓跌/...）× 订单大小（小/中/大/特大）→ 输出博弈矩阵。
        """
        strategy_data, total_strategy = self._load_strategy_data()

        print("=" * 120)
        print("维度6: 顺势/逆势博弈分析")
        print("       上涨段: BUY=追涨, SELL=高抛")
        print("       下跌段: SELL=杀跌, BUY=低吸")
        print("=" * 120)

        cat_names = [c[0] for c in ORDER_TYPES]
        # 按订单类型分块展示
        for cat in cat_names:
            print(f"\n{'─' * 100}")
            print(f"  【{cat}】顺势/逆势博弈矩阵")
            print(f"{'─' * 100}")

            # 涨段博弈
            print(f"\n  涨段内: 追涨 ←→ 高抛")
            print(f"  {'节奏':<8} | {'追涨':>25} | {'高抛':>25} | {'净买万':>10} {'推升比':>6}")
            print(f"  {'':>8} | {'笔数 金额(万)':>25} | {'笔数 金额(万)':>25} | {'':>10} {'':>6}")
            print(f"  {'─' * 95}")

            for rhythm in sorted(strategy_data.keys()):
                d = strategy_data[rhythm][cat]
                push = d.get("追涨", {"cnt": 0, "amt": 0})
                dump = d.get("高抛", {"cnt": 0, "amt": 0})
                if push["cnt"] + dump["cnt"] == 0:
                    continue
                net = (push["amt"] - dump["amt"]) / 1e4
                total = push["cnt"] + dump["cnt"]
                push_ratio = push["cnt"] / total * 100 if total > 0 else 0
                print(
                    f"  {rhythm:<8} | {push['cnt']:>4}笔 {push['amt']/1e4:>15.0f}万 "
                    f"| {dump['cnt']:>4}笔 {dump['amt']/1e4:>15.0f}万 "
                    f"| {net:>+9.0f}万 {push_ratio:>5.0f}%")

            # 跌段博弈
            print(f"\n  跌段内: 低吸 ←→ 杀跌")
            print(f"  {'节奏':<8} | {'低吸':>25} | {'杀跌':>25} | {'净买万'} {'吸筹比':>6}")
            print(f"  {'':>8} | {'笔数 金额(万)':>25} | {'笔数 金额(万)':>25} |")
            print(f"  {'─' * 95}")

            for rhythm in sorted(strategy_data.keys()):
                d = strategy_data[rhythm][cat]
                absorb = d.get("低吸", {"cnt": 0, "amt": 0})
                short = d.get("杀跌", {"cnt": 0, "amt": 0})
                if absorb["cnt"] + short["cnt"] == 0:
                    continue
                net = (absorb["amt"] - short["amt"]) / 1e4
                total = absorb["cnt"] + short["cnt"]
                absorb_ratio = absorb["cnt"] / total * 100 if total > 0 else 0
                print(
                    f"  {rhythm:<8} | {absorb['cnt']:>4}笔 {absorb['amt']/1e4:>15.0f}万 "
                    f"| {short['cnt']:>4}笔 {short['amt']/1e4:>15.0f}万 "
                    f"| {net:>+9.0f}万 {absorb_ratio:>5.0f}%")

        # 汇总：全类型对比
        print(f"\n{'─' * 100}")
        print(f"  全订单类型 汇总对比")
        print(f"{'─' * 100}")

        # 全订单类型汇总对比（total_strategy 已由 _load_strategy_data 计算）

        print(f"\n  {'订单':<8} | {'追涨':>20} | {'高抛':>20} | {'低吸':>20} | {'杀跌':>20}")
        print(f"  {'':>8} | {'笔数 金额(万)':>20} | {'笔数 金额(万)':>20} | {'笔数 金额(万)':>20} | {'笔数 金额(万)':>20}")
        print(f"  {'─' * 100}")

        for cat in cat_names:
            d = total_strategy[cat]
            push = d.get("追涨", {"cnt": 0, "amt": 0})
            dump = d.get("高抛", {"cnt": 0, "amt": 0})
            absorb = d.get("低吸", {"cnt": 0, "amt": 0})
            short = d.get("杀跌", {"cnt": 0, "amt": 0})
            print(
                f"  {cat:<8} | {push['cnt']:>4}笔 {push['amt']/1e4:>14.0f}万 "
                f"| {dump['cnt']:>4}笔 {dump['amt']/1e4:>14.0f}万 "
                f"| {absorb['cnt']:>4}笔 {absorb['amt']/1e4:>14.0f}万 "
                f"| {short['cnt']:>4}笔 {short['amt']/1e4:>14.0f}万")

        # 多方 vs 空方汇总
        print(f"\n  多方 vs 空方 力量对比:")
        for cat in cat_names:
            d = total_strategy[cat]
            push = d.get("追涨", {"cnt": 0, "amt": 0})
            absorb = d.get("低吸", {"cnt": 0, "amt": 0})
            dump = d.get("高抛", {"cnt": 0, "amt": 0})
            short = d.get("杀跌", {"cnt": 0, "amt": 0})
            bull_cnt = push["cnt"] + absorb["cnt"]
            bull_amt = (push["amt"] + absorb["amt"]) / 1e4
            bear_cnt = dump["cnt"] + short["cnt"]
            bear_amt = (dump["amt"] + short["amt"]) / 1e4
            up_bull_pct = push["cnt"] / (push["cnt"] + dump["cnt"]) * 100 if (push["cnt"] + dump["cnt"]) > 0 else 0
            dn_bull_pct = absorb["cnt"] / (absorb["cnt"] + short["cnt"]) * 100 if (absorb["cnt"] + short["cnt"]) > 0 else 0
            print(
                f"  {cat:<8} 多方: {bull_cnt:>5}笔 {bull_amt:>12.0f}万  |  空方: {bear_cnt:>5}笔 {bear_amt:>12.0f}万  |  "
                f"净: {bull_amt-bear_amt:>+10.0f}万  |  多方占: 涨段{up_bull_pct:>5.0f}% 跌段{dn_bull_pct:>5.0f}%")

        # 进攻/被动比例
        print(f"\n  多方资金分配（推升 vs 吸筹）:")
        for cat in cat_names:
            d = total_strategy[cat]
            push = d.get("追涨", {"cnt": 0, "amt": 0})
            absorb = d.get("低吸", {"cnt": 0, "amt": 0})
            total_amt = push["amt"] + absorb["amt"]
            if total_amt > 0:
                push_pct = push["amt"] / total_amt * 100
                absorb_pct = absorb["amt"] / total_amt * 100
                print(f"  {cat:<8} 推升(进攻) {push['amt']/1e4:>10.0f}万 ({push_pct:>5.0f}%)  |  吸筹(防守) {absorb['amt']/1e4:>10.0f}万 ({absorb_pct:>5.0f}%)")

        print()
        return strategy_data, total_strategy

    # ================================================================
    # 维度7: 叙事分析报告（每个订单类型的 多方vs空方 深度解读）
    # ================================================================
    def dim7_narrative_report(self):
        """
        对每个订单类型，生成结构化的叙事分析报告：
        - 多方 vs 空方 总体力量对比
        - 证据1：多方资金分配（推升 vs 吸筹）
        - 证据2：涨段博弈（推升 vs 出货，按节奏细分）
        - 证据3：跌段博弈（吸筹 vs 做空，按节奏细分，看多方是否随跌势加速而退缩）
        - 结论
        """
        strategy_data, total = self._load_strategy_data()

        cat_names = [c[0] for c in ORDER_TYPES]

        print("=" * 80)
        print(f"维度7: 叙事分析报告 — {self.stock} {self.date}")
        print("       分析框架：顺势/逆势博弈 × 订单大小")
        print("=" * 80)

        for cat in cat_names:
            d = total[cat]
            push = d.get("追涨", {"cnt": 0, "amt": 0})
            absorb = d.get("低吸", {"cnt": 0, "amt": 0})
            dump = d.get("高抛", {"cnt": 0, "amt": 0})
            short = d.get("杀跌", {"cnt": 0, "amt": 0})

            bull_cnt = push["cnt"] + absorb["cnt"]
            bull_amt = (push["amt"] + absorb["amt"]) / 1e4
            bear_cnt = dump["cnt"] + short["cnt"]
            bear_amt = (dump["amt"] + short["amt"]) / 1e4
            net = bull_amt - bear_amt

            # ── 标题 ──
            print(f"\n{'█' * 80}")
            print(f"█ 【{cat}】多方 vs 空方 力量对比")
            print(f"{'█' * 80}")

            # ── 总览表 ──
            print(f"""
┌──────────┬────────────────────────────────┬────────────────────────────────┐
│          │          多  方                 │          空  方                 │
├──────────┼────────────────────────────────┼────────────────────────────────┤
│ 下跌段   │ 低吸 {absorb['cnt']:>4}笔/{absorb['amt']/1e4:>10.0f}万        │ 杀跌 {short['cnt']:>4}笔/{short['amt']/1e4:>10.0f}万        │
│ 上涨段   │ 追涨 {push['cnt']:>4}笔/{push['amt']/1e4:>10.0f}万        │ 高抛 {dump['cnt']:>4}笔/{dump['amt']/1e4:>10.0f}万        │
├──────────┼────────────────────────────────┼────────────────────────────────┤
│ 合 计    │ {bull_cnt:>4}笔/{bull_amt:>10.0f}万                │ {bear_cnt:>4}笔/{bear_amt:>10.0f}万                │
└──────────┴────────────────────────────────┴────────────────────────────────┘""")

            # 净额判断
            if net > 0:
                print(f"  表面看：多方净买入 {net:+.0f}万，多方占优。")
            else:
                print(f"  表面看：空方净卖出 {net:+.0f}万，空方占优。")

            # ── 证据1 ──
            total_bull = push["amt"] + absorb["amt"]
            push_pct = push["amt"] / total_bull * 100 if total_bull > 0 else 0
            absorb_pct = absorb["amt"] / total_bull * 100 if total_bull > 0 else 0

            print(f"\n  证据1：多方力量集中在 {'进攻' if push_pct > absorb_pct else '防守'}，不在{'进攻' if absorb_pct > push_pct else '防守'}")

            if absorb_pct > 50:
                print(f"  下跌段中多方投入了 {absorb['amt']/1e4:.0f}万 吸筹，占多方总兵力的 {absorb_pct:.0f}%。")
                print(f"  换句话说，多方将近一半的钱花在了\"接飞刀\"上，而不是主动推升。")
            else:
                print(f"  下跌段中多方投入了 {absorb['amt']/1e4:.0f}万 吸筹（{absorb_pct:.0f}%），涨段推升 {push['amt']/1e4:.0f}万（{push_pct:.0f}%）。")
                if push_pct > 60:
                    print(f"  多方更倾向于在上涨中推进，属于主动进攻型资金。")

            # ── 证据2 ──
            print(f"\n  证据2：涨段内 推升 ←→ 出货 博弈")
            up_data = []
            for rhythm in sorted(strategy_data.keys()):
                rd = strategy_data[rhythm][cat]
                p = rd.get("追涨", {"cnt": 0, "amt": 0})
                du = rd.get("高抛", {"cnt": 0, "amt": 0})
                if p["cnt"] + du["cnt"] > 0:
                    up_data.append((rhythm, p, du))

            # 按推升优于出货的程度排序
            up_data.sort(key=lambda x: x[1]["amt"] - x[2]["amt"], reverse=True)

            for rhythm, p, du in up_data:
                diff = (p["amt"] - du["amt"]) / 1e4
                total_amt_wan = (p["amt"] + du["amt"]) / 1e4
                push_p = p["cnt"] / (p["cnt"] + du["cnt"]) * 100 if (p["cnt"] + du["cnt"]) > 0 else 0
                if total_amt_wan < 500:  # 过滤噪音段
                    continue
                bar = "█" * int(abs(diff) / max(abs(diff) for (_, p2, d2) in up_data if (p2["amt"] + d2["amt"]) / 1e4 >= 500) * 15) if up_data else ""
                side = "多方主导" if diff > 1000 else ("空方出货" if diff < -1000 else "势均力敌")
                print(f"    {rhythm:<8} 推升 {p['amt']/1e4:>8.0f}万 ←→ 出货 {du['amt']/1e4:>8.0f}万   净 {diff:>+9.0f}万   {side}")

            # 判断涨得急时出货是否增多
            urgent_up = [x for x in up_data if "急涨" in x[0] or "加速涨" in x[0]]
            slow_up = [x for x in up_data if "缓涨" in x[0] or "减速涨" in x[0]]
            if urgent_up and slow_up:
                urgent_push_ratio = sum(x[1]["cnt"] for x in urgent_up) / max(sum(x[1]["cnt"] + x[2]["cnt"] for x in urgent_up), 1)
                slow_push_ratio = sum(x[1]["cnt"] for x in slow_up) / max(sum(x[1]["cnt"] + x[2]["cnt"] for x in slow_up), 1)
                if urgent_push_ratio < slow_push_ratio - 0.05:
                    print(f"    ⚠ 急涨段推升比({urgent_push_ratio:.0%}) < 缓涨段({slow_push_ratio:.0%})，涨得越急、出货越多。")
                elif urgent_push_ratio > slow_push_ratio + 0.05:
                    print(f"    ✓ 急涨段推升比({urgent_push_ratio:.0%}) > 缓涨段({slow_push_ratio:.0%})，追涨意愿强于温和上涨。")

            # ── 证据3 ──
            print(f"\n  证据3：跌段的速度揭示谁在控盘")
            down_data = []
            for rhythm in sorted(strategy_data.keys()):
                rd = strategy_data[rhythm][cat]
                a = rd.get("低吸", {"cnt": 0, "amt": 0})
                s = rd.get("杀跌", {"cnt": 0, "amt": 0})
                if a["cnt"] + s["cnt"] > 0:
                    down_data.append((rhythm, a, s))

            # 按做空强度排序
            down_data.sort(key=lambda x: x[2]["amt"] - x[1]["amt"], reverse=True)

            for rhythm, a, s in down_data:
                diff = (a["amt"] - s["amt"]) / 1e4
                total_amt_wan = (a["amt"] + s["amt"]) / 1e4
                absorb_p = a["cnt"] / (a["cnt"] + s["cnt"]) * 100 if (a["cnt"] + s["cnt"]) > 0 else 0
                if total_amt_wan < 500:
                    continue

                # 判断控制方
                if diff > 2000:
                    ctrl = "← 多方局部占优"
                elif diff < -2000:
                    ctrl = "→ 空方主导"
                else:
                    ctrl = "⇄ 均势"

                print(f"    {rhythm:<8} 吸筹 {a['amt']/1e4:>8.0f}万 vs 做空 {s['amt']/1e4:>8.0f}万   {ctrl}")

            # 下跌加速时的多方反应
            accel_down = [x for x in down_data if "加速跌" in x[0]]
            fast_down = [x for x in down_data if "急跌" in x[0]]
            slow_down = [x for x in down_data if "缓跌" in x[0] or "减速跌" in x[0]]

            if accel_down and slow_down:
                accel_absorb_amt = sum(x[1]["amt"] for x in accel_down)
                slow_absorb_amt = sum(x[1]["amt"] for x in slow_down)
                accel_short_amt = sum(x[2]["amt"] for x in accel_down)
                slow_short_amt = sum(x[2]["amt"] for x in slow_down)

                accel_ratio = accel_absorb_amt / max(accel_short_amt, 1) if accel_short_amt > 0 else 999
                slow_ratio = slow_absorb_amt / max(slow_short_amt, 1) if slow_short_amt > 0 else 999

                if accel_ratio < slow_ratio * 0.7:
                    print(f"\n    ⚠ 下跌加速时多方退缩：缓跌阶段吸筹/做空比 {slow_ratio:.2f}，加速跌降至 {accel_ratio:.2f}")
                    print(f"       加速跌中多方投入 {accel_absorb_amt/1e4:.0f}万 vs 缓跌 {slow_absorb_amt/1e4:.0f}万——多方力量随跌势加速反而减弱")
                elif accel_ratio > slow_ratio * 1.3:
                    print(f"\n    ✓ 下跌加速时多方加码：加速跌吸筹力度是缓跌的 {accel_ratio/slow_ratio:.1f}倍")

            # 额外：急跌 vs 缓跌
            if fast_down and slow_down:
                fast_absorb = sum(x[1]["amt"] for x in fast_down)
                slow_absorb_ = sum(x[1]["amt"] for x in slow_down)
                fast_short = sum(x[2]["amt"] for x in fast_down)
                slow_short_ = sum(x[2]["amt"] for x in slow_down)
                fast_r = fast_absorb / max(fast_short, 1)
                slow_r = slow_absorb_ / max(slow_short_, 1)
                if fast_r < slow_r * 0.8:
                    print(f"       急跌阶段同样印证：吸筹/做空比 急跌{fast_r:.2f} < 缓跌{slow_r:.2f}")

            # ── 结论 ──
            print(f"\n  ┌{'─' * 72}┐")
            print(f"  │ 结论：{cat}行为特征")
            print(f"  ├{'─' * 72}┤")

            conclusions = []

            # 空方主导判断
            short_dump_total = (short["amt"] + dump["amt"]) / 1e4
            push_absorb_total = bull_amt
            if short["amt"] > absorb["amt"] * 1.3:
                conclusions.append(f"空方主导：做空+出货={short_dump_total:.0f}万，跌段中做空 vs 吸筹={short['cnt']/max(absorb['cnt'],1):.1f}:1")
            elif absorb["amt"] > short["amt"] * 1.3:
                conclusions.append(f"多方主导：吸筹+推升={push_absorb_total:.0f}万，跌段中吸筹 vs 做空={absorb['cnt']/max(short['cnt'],1):.1f}:1")
            else:
                conclusions.append(f"多空均衡：多方={push_absorb_total:.0f}万 vs 空方={short_dump_total:.0f}万")

            # 被动/主动判断
            if absorb_pct > 55:
                conclusions.append(f"多方被动：{absorb_pct:.0f}% 资金花在下跌段接盘")
            elif push_pct > 60:
                conclusions.append(f"多方主动：{push_pct:.0f}% 资金用于涨段推升")

            # 涨段出货判断
            if push["cnt"] > 0 and dump["cnt"] > 0:
                if dump["cnt"] > push["cnt"] * 0.8:
                    conclusions.append("涨段出货压力大：上涨中空方出货接近推升力度")
                elif dump["cnt"] < push["cnt"] * 0.5:
                    conclusions.append("涨段推升顺利：出货阻力仅为推升的 {:.0%}".format(dump["cnt"] / max(push["cnt"], 1)))

            # 跌段加速退缩判断
            if accel_down and slow_down and accel_ratio < slow_ratio * 0.7:
                conclusions.append("下跌加速时多方退缩，非主动低吸型资金")
            elif accel_down and slow_down and accel_ratio > slow_ratio * 1.3:
                conclusions.append("下跌加速时多方加码，具备主动低吸特征")

            for i, c in enumerate(conclusions, 1):
                print(f"  │ {i}. {c}")

            print(f"  └{'─' * 72}┘")

        # ── 跨类型对比 ──
        print(f"\n{'█' * 80}")
        print(f"█ 跨订单类型横评")
        print(f"{'█' * 80}")
        print(f"\n  {'订单':<8} {'多金额':>10} {'空金额':>10} {'净额':>10} {'涨段多方%':>10} {'跌段多方%':>10} {'推升%':>8} {'特征'}")
        print(f"  {'─' * 85}")

        for cat in cat_names:
            d = total[cat]
            push = d.get("追涨", {"cnt": 0, "amt": 0})
            absorb = d.get("低吸", {"cnt": 0, "amt": 0})
            dump = d.get("高抛", {"cnt": 0, "amt": 0})
            short = d.get("杀跌", {"cnt": 0, "amt": 0})

            bull_amt = (push["amt"] + absorb["amt"]) / 1e4
            bear_amt = (dump["amt"] + short["amt"]) / 1e4
            up_bull_pct = push["cnt"] / max(push["cnt"] + dump["cnt"], 1) * 100
            dn_bull_pct = absorb["cnt"] / max(absorb["cnt"] + short["cnt"], 1) * 100
            total_bull = push["amt"] + absorb["amt"]
            push_pct = push["amt"] / max(total_bull, 1) * 100

            # 简单特征标签
            net_wan = bull_amt - bear_amt
            if net_wan > 5000:
                tag = "多方主导"
            elif net_wan < -5000:
                if absorb["amt"] > push["amt"]:
                    tag = "空方碾压，多方被动防守"
                else:
                    tag = "空方主导"
            else:
                if abs(net_wan) < 2000:
                    tag = "多空均衡"
                elif net_wan < 0:
                    tag = "空方略优"
                else:
                    tag = "多方略优"

            # 跌段多方是否退缩
            # 手动检查
            accel = strategy_data.get("加速跌", {}).get(cat, {})
            slow_d = strategy_data.get("缓跌", {}).get(cat, {})
            a_absorb = accel.get("低吸", {"cnt": 0, "amt": 0})
            a_short = accel.get("杀跌", {"cnt": 0, "amt": 0})
            s_absorb = slow_d.get("低吸", {"cnt": 0, "amt": 0})
            s_short = slow_d.get("杀跌", {"cnt": 0, "amt": 0})
            if a_short["amt"] > a_absorb["amt"] * 2 and s_absorb["amt"] > s_short["amt"] * 0.5:
                tag += " + 加速跌退缩"

            print(f"  {cat:<8} {bull_amt:>10.0f} {bear_amt:>10.0f} {net_wan:>+9.0f}万 {up_bull_pct:>9.0f}% {dn_bull_pct:>9.0f}% {push_pct:>7.0f}%  {tag}")

        print()


    # ================================================================
    # Dashboard 接口：输出标准化 JSON
    # ================================================================
    def _build_dim1_dict(self, rhythm_ticks, rhythm_dur, rhythm_order):
        """维度1 标准化：节奏-订单画像"""
        cat_names = [c[0] for c in ORDER_TYPES]
        rhythms = []
        for rhythm in rhythm_order:
            total_min, seg_cnt = rhythm_dur.get(rhythm, (1, 1))
            vals = rhythm_ticks.get(rhythm, [])
            cats = {}
            for label, lo, hi in ORDER_TYPES:
                cat_vals = [v for v in vals if lo <= v < hi]
                cnt_pm = round(len(cat_vals) / total_min, 1) if total_min > 0 else 0
                amt_pm = round(sum(cat_vals) / total_min / 1e4) if total_min > 0 else 0
                cats[label] = {"count_per_min": cnt_pm, "amt_per_min_wan": amt_pm}
            all_pm = round(len(vals) / total_min, 1) if total_min > 0 else 0
            all_amt = round(sum(vals) / total_min / 1e4) if total_min > 0 else 0
            cats["合计"] = {"count_per_min": all_pm, "amt_per_min_wan": all_amt}
            rhythms.append({
                "rhythm": rhythm, "segment_count": seg_cnt, "duration_min": total_min,
                "orders": cats,
            })
        return {"rhythms": rhythms, "order_types": cat_names}

    def _build_dim2_dict(self, consistency, rhythm_order):
        """维度2 标准化：方向一致性"""
        cat_names = [c[0] for c in ORDER_TYPES]
        rows = []
        for rhythm in rhythm_order:
            for l1_dir in ["下跌", "上涨", "横盘"]:
                key = (rhythm, l1_dir)
                if key not in consistency:
                    continue
                d = consistency[key]
                row = {"rhythm": rhythm, "direction": l1_dir}
                for cat in cat_names:
                    b = d[cat]["BUY"]
                    t = d[cat]["total"]
                    row[cat] = round(b / t, 3) if t > 0 else 0
                rows.append(row)

        # 合计
        totals = []
        total_agg = defaultdict(lambda: defaultdict(lambda: {"BUY": 0, "total": 0}))
        for (rhythm, l1_dir), d in consistency.items():
            for cat in cat_names:
                total_agg[l1_dir][cat]["BUY"] += d[cat]["BUY"]
                total_agg[l1_dir][cat]["total"] += d[cat]["total"]
        for l1_dir in ["下跌", "上涨", "横盘"]:
            d = total_agg[l1_dir]
            row = {"direction": l1_dir}
            for cat in cat_names:
                b = d[cat]["BUY"]
                t = d[cat]["total"]
                row[cat] = round(b / t, 3) if t > 0 else 0
            totals.append(row)

        return {"rows": rows, "totals": totals, "order_types": cat_names}

    def _build_dim6_dict(self, strategy_data, total_strategy):
        """将 dim6 原始数据转换为 Dashboard 可视化所需结构"""
        cat_names = [c[0] for c in ORDER_TYPES]
        result = {}

        for cat in cat_names:
            d = total_strategy[cat]
            push = d.get("追涨", {"cnt": 0, "amt": 0})
            absorb = d.get("低吸", {"cnt": 0, "amt": 0})
            dump = d.get("高抛", {"cnt": 0, "amt": 0})
            short = d.get("杀跌", {"cnt": 0, "amt": 0})

            bull_amt = (push["amt"] + absorb["amt"]) / 1e4
            bear_amt = (dump["amt"] + short["amt"]) / 1e4
            bull_cnt = push["cnt"] + absorb["cnt"]
            bear_cnt = dump["cnt"] + short["cnt"]
            up_bull_pct = round(push["cnt"] / max(push["cnt"] + dump["cnt"], 1) * 100)
            dn_bull_pct = round(absorb["cnt"] / max(absorb["cnt"] + short["cnt"], 1) * 100)
            total_bull = push["amt"] + absorb["amt"]
            push_pct = round(push["amt"] / max(total_bull, 1) * 100) if total_bull > 0 else 0

            summary = {
                "multiparty_cnt": bull_cnt,
                "multiparty_amt": round(bull_amt),
                "multiparty_push_amt": round(push["amt"] / 1e4),
                "multiparty_absorb_amt": round(absorb["amt"] / 1e4),
                "shortparty_cnt": bear_cnt,
                "shortparty_amt": round(bear_amt),
                "shortparty_short_amt": round(short["amt"] / 1e4),
                "shortparty_dump_amt": round(dump["amt"] / 1e4),
                "net_amt": round(bull_amt - bear_amt),
                "up_bull_pct": up_bull_pct,
                "dn_bull_pct": dn_bull_pct,
                "push_pct": push_pct,
                "dominant": "multiparty" if bull_amt - bear_amt > 5000 else ("shortparty" if bear_amt - bull_amt > 5000 else "balanced"),
            }

            up_rhythms = []
            down_rhythms = []

            for rhythm in sorted(strategy_data.keys()):
                rd = strategy_data[rhythm][cat]

                # 涨段
                p = rd.get("追涨", {"cnt": 0, "amt": 0})
                du = rd.get("高抛", {"cnt": 0, "amt": 0})
                if p["cnt"] + du["cnt"] > 0:
                    total_amt_wan = (p["amt"] + du["amt"]) / 1e4
                    if total_amt_wan >= 500:
                        push_ratio = round(p["cnt"] / max(p["cnt"] + du["cnt"], 1) * 100)
                        up_rhythms.append({
                            "rhythm": rhythm,
                            "push_cnt": p["cnt"], "push_amt": round(p["amt"] / 1e4),
                            "dump_cnt": du["cnt"], "dump_amt": round(du["amt"] / 1e4),
                            "net": round((p["amt"] - du["amt"]) / 1e4),
                            "push_ratio": push_ratio,
                            "verdict": "多方主导" if (p["amt"] - du["amt"]) / 1e4 > 1000 else ("空方出货" if (p["amt"] - du["amt"]) / 1e4 < -1000 else "势均力敌"),
                        })

                # 跌段
                a = rd.get("低吸", {"cnt": 0, "amt": 0})
                s = rd.get("杀跌", {"cnt": 0, "amt": 0})
                if a["cnt"] + s["cnt"] > 0:
                    total_amt_wan = (a["amt"] + s["amt"]) / 1e4
                    if total_amt_wan >= 500:
                        absorb_ratio = round(a["cnt"] / max(a["cnt"] + s["cnt"], 1) * 100)
                        down_rhythms.append({
                            "rhythm": rhythm,
                            "absorb_cnt": a["cnt"], "absorb_amt": round(a["amt"] / 1e4),
                            "short_cnt": s["cnt"], "short_amt": round(s["amt"] / 1e4),
                            "net": round((a["amt"] - s["amt"]) / 1e4),
                            "absorb_ratio": absorb_ratio,
                            "verdict": "多方局部占优" if (a["amt"] - s["amt"]) / 1e4 > 2000 else ("空方主导" if (a["amt"] - s["amt"]) / 1e4 < -2000 else "均势"),
                        })

            result[cat] = {
                "summary": summary,
                "up_rhythms": up_rhythms,
                "down_rhythms": down_rhythms,
            }

        return result

    def _build_dim7_dict(self, strategy_data, total_strategy):
        """将 dim7 叙事报告提取为结构化摘要"""
        cat_names = [c[0] for c in ORDER_TYPES]
        narratives = {}
        cross_summary = []

        for cat in cat_names:
            d = total_strategy[cat]
            push = d.get("追涨", {"cnt": 0, "amt": 0})
            absorb = d.get("低吸", {"cnt": 0, "amt": 0})
            dump = d.get("高抛", {"cnt": 0, "amt": 0})
            short = d.get("杀跌", {"cnt": 0, "amt": 0})

            bull_amt = (push["amt"] + absorb["amt"]) / 1e4
            bear_amt = (dump["amt"] + short["amt"]) / 1e4
            net = bull_amt - bear_amt
            push_pct = push["amt"] / max(push["amt"] + absorb["amt"], 1) * 100
            absorb_pct = absorb["amt"] / max(push["amt"] + absorb["amt"], 1) * 100

            # 涨段博弈判定
            up_verdicts = defaultdict(lambda: {"push_amt": 0, "dump_amt": 0})
            dn_verdicts = defaultdict(lambda: {"absorb_amt": 0, "short_amt": 0})
            for rhythm in strategy_data:
                rd = strategy_data[rhythm][cat]
                p = rd.get("追涨", {"cnt": 0, "amt": 0})
                du = rd.get("高抛", {"cnt": 0, "amt": 0})
                if p["cnt"] + du["cnt"] > 0:
                    up_verdicts[rhythm] = {"push_amt": p["amt"] / 1e4, "dump_amt": du["amt"] / 1e4}
                a = rd.get("低吸", {"cnt": 0, "amt": 0})
                s = rd.get("杀跌", {"cnt": 0, "amt": 0})
                if a["cnt"] + s["cnt"] > 0:
                    dn_verdicts[rhythm] = {"absorb_amt": a["amt"] / 1e4, "short_amt": s["amt"] / 1e4}

            # 下跌加速退缩判断
            accel = strategy_data.get("加速跌", {}).get(cat, {})
            s_acc = accel.get("低吸", {"cnt": 0, "amt": 0})
            sh_acc = accel.get("杀跌", {"cnt": 0, "amt": 0})
            slow_d = strategy_data.get("缓跌", {}).get(cat, {})
            s_slow = slow_d.get("低吸", {"cnt": 0, "amt": 0})
            sh_slow = slow_d.get("杀跌", {"cnt": 0, "amt": 0})

            accel_ratio = s_acc["amt"] / max(sh_acc["amt"], 1) if sh_acc["amt"] > 0 else 999
            slow_ratio = s_slow["amt"] / max(sh_slow["amt"], 1) if sh_slow["amt"] > 0 else 999
            retreat = accel_ratio < slow_ratio * 0.7 and sh_acc["amt"] > 0

            # 简单标签
            if net > 5000:
                tag = "多方主导"
            elif net < -5000:
                tag = "空方主导"
            else:
                tag = "多空均衡" if abs(net) < 2000 else ("空方略优" if net < 0 else "多方略优")

            if retreat:
                tag += " + 加速跌退缩"

            narratives[cat] = {
                "bull_amt": round(bull_amt),
                "bear_amt": round(bear_amt),
                "net": round(net),
                "tag": tag,
                "push_pct": round(push_pct),
                "absorb_pct": round(absorb_pct),
                "retreat_in_accel": retreat,
                "up_bull_pct": round(push["cnt"] / max(push["cnt"] + dump["cnt"], 1) * 100),
                "dn_bull_pct": round(absorb["cnt"] / max(absorb["cnt"] + short["cnt"], 1) * 100),
                "up_verdicts": {k: v for k, v in sorted(up_verdicts.items())},
                "dn_verdicts": {k: v for k, v in sorted(dn_verdicts.items())},
            }

            cross_summary.append({
                "cat": cat,
                "bull_amt": round(bull_amt),
                "bear_amt": round(bear_amt),
                "net": round(net),
                "tag": tag,
                "up_bull_pct": round(push["cnt"] / max(push["cnt"] + dump["cnt"], 1) * 100),
                "dn_bull_pct": round(absorb["cnt"] / max(absorb["cnt"] + short["cnt"], 1) * 100),
                "push_pct": round(push_pct),
            })

        return {"categories": narratives, "cross_summary": cross_summary}

    def to_dict(self):
        """供 Dashboard 调用的唯一入口，返回全维度结构化数据（无 print）"""
        strategy_data, total_strategy = self._load_strategy_data()
        rhythm_ticks, rhythm_dur, rhythm_order = self._load_dim1_data()
        consistency, d2_rhythm_order = self._load_dim2_data()

        return {
            "stock": self.stock,
            "date": self.date,
            "dim1": self._build_dim1_dict(rhythm_ticks, rhythm_dur, rhythm_order),
            "dim2": self._build_dim2_dict(consistency, d2_rhythm_order),
            "dim6": self._build_dim6_dict(strategy_data, total_strategy),
            "dim7": self._build_dim7_dict(strategy_data, total_strategy),
        }


# ============================================================
# 主入口
# ============================================================

def main():
    import argparse
    p = argparse.ArgumentParser(description="订单行为分析工具")
    p.add_argument("--stock", default="HK.00700")
    p.add_argument("--date", default="2026-06-18")
    p.add_argument("--dim", type=int, nargs="*",
                   help="只运行指定维度（1-7），默认全部")
    args = p.parse_args()

    analyzer = OrderBehaviorAnalyzer(stock=args.stock, date=args.date)

    dims = args.dim or [1, 2, 3, 4, 5, 6, 7]

    try:
        if 1 in dims:
            analyzer.dim1_rhythm_order_profile()
        if 2 in dims:
            analyzer.dim2_direction_consistency()
        if 3 in dims:
            analyzer.dim3_heatmaps()
        if 4 in dims:
            analyzer.dim4_price_impact()
        if 5 in dims:
            analyzer.dim5_segment_snapshot()
        if 6 in dims:
            analyzer.dim6_strategy_analysis()
        if 7 in dims:
            analyzer.dim7_narrative_report()
    finally:
        analyzer.close()


if __name__ == "__main__":
    main()
