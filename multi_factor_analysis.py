#!/usr/bin/env python3
"""
多因子条件概率分析：RSI + 价格位置 + MA + 尾盘大单资金流

假设：当股价处于低位、RSI 超卖、但大资金（特大单/大单）在下午尾盘突然
净买入时，未来几天股价可能大幅上涨。

分析框架：
1. 技术指标：RSI(14)、MA(5/10/20/60)、52周位置、布林带位置
2. 日内资金流分段：早盘(09:30-12:00)、午盘(13:00-16:00)、尾盘(14:30-16:00)
   按四档阈值聚合
3. 构建复合信号，计算条件概率（信号出现后 N 日收益分布 vs 无条件分布）
"""
import sys
import numpy as np
from collections import defaultdict
from datetime import date, time
from scipy.stats import spearmanr, mannwhitneyu

from db import get_conn

STOCK = sys.argv[1] if len(sys.argv) > 1 else "HK.00700"

# ── 阈值 ──
XL_VOL = 25_000
LG_VOL = 7_000
MD_VOL = 300

# ── RSI 参数 ──
RSI_PERIOD = 14
RSI_OVERSOLD = 35  # RSI 低于此值视为超卖

# ── 尾盘定义 ──
AFTERNOON_START = time(13, 0)    # 午盘开盘
LATE_SESSION   = time(14, 30)    # 尾盘
MARKET_CLOSE   = time(16, 0)
MORNING_START  = time(9, 30)
MORNING_END    = time(12, 0)


# ════════════════════════════════════════════════════
# 1. 数据加载
# ════════════════════════════════════════════════════

def load_daily_quote(stock):
    """加载全部日线 + 52周高低"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT trade_date, last_price, volume, turnover,
                      high_52w, low_52w
               FROM daily_quote WHERE stock_code=%s
               ORDER BY trade_date""",
            (stock,),
        )
        return cur.fetchall()


def load_tick_flow_sessions(stock):
    """
    从 tick_data 按日 + 时段聚合四档资金流
    返回 dict: date -> {
        all: {xl_in, xl_out, lg_in, lg_out, md_in, md_out, sm_in, sm_out},
        morning: {...}, afternoon: {...}, late: {...}
    }
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT DATE(tick_time) AS td,
                      CASE
                        WHEN tick_time::time >= %(late)s  THEN 'late'
                        WHEN tick_time::time >= %(aft)s   THEN 'afternoon'
                        WHEN tick_time::time <= %(morn_end)s THEN 'morning'
                        ELSE 'midday'
                      END AS session,
                      CASE
                        WHEN volume >= %(xl)s THEN 'xl'
                        WHEN volume >= %(lg)s THEN 'lg'
                        WHEN volume >= %(md)s THEN 'md'
                        ELSE 'sm'
                      END AS tier,
                      ticker_direction,
                      SUM(turnover) AS total_turnover
               FROM tick_data
               WHERE stock_code=%(code)s
                 AND ticker_direction IN ('BUY','SELL')
               GROUP BY td, session, tier, ticker_direction
               ORDER BY td, session, tier, ticker_direction""",
               {"code": stock, "xl": XL_VOL, "lg": LG_VOL, "md": MD_VOL,
                "late": LATE_SESSION, "aft": AFTERNOON_START, "morn_end": MORNING_END},
        )
        rows = cur.fetchall()

    result = {}
    fields = ["xl_in", "xl_out", "lg_in", "lg_out", "md_in", "md_out", "sm_in", "sm_out"]
    for td, session, tier, direction, turnover in rows:
        if td not in result:
            result[td] = {"all": dict.fromkeys(fields, 0.0),
                          "morning": dict.fromkeys(fields, 0.0),
                          "afternoon": dict.fromkeys(fields, 0.0),
                          "late": dict.fromkeys(fields, 0.0)}
        key = f"{tier}_{'in' if direction == 'BUY' else 'out'}"

        # all（汇总）
        result[td]["all"][key] += float(turnover)

        # 按时段
        if session in ("morning", "afternoon", "late"):
            result[td][session][key] += float(turnover)
        elif session == "midday":
            # 午休时段（12:00-13:00）归入 afternoon
            result[td]["afternoon"][key] += float(turnover)

    return result


# ════════════════════════════════════════════════════
# 2. 技术指标计算
# ════════════════════════════════════════════════════

def compute_indicators(price_rows):
    """在日线序列上计算全部技术指标"""
    dates = np.array([r[0] for r in price_rows])
    closes = np.array([float(r[1]) for r in price_rows], dtype=float)
    high52 = np.array([float(r[4]) if r[4] else np.nan for r in price_rows], dtype=float)
    low52 = np.array([float(r[5]) if r[5] else np.nan for r in price_rows], dtype=float)
    n = len(closes)

    # ── RSI(14) ──
    rsi = np.full(n, np.nan)
    if n > RSI_PERIOD:
        diffs = np.diff(closes)
        gains = np.where(diffs > 0, diffs, 0)
        losses = np.where(diffs < 0, -diffs, 0)
        avg_gain = np.full(n, np.nan)
        avg_loss = np.full(n, np.nan)
        # 初始 Wilder 平滑
        avg_gain[RSI_PERIOD] = np.mean(gains[:RSI_PERIOD])
        avg_loss[RSI_PERIOD] = np.mean(losses[:RSI_PERIOD])
        for i in range(RSI_PERIOD + 1, n):
            avg_gain[i] = (avg_gain[i - 1] * (RSI_PERIOD - 1) + gains[i - 1]) / RSI_PERIOD
            avg_loss[i] = (avg_loss[i - 1] * (RSI_PERIOD - 1) + losses[i - 1]) / RSI_PERIOD
        for i in range(RSI_PERIOD, n):
            if avg_loss[i] == 0:
                rsi[i] = 100.0
            else:
                rs = avg_gain[i] / avg_loss[i]
                rsi[i] = 100.0 - 100.0 / (1.0 + rs)

    # ── MA ──
    def ma(window):
        result = np.full(n, np.nan)
        for i in range(window - 1, n):
            result[i] = np.mean(closes[i - window + 1 : i + 1])
        return result

    ma5  = ma(5)
    ma10 = ma(10)
    ma20 = ma(20)
    ma60 = ma(60)

    # ── 价格位置 ──
    # 52周位置
    price_52w_pos = np.where(
        (high52 - low52) > 0,
        (closes - low52) / (high52 - low52),
        np.nan,
    )

    # vs MA20 偏离
    vs_ma20_pct = np.where(~np.isnan(ma20), (closes - ma20) / ma20 * 100, np.nan)

    # N日最低点位置（20日内价格处于什么分位）
    pos_20d = np.full(n, np.nan)
    for i in range(20, n):
        window = closes[i - 19 : i + 1]
        lo, hi = np.min(window), np.max(window)
        if hi > lo:
            pos_20d[i] = (closes[i] - lo) / (hi - lo)
        else:
            pos_20d[i] = 0.5

    # ── 布林带 ──
    bb_mid = ma20
    bb_upper = np.full(n, np.nan)
    bb_lower = np.full(n, np.nan)
    bb_width = np.full(n, np.nan)
    bb_pos = np.full(n, np.nan)  # 0=下轨, 0.5=中轨, 1=上轨
    for i in range(19, n):
        std = np.std(closes[i - 19 : i + 1], ddof=1)
        bb_upper[i] = bb_mid[i] + 2 * std
        bb_lower[i] = bb_mid[i] - 2 * std
        if bb_upper[i] > bb_lower[i]:
            bb_pos[i] = (closes[i] - bb_lower[i]) / (bb_upper[i] - bb_lower[i])
            bb_width[i] = (bb_upper[i] - bb_lower[i]) / bb_mid[i] * 100

    # ── 近期涨跌幅 ──
    ret_1d  = np.full(n, np.nan)
    ret_5d  = np.full(n, np.nan)
    ret_10d = np.full(n, np.nan)
    ret_20d = np.full(n, np.nan)
    for i in range(1, n):
        ret_1d[i] = closes[i] / closes[i - 1] - 1
    for i in range(5, n):
        ret_5d[i] = closes[i] / closes[i - 5] - 1
    for i in range(10, n):
        ret_10d[i] = closes[i] / closes[i - 10] - 1
    for i in range(20, n):
        ret_20d[i] = closes[i] / closes[i - 20] - 1

    # ── 成交量变化 ──
    vol_5d = np.full(n, np.nan)
    for i in range(5, n):
        avg_vol = np.mean([float(r[2]) for r in price_rows[i - 4 : i + 1]])
        if avg_vol > 0:
            vol_5d[i] = float(price_rows[i][2]) / avg_vol

    return {
        "dates": dates, "closes": closes,
        "rsi": rsi, "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
        "price_52w_pos": price_52w_pos, "vs_ma20_pct": vs_ma20_pct,
        "pos_20d": pos_20d,
        "bb_lower": bb_lower, "bb_upper": bb_upper, "bb_mid": bb_mid,
        "bb_width": bb_width, "bb_pos": bb_pos,
        "ret_1d": ret_1d, "ret_5d": ret_5d, "ret_10d": ret_10d, "ret_20d": ret_20d,
        "vol_5d": vol_5d,
    }


# ════════════════════════════════════════════════════
# 3. 信号定义与条件概率
# ════════════════════════════════════════════════════

def build_combined_dataset(ind, flow):
    """
    对齐技术指标和资金流数据，构建日级数据集
    返回 list of dict，每天一条
    """
    date_to_idx = {d: i for i, d in enumerate(ind["dates"])}
    rows = []

    for td_str in sorted(date_to_idx.keys()):
        td_date = td_str  # already date object
        i = date_to_idx[td_str]

        # 基础指标
        row = {
            "date": td_str,
            "idx": i,
            "close": ind["closes"][i],
        }
        for key in ["rsi", "ma5", "ma10", "ma20", "ma60",
                     "price_52w_pos", "vs_ma20_pct", "pos_20d",
                     "bb_lower", "bb_upper", "bb_mid", "bb_width", "bb_pos",
                     "ret_1d", "ret_5d", "ret_10d", "ret_20d", "vol_5d"]:
            row[key] = ind[key][i]

        # MA 多头/空头排列
        if not np.isnan(ind["ma5"][i]) and not np.isnan(ind["ma10"][i]) and \
           not np.isnan(ind["ma20"][i]) and not np.isnan(ind["ma60"][i]):
            ma_vals = [ind["ma5"][i], ind["ma10"][i], ind["ma20"][i], ind["ma60"][i]]
            row["ma_bullish"] = all(ma_vals[j] > ma_vals[j + 1] for j in range(len(ma_vals) - 1))
            row["ma_bearish"] = all(ma_vals[j] < ma_vals[j + 1] for j in range(len(ma_vals) - 1))
        else:
            row["ma_bullish"] = False
            row["ma_bearish"] = False

        # 资金流
        if td_str in flow:
            f = flow[td_str]
            for session in ["all", "morning", "afternoon", "late"]:
                fs = f[session]
                total_in = fs["xl_in"] + fs["lg_in"] + fs["md_in"] + fs["sm_in"]
                total_out = fs["xl_out"] + fs["lg_out"] + fs["md_out"] + fs["sm_out"]
                total_all = total_in + total_out

                # 各档净买入（亿元）
                row[f"{session}_xl_net"]  = (fs["xl_in"] - fs["xl_out"]) / 1e8
                row[f"{session}_lg_net"]  = (fs["lg_in"] - fs["lg_out"]) / 1e8
                row[f"{session}_md_net"]  = (fs["md_in"] - fs["md_out"]) / 1e8
                row[f"{session}_sm_net"]  = (fs["sm_in"] - fs["sm_out"]) / 1e8

                # 大资金合计（特大+大）
                row[f"{session}_big_net"] = row[f"{session}_xl_net"] + row[f"{session}_lg_net"]
                row[f"{session}_small_net"] = row[f"{session}_md_net"] + row[f"{session}_sm_net"]

                # 各档主动买占比
                if total_all > 0:
                    row[f"{session}_xl_buy_ratio"]  = fs["xl_in"] / total_all
                    row[f"{session}_lg_buy_ratio"]  = fs["lg_in"] / total_all
                    row[f"{session}_big_buy_ratio"]  = (fs["xl_in"] + fs["lg_in"]) / total_all
                    row[f"{session}_total_bsr"] = total_in / total_out if total_out > 0 else np.nan
                else:
                    for k in ["xl_buy_ratio", "lg_buy_ratio", "big_buy_ratio"]:
                        row[f"{session}_{k}"] = np.nan
                    row[f"{session}_total_bsr"] = np.nan

                # 成交额（亿）
                row[f"{session}_turnover"] = total_all / 1e8

            # 尾盘特性
            row["late_vs_morning_big"] = (
                row["late_big_net"] - row["morning_big_net"]
                if not np.isnan(row.get("late_big_net", np.nan)) else np.nan
            )
        else:
            # 无 tick 数据的日子
            for session in ["all", "morning", "afternoon", "late"]:
                for field in ["xl_net", "lg_net", "md_net", "sm_net", "big_net", "small_net",
                              "xl_buy_ratio", "lg_buy_ratio", "big_buy_ratio",
                              "total_bsr", "turnover"]:
                    row[f"{session}_{field}"] = np.nan
            row["late_vs_morning_big"] = np.nan

        # 未来 N 日收益（收盘到收盘）
        for horizon in [1, 3, 5, 10, 20]:
            tgt = i + horizon
            if tgt < len(ind["closes"]):
                row[f"fwd_{horizon}d"] = ind["closes"][tgt] / ind["closes"][i] - 1
            else:
                row[f"fwd_{horizon}d"] = np.nan

        # 未来 N 日最高涨幅
        for horizon in [3, 5, 10]:
            end = min(i + horizon + 1, len(ind["closes"]))
            if end > i + 1:
                row[f"fwd_max_{horizon}d"] = np.max(ind["closes"][i + 1 : end]) / ind["closes"][i] - 1
            else:
                row[f"fwd_max_{horizon}d"] = np.nan

        rows.append(row)

    return rows


# ════════════════════════════════════════════════════
# 4. 条件概率分析
# ════════════════════════════════════════════════════

def define_signals(rows):
    """
    定义多组复合信号，每个信号是一个布尔 mask + 描述
    返回 list of (name, mask, description)
    """
    n = len(rows)
    signals = []

    # 筛选：有 tick 数据且有足够的前瞻收益
    has_flow = np.array([not np.isnan(r.get("all_big_net", np.nan)) for r in rows], dtype=bool)

    # 辅助数组
    rsi       = np.array([r["rsi"] for r in rows], dtype=float)
    pos_20d   = np.array([r["pos_20d"] for r in rows], dtype=float)
    vs_ma20   = np.array([r["vs_ma20_pct"] for r in rows], dtype=float)
    pos_52w   = np.array([r["price_52w_pos"] for r in rows], dtype=float)
    bb_pos    = np.array([r["bb_pos"] for r in rows], dtype=float)
    ret_5d    = np.array([r["ret_5d"] for r in rows], dtype=float)
    ret_10d   = np.array([r["ret_10d"] for r in rows], dtype=float)
    ret_20d   = np.array([r["ret_20d"] for r in rows], dtype=float)

    all_big_net     = np.array([r.get("all_big_net", np.nan) for r in rows], dtype=float)
    late_big_net    = np.array([r.get("late_big_net", np.nan) for r in rows], dtype=float)
    late_xl_net     = np.array([r.get("late_xl_net", np.nan) for r in rows], dtype=float)
    afternoon_big   = np.array([r.get("afternoon_big_net", np.nan) for r in rows], dtype=float)
    morning_big     = np.array([r.get("morning_big_net", np.nan) for r in rows], dtype=float)
    late_vs_morning = np.array([r.get("late_vs_morning_big", np.nan) for r in rows], dtype=float)
    all_big_ratio   = np.array([r.get("all_big_buy_ratio", np.nan) for r in rows], dtype=float)
    late_big_ratio  = np.array([r.get("late_big_buy_ratio", np.nan) for r in rows], dtype=float)

    # ── 原子信号 ──
    oversold    = ~np.isnan(rsi) & (rsi <= RSI_OVERSOLD)
    low_pos     = ~np.isnan(pos_20d) & (pos_20d <= 0.25)
    below_ma20  = ~np.isnan(vs_ma20) & (vs_ma20 < -3)
    low_52w     = ~np.isnan(pos_52w) & (pos_52w <= 0.25)
    bb_low      = ~np.isnan(bb_pos) & (bb_pos <= 0.15)
    fallen_5d   = ~np.isnan(ret_5d) & (ret_5d < -0.03)   # 近5日跌超3%
    fallen_10d  = ~np.isnan(ret_10d) & (ret_10d < -0.05) # 近10日跌超5%

    big_buying      = has_flow & (all_big_net > 0)
    late_big_buying = has_flow & (late_big_net > 0) & (~np.isnan(late_big_net))
    late_xl_buying  = has_flow & (late_xl_net > 0) & (~np.isnan(late_xl_net))
    late_dominant   = has_flow & (late_vs_morning > 0) & (~np.isnan(late_vs_morning))
    late_xl_strong  = has_flow & (late_xl_net > 0) & (late_xl_net > np.nanmedian(late_xl_net[has_flow]))

    # ── 单因子信号（baseline） ──
    signals.append(("仅RSI≤35",      has_flow & oversold,        "RSI 超卖"))
    signals.append(("仅20日低位",    has_flow & low_pos,         "价格处于20日低位25%以下"))
    signals.append(("仅MA20下方3%",  has_flow & below_ma20,      "低于MA20超过3%"))
    signals.append(("仅52周低位",    has_flow & low_52w,         "处于52周低位25%以下"))
    signals.append(("仅布林下轨",    has_flow & bb_low,          "接近布林下轨"))
    signals.append(("仅近5日跌>3%",  has_flow & fallen_5d,       "近5日跌幅超3%"))
    signals.append(("仅尾盘特大单净买", has_flow & late_xl_buying, "尾盘特大单净买入"))
    signals.append(("仅尾盘大单净买",   has_flow & late_big_buying, "尾盘大资金净买入"))
    signals.append(("仅尾盘强于早盘",  has_flow & late_dominant,  "尾盘大资金强于早盘"))

    # ── 二因子组合 ──
    signals.append(("低位+尾盘大单",   has_flow & low_pos & late_big_buying,  "20日低位 + 尾盘大资金净买"))
    signals.append(("低位+尾盘特大单", has_flow & low_pos & late_xl_buying,   "20日低位 + 尾盘特大单净买"))
    signals.append(("RSI低+尾盘大单",  has_flow & oversold & late_big_buying, "RSI≤35 + 尾盘大资金净买"))
    signals.append(("RSI低+尾盘特大单",has_flow & oversold & late_xl_buying,  "RSI≤35 + 尾盘特大单净买"))
    signals.append(("超跌+尾盘大单",   has_flow & fallen_5d & late_big_buying, "近5日跌>3% + 尾盘大资金净买"))
    signals.append(("低位+尾盘强于早盘", has_flow & low_pos & late_dominant,  "20日低位 + 尾盘强于早盘"))
    signals.append(("RSI低+尾盘强于早盘", has_flow & oversold & late_dominant,"RSI≤35 + 尾盘强于早盘"))

    # ── 三因子组合（核心假设） ──
    signals.append(("RSI低+低位+尾盘大单",   has_flow & oversold & low_pos & late_big_buying,
                    "RSI≤35 + 20日低位 + 尾盘大资金净买"))
    signals.append(("RSI低+低位+尾盘特大单", has_flow & oversold & low_pos & late_xl_buying,
                    "RSI≤35 + 20日低位 + 尾盘特大单净买"))
    signals.append(("超跌+低位+尾盘大单",    has_flow & fallen_5d & low_pos & late_big_buying,
                    "近5日跌>3% + 20日低位 + 尾盘大资金净买"))

    # ── 更多阈值变体 ──
    rsi_40_mask    = ~np.isnan(rsi) & (rsi <= 40)
    pos_30_mask    = ~np.isnan(pos_20d) & (pos_20d <= 0.30)
    signals.append(("RSI≤40+低位30%+尾盘大单", has_flow & rsi_40_mask & pos_30_mask & late_big_buying,
                    "RSI≤40 + 20日低位30% + 尾盘大资金净买"))
    signals.append(("RSI≤40+低位30%+尾盘特大单", has_flow & rsi_40_mask & pos_30_mask & late_xl_buying,
                    "RSI≤40 + 20日低位30% + 尾盘特大单净买"))

    # ── 连续多日尾盘大单（趋势信号） ──
    # 前1天也是尾盘大单净买
    late_big_prev = np.roll(late_big_net > 0, 1)
    late_big_prev[0] = False
    signals.append(("连续2日尾盘大单净买", has_flow & late_big_buying & late_big_prev,
                    "连续2天尾盘大资金净买入"))
    signals.append(("低位+连续2日尾盘大单", has_flow & low_pos & late_big_buying & late_big_prev,
                    "20日低位 + 连续2天尾盘大资金净买"))

    return signals


def analyze_signals(rows, signals):
    """对每个信号计算条件概率统计"""
    n = len(rows)
    horizons = [1, 3, 5, 10, 20]
    max_horizons = [3, 5, 10]

    # 无条件分布（作为 baseline，只用有 tick 数据的日子）
    has_flow = np.array([not np.isnan(r.get("all_big_net", np.nan)) for r in rows], dtype=bool)

    # 构建收益数组
    fwd = {}
    for h in horizons:
        fwd[h] = np.array([r[f"fwd_{h}d"] for r in rows], dtype=float)
    fwd_max = {}
    for h in max_horizons:
        fwd_max[h] = np.array([r[f"fwd_max_{h}d"] for r in rows], dtype=float)

    def stats(arr_mask):
        """返回 (count, mean, median, std, win_rate, best, worst)"""
        vals = np.array([r[f"fwd_{h}d"] for r, m in zip(rows, arr_mask) if m and not np.isnan(r.get(f"fwd_{h}d", np.nan))])
        # placeholder
        pass

    # 先算无条件分布
    baseline = {}
    for h in horizons:
        vals = fwd[h][has_flow]
        valid = vals[~np.isnan(vals)]
        if len(valid) > 0:
            baseline[h] = {
                "n": len(valid),
                "mean": float(np.mean(valid)),
                "median": float(np.median(valid)),
                "std": float(np.std(valid)),
                "win_rate": float(np.mean(valid > 0)),
                "best": float(np.max(valid)),
                "worst": float(np.min(valid)),
            }

    baseline_max = {}
    for h in max_horizons:
        vals = fwd_max[h][has_flow]
        valid = vals[~np.isnan(vals)]
        if len(valid) > 0:
            baseline_max[h] = {
                "n": len(valid),
                "mean": float(np.mean(valid)),
                "median": float(np.median(valid)),
            }

    # 各信号统计
    results = []
    for name, mask, desc in signals:
        active = mask.sum()
        if active < 2:
            continue

        sig_results = {"name": name, "desc": desc, "count": int(active)}
        for h in horizons:
            vals = fwd[h][mask]
            valid = vals[~np.isnan(vals)]
            if len(valid) < 2:
                sig_results[f"h{h}"] = None
                continue
            sig_results[f"h{h}"] = {
                "n": len(valid),
                "mean": float(np.mean(valid)),
                "median": float(np.median(valid)),
                "std": float(np.std(valid)),
                "win_rate": float(np.mean(valid > 0)),
                "best": float(np.max(valid)),
                "worst": float(np.min(valid)),
            }

        # 最大涨幅
        for h in max_horizons:
            vals = fwd_max[h][mask]
            valid = vals[~np.isnan(vals)]
            if len(valid) >= 2:
                sig_results[f"max{h}"] = {
                    "n": len(valid), "mean": float(np.mean(valid)), "median": float(np.median(valid))
                }

        results.append(sig_results)

    return results, baseline, baseline_max


def print_analysis(results, baseline, rows):
    """打印条件概率分析结果"""
    has_flow = [r for r in rows if not np.isnan(r.get("all_big_net", np.nan))]
    n_flow = len(has_flow)

    print(f"\n{'='*120}")
    print(f"  多因子条件概率分析 — {STOCK}")
    print(f"  有资金流数据: {n_flow} 天 (共 {len(rows)} 天日线)")
    print(f"  基准: RSI≤{RSI_OVERSOLD}=超卖, 20日低位≤25%, 尾盘=14:30-16:00 大单≥{LG_VOL}股/特大单≥{XL_VOL}股")
    print(f"{'='*120}")

    # ── 无条件分布 ──
    print(f"\n  ── 无条件分布（所有有资金流的日子, n={baseline[1]['n']}）──")
    hdr = f"  {'':>5s}"
    for h in [1, 3, 5, 10, 20]:
        hdr += f"  {'H' + str(h) + 'D均值':>10s}  {'胜率':>6s}"
    print(hdr)
    row_str = f"  {'基准':>5s}"
    for h in [1, 3, 5, 10, 20]:
        b = baseline[h]
        row_str += f"  {b['mean']*100:+.2f}%  {b['win_rate']:.0%} "
    print(row_str)
    print()

    # ── 信号排序（按 10D 超额收益） ──
    sorted_results = sorted(
        results,
        key=lambda r: (r.get("h10", {}).get("mean", -999) if r.get("h10") else -999)
                     - (baseline[10]["mean"] if r.get("h10") else 0),
        reverse=True,
    )

    print(f"  {'信号':40s}  {'次数':>4s}  ", end="")
    for h in [1, 3, 5, 10, 20]:
        print(f"  {'H'+str(h)+'D':>8s}", end="  ")
    print(f"  {'H5_max':>8s}  {'H10_max':>8s}")
    print(f"  {'─'*40}  {'─'*4}  {'─'*10}  {'─'*10}  {'─'*10}  {'─'*10}  {'─'*10}  {'─'*10}  {'─'*10}")

    for sr in sorted_results:
        name = sr["name"]
        cnt = sr["count"]
        line = f"  {name:40s}  {cnt:4d}  "

        for h in [1, 3, 5, 10, 20]:
            hs = sr.get(f"h{h}")
            if hs:
                excess = hs["mean"] - baseline[h]["mean"]
                line += f"  {hs['mean']*100:+.2f}%"
                # 标注超额收益
                if abs(excess) > 0.01:
                    line += f"(Δ{excess*100:+.1f}%) "
                else:
                    line += "       "
            else:
                line += "      N/A    "

        for h in [5, 10]:
            mx = sr.get(f"max{h}")
            if mx:
                line += f"  {mx['mean']*100:+.2f}%      "
            else:
                line += "      N/A      "

        print(line)

    # ── 详细事件日志 ──
    print(f"\n{'='*120}")
    print(f"  事件详情（每个信号的触发日期 + 后续走势）")
    print(f"{'='*120}")

    # 构建信号 mask（复用前面的逻辑）
    from collections import OrderedDict
    rsi       = np.array([r["rsi"] for r in rows], dtype=float)
    pos_20d   = np.array([r["pos_20d"] for r in rows], dtype=float)
    ret_5d    = np.array([r["ret_5d"] for r in rows], dtype=float)

    late_big_net  = np.array([r.get("late_big_net", np.nan) for r in rows], dtype=float)
    late_xl_net   = np.array([r.get("late_xl_net", np.nan) for r in rows], dtype=float)
    has_flow_arr  = np.array([not np.isnan(r.get("all_big_net", np.nan)) for r in rows], dtype=bool)

    key_signals = OrderedDict([
        ("RSI≤35 + 20日低位 + 尾盘大单净买",
         has_flow_arr & (~np.isnan(rsi) & (rsi <= RSI_OVERSOLD)) & (~np.isnan(pos_20d) & (pos_20d <= 0.25)) & (late_big_net > 0)),
        ("20日低位 + 尾盘特大单净买",
         has_flow_arr & (~np.isnan(pos_20d) & (pos_20d <= 0.25)) & (late_xl_net > 0)),
        ("近5日跌>3% + 尾盘大单净买",
         has_flow_arr & (~np.isnan(ret_5d) & (ret_5d < -0.03)) & (late_big_net > 0)),
        ("20日低位 + 尾盘大单净买",
         has_flow_arr & (~np.isnan(pos_20d) & (pos_20d <= 0.25)) & (late_big_net > 0)),
    ])

    for sig_name, mask in key_signals.items():
        indices = np.where(mask)[0]
        if len(indices) == 0:
            print(f"\n  [{sig_name}] 无触发")
            continue
        print(f"\n  [{sig_name}] 触发 {len(indices)} 次:")
        print(f"  {'日期':>12s}  {'收盘':>8s}  {'RSI':>5s}  {'20d位':>5s}  "
              f"{'5d%':>7s}  {'尾盘大单净':>10s}  {'尾盘特大净':>10s}  "
              f"{'1D':>7s}  {'3D':>7s}  {'5D':>7s}  {'10D':>7s}  {'20D':>7s}")
        for idx in indices:
            r = rows[idx]
            date_str = str(r["date"])
            to_pct = lambda v: f"{v*100:+.2f}%" if not np.isnan(v) else "   N/A"
            print(f"  {date_str:>12s}  {r['close']:8.2f}  {r['rsi']:5.1f}  {r['pos_20d']:.2f}  "
                  f"{to_pct(r['ret_5d']):>7s}  {r.get('late_big_net',0)*100:+.2f}M     "
                  f"{r.get('late_xl_net',0)*100:+.2f}M     "
                  f"{to_pct(r.get('fwd_1d',np.nan)):>7s}  {to_pct(r.get('fwd_3d',np.nan)):>7s}  "
                  f"{to_pct(r.get('fwd_5d',np.nan)):>7s}  {to_pct(r.get('fwd_10d',np.nan)):>7s}  "
                  f"{to_pct(r.get('fwd_20d',np.nan)):>7s}")

    # ── 尾盘大单特征汇总 ──
    print(f"\n{'='*120}")
    print(f"  尾盘大单特征分布（39天）")
    print(f"{'='*120}")
    flow_days = [r for r in rows if not np.isnan(r.get("all_big_net", np.nan))]
    late_big_arr = np.array([r.get("late_big_net", 0) for r in flow_days])
    late_xl_arr  = np.array([r.get("late_xl_net", 0) for r in flow_days])

    print(f"  尾盘大资金净买入: 均值 {np.mean(late_big_arr)*100:+.2f}M  中位 {np.median(late_big_arr)*100:+.2f}M")
    print(f"  尾盘特大单净买入: 均值 {np.mean(late_xl_arr)*100:+.2f}M  中位 {np.median(late_xl_arr)*100:+.2f}M")
    print(f"  尾盘大资金净买天数: {(late_big_arr > 0).sum()}/{len(flow_days)}")
    print(f"  尾盘特大单净买天数: {(late_xl_arr > 0).sum()}/{len(flow_days)}")


# ════════════════════════════════════════════════════
# 5. 主函数
# ════════════════════════════════════════════════════

def main():
    stock = STOCK
    print(f"\n{'='*120}")
    print(f"  RSI + 价格位置 + 尾盘大单 多因子条件概率分析 — {stock}")
    print(f"{'='*120}")

    # 1. 加载数据
    print("\n[1/4] 加载日线 + 计算技术指标...")
    price_rows = load_daily_quote(stock)
    print(f"  daily_quote: {len(price_rows)} 天")

    ind = compute_indicators(price_rows)
    n_valid_ta = int((~np.isnan(ind["rsi"])).sum())
    print(f"  技术指标有效天数: {n_valid_ta} (RSI≥14天后)")

    print("\n[2/4] 加载 tick_data 按时段聚合...")
    flow = load_tick_flow_sessions(stock)
    print(f"  资金流覆盖: {len(flow)} 天")

    print("\n[3/4] 对齐数据...")
    rows = build_combined_dataset(ind, flow)
    has_flow = sum(1 for r in rows if not np.isnan(r.get("all_big_net", np.nan)))
    print(f"  总天数: {len(rows)}  |  有资金流: {has_flow} 天")

    print("\n[4/4] 定义信号 + 条件概率分析...")
    signals = define_signals(rows)
    results, baseline, baseline_max = analyze_signals(rows, signals)

    # 输出
    print_analysis(results, baseline, rows)

    print()


if __name__ == "__main__":
    main()
