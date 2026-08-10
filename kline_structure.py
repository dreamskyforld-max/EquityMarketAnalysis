"""
K 线结构分析：识别震荡区间、突变点、支撑/阻力位，结合主力资金行为。
"""
import sys
import numpy as np
import pandas as pd
from db import get_conn

XL_VOL, LG_VOL, MD_VOL = 25000, 7000, 300

def load_full_data(stock):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT trade_date, last_price, volume, turnover,
                   open_price, high_price, low_price
            FROM daily_quote WHERE stock_code=%s ORDER BY trade_date
        """, (stock,))
        price = pd.DataFrame(cur.fetchall(),
            columns=["date","close","vol","turnover","open","high","low"])

        cur.execute("""
            SELECT DATE(tick_time) AS td,
                   COALESCE(SUM(CASE WHEN volume>=%s AND ticker_direction='BUY'  THEN turnover END),0) AS xl_in,
                   COALESCE(SUM(CASE WHEN volume>=%s AND ticker_direction='SELL' THEN turnover END),0) AS xl_out,
                   COALESCE(SUM(CASE WHEN volume>=%s AND volume<%s AND ticker_direction='BUY'  THEN turnover END),0) AS lg_in,
                   COALESCE(SUM(CASE WHEN volume>=%s AND volume<%s AND ticker_direction='SELL' THEN turnover END),0) AS lg_out,
                   COALESCE(SUM(CASE WHEN volume>=%s AND tick_time::time>='15:00' AND ticker_direction='BUY'  THEN turnover END),0) AS xl_in_late,
                   COALESCE(SUM(CASE WHEN volume>=%s AND tick_time::time>='15:00' AND ticker_direction='SELL' THEN turnover END),0) AS xl_out_late,
                   COALESCE(SUM(CASE WHEN volume>=%s AND volume<%s AND tick_time::time>='15:00' AND ticker_direction='BUY'  THEN turnover END),0) AS lg_in_late,
                   COALESCE(SUM(CASE WHEN volume>=%s AND volume<%s AND tick_time::time>='15:00' AND ticker_direction='SELL' THEN turnover END),0) AS lg_out_late,
                   COUNT(*) AS tick_cnt
            FROM tick_data WHERE stock_code=%s AND ticker_direction IN ('BUY','SELL')
            GROUP BY td ORDER BY td
        """, (XL_VOL,XL_VOL, LG_VOL,XL_VOL, LG_VOL,XL_VOL,
              XL_VOL,XL_VOL, LG_VOL,XL_VOL, LG_VOL,XL_VOL, stock))
        flow = pd.DataFrame(cur.fetchall(),
            columns=["date","xl_in","xl_out","lg_in","lg_out",
                     "xl_in_late","xl_out_late","lg_in_late","lg_out_late","tick_cnt"])

    df = price.merge(flow, on="date", how="inner")
    for c in df.columns:
        if c != "date":
            df[c] = df[c].astype(float)

    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    # 基础衍生
    df["ret"] = df["close"].pct_change()
    df["xl_net"] = df["xl_in"] - df["xl_out"]
    df["lg_net"] = df["lg_in"] - df["lg_out"]
    df["big_net"] = df["xl_net"] + df["lg_net"]
    df["xl_late_net"] = df["xl_in_late"] - df["xl_out_late"]
    df["lg_late_net"] = df["lg_in_late"] - df["lg_out_late"]
    df["big_late_net"] = df["xl_late_net"] + df["lg_late_net"]
    df["big_net_pct"] = df["big_net"] / df["turnover"].replace(0, np.nan)
    df["big_late_pct"] = df["big_late_net"] / df["turnover"].replace(0, np.nan)
    df["turnover_M"] = df["turnover"] / 1e6

    # 振幅
    df["amplitude"] = (df["high"] - df["low"]) / df["close"].shift(1)

    # 上/下影线占比
    body = abs(df["close"] - df["open"])
    upper_wick = df["high"] - df[["open","close"]].max(axis=1)
    lower_wick = df[["open","close"]].min(axis=1) - df["low"]
    total_range = df["high"] - df["low"]
    df["upper_wick_pct"] = np.where(total_range > 0, upper_wick / total_range, 0)
    df["lower_wick_pct"] = np.where(total_range > 0, lower_wick / total_range, 0)
    df["body_pct"] = np.where(total_range > 0, body / total_range, 0)

    # 缺口
    df["gap"] = df["open"] - df["close"].shift(1)
    df["gap_pct"] = df["gap"] / df["close"].shift(1)

    # RSI(14)
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # MA
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["dist_ma20"] = (df["close"] - df["ma20"]) / df["ma20"]

    # 20日位置
    roll_low = df["low"].rolling(20).min()
    roll_high = df["high"].rolling(20).max()
    df["pos_20d"] = (df["close"] - roll_low) / (roll_high - roll_low).replace(0, np.nan)

    # 前向收益
    for n in [1,3,5,10]:
        df[f"fwd_{n}d"] = df["close"].shift(-n) / df["close"] - 1

    # 近期波动率
    df["vol_5d"] = df["ret"].rolling(5).std()
    df["vol_ratio"] = df["vol"] / df["vol"].rolling(20).mean()

    return df


def detect_swings(df):
    """
    识别价格摆动高点和低点（局部极值）。
    window=3: 前后各3天内的最高/最低点。
    """
    highs, lows = [], []
    w = 3
    for i in range(w, len(df) - w):
        window = df["high"].iloc[i-w:i+w+1]
        if df["high"].iloc[i] == window.max():
            highs.append(i)
        window = df["low"].iloc[i-w:i+w+1]
        if df["low"].iloc[i] == window.min():
            lows.append(i)
    return highs, lows


def detect_sudden_drops(df, pct_threshold=-0.03):
    """
    识别突然下跌日：单日跌幅>3% 或 跳空低开>2% 或 振幅>5%且收跌。
    """
    drops = df[
        (df["ret"] < pct_threshold) |
        (df["gap_pct"] < -0.02) |
        ((df["amplitude"] > 0.05) & (df["ret"] < -0.02))
    ].copy()
    return drops


def classify_regime(df):
    """
    震荡行情细分：
      - 急跌日 (CRASH):      单日跌>3%
      - 反弹日 (BOUNCE):     急跌后2日内涨幅>2%
      - 下跌段 (DECLINE):    连续2日以上收跌
      - 上涨段 (RALLY):      连续2日以上收涨
      - 窄幅盘整 (TIGHT):    振幅<2% 且 涨跌幅绝对值<1%
      - 宽幅震荡 (WIDE):     振幅>4%
    """
    regimes = []
    for i, row in df.iterrows():
        r = "其他"
        if row["ret"] < -0.03:
            r = "急跌"
        elif row["ret"] > 0.03:
            r = "急涨"
        elif row["amplitude"] < 0.02 and abs(row["ret"]) < 0.01:
            r = "窄幅盘整"
        elif row["amplitude"] > 0.04:
            r = "宽幅震荡"

        # 连续涨/跌
        if i >= 1:
            if df["ret"].iloc[i] < 0 and df["ret"].iloc[i-1] < 0:
                if r == "其他": r = "连跌"
            if df["ret"].iloc[i] > 0 and df["ret"].iloc[i-1] > 0:
                if r == "其他": r = "连涨"

        regimes.append(r)
    return regimes


def print_kline_table(df, regimes):
    """ASCII K线表"""
    print(f"\n{'─'*90}")
    print(f"{'日期':>6s}  {'开盘':>7s} {'收盘':>7s} {'最高':>7s} {'最低':>7s}  "
          f"{'涨跌':>7s} {'振幅':>6s} {'RSI':>5s} {'pos':>5s}  "
          f"{'大单净':>8s} {'尾盘':>7s}  {'阶段':s}")
    print(f"{'─'*90}")

    for i, (_, row) in enumerate(df.iterrows()):
        date_str = row["date"].strftime("%m-%d")
        ret_str = f"{row['ret']:+.2%}"
        amp_str = f"{row['amplitude']:.1%}"
        rsi_str = f"{row['rsi']:.0f}" if not pd.isna(row["rsi"]) else "N/A"
        pos_str = f"{row['pos_20d']:.2f}" if not pd.isna(row["pos_20d"]) else "N/A"
        big_str = f"{row['big_net']/1e6:+.1f}M"
        late_str = f"{row['big_late_net']/1e6:+.1f}M"

        # 用箭头标注关键日
        tag = ""
        if row["ret"] < -0.03:
            tag = " ▼ 急跌"
        elif row["ret"] > 0.03:
            tag = " ▲ 急涨"
        elif row["amplitude"] > 0.05:
            tag = " ↔ 巨震"

        regime = regimes[i]
        print(f"  {date_str}  {row['open']:7.1f} {row['close']:7.1f} "
              f"{row['high']:7.1f} {row['low']:7.1f}  "
              f"{ret_str:>7s} {amp_str:>6s} {rsi_str:>5s} {pos_str:>5s}  "
              f"{big_str:>8s} {late_str:>7s}  {regime}{tag}")


def analyze_drop_behavior(df, drops):
    """分析急跌日的大资金行为"""
    print(f"\n{'─'*90}")
    print("【急跌日主力行为分析】")
    print(f"{'─'*90}")

    for _, d in drops.iterrows():
        idx = df.index[df["date"] == d["date"]].tolist()
        if not idx: continue
        i = idx[0]

        # 跌前 3 天大单净流向
        pre_flow = df["big_net"].iloc[max(0,i-3):i].sum() / 1e6
        pre_late = df["big_late_net"].iloc[max(0,i-3):i].sum() / 1e6

        # 跌当天
        big_today = d["big_net"] / 1e6
        late_today = d["big_late_net"] / 1e6

        # 跌后 N 天反弹
        post_ret = {}
        for n in [1,3,5,10]:
            if i + n < len(df):
                post_ret[n] = df["close"].iloc[i+n] / d["close"] - 1

        date_s = d["date"].strftime("%m-%d")
        # 解释主力行为
        if big_today < -500:
            intent = "主力主动砸盘"
        elif big_today < -100:
            intent = "主力减仓/被动跟卖"
        elif big_today > 100:
            intent = "主力逆势接盘（趁跌吸筹）"
        elif late_today > 50:
            intent = "尾盘偷袭买入"
        else:
            intent = "主力观望"

        print(f"  {date_s} 跌{d['ret']:+.2%}  振幅{d['amplitude']:.1%}  "
              f"大单{big_today:+.1f}M  尾盘{late_today:+.1f}M  跌前3日大单{pre_flow:+.1f}M")
        print(f"         → {intent}")
        ret_str = " ".join([f"{n}d{post_ret[n]:+.2%}" for n in sorted(post_ret.keys()) if not pd.isna(post_ret[n])])
        if ret_str:
            print(f"         跌后收益: {ret_str}")
        print()


def analyze_range_structure(df, highs, lows):
    """分析震荡区间结构"""
    print(f"\n{'─'*90}")
    print("【震荡区间结构】")
    print(f"{'─'*90}")

    # 取全部价格数据（不限于有资金流的日期）
    all_close = df["close"].values
    all_high = df["high"].values
    all_low = df["low"].values

    print(f"  整体价格区间: {all_low.min():.1f} ~ {all_high.max():.1f}  "
          f"(幅度 {(all_high.max()/all_low.min()-1)*100:.1f}%)")

    # 识别主要支撑/阻力位
    # 简单方法：找出局部极值点对应的价格水平聚类
    if highs:
        resist_levels = sorted([df["high"].iloc[h] for h in highs])
        print(f"  阻力位(局部高点): {', '.join([f'{x:.1f}' for x in resist_levels])}")
    if lows:
        support_levels = sorted([df["low"].iloc[l] for l in lows])
        print(f"  支撑位(局部低点): {', '.join([f'{x:.1f}' for x in support_levels])}")

    # 分段统计
    for label, cond in [
        ("MA20上方 (>+2%)", df["dist_ma20"] > 0.02),
        ("MA20附近 (±2%)", df["dist_ma20"].abs() <= 0.02),
        ("MA20下方 (<-2%)", df["dist_ma20"] < -0.02),
    ]:
        subset = df[cond]
        if len(subset) == 0: continue
        avg_big = subset["big_net"].mean() / 1e6
        avg_late = subset["big_late_net"].mean() / 1e6
        print(f"  {label}: {len(subset)}天  "
              f"日均大单净{avg_big:+.1f}M  日均尾盘{avg_late:+.1f}M  "
              f"后5日均涨幅{subset['fwd_5d'].mean():+.2%}")


def analyze_volatility_flow(df):
    """分析高波动日的资金行为"""
    print(f"\n{'─'*90}")
    print("【波动率与主力资金】")
    print(f"{'─'*90}")

    # 按振幅分组
    for label, cond in [
        ("高振幅 (>5%)", df["amplitude"] > 0.05),
        ("中振幅 (3-5%)", (df["amplitude"] > 0.03) & (df["amplitude"] <= 0.05)),
        ("低振幅 (<3%)", df["amplitude"] <= 0.03),
    ]:
        subset = df[cond]
        if len(subset) == 0: continue
        avg_big = subset["big_net"].mean() / 1e6
        avg_late = subset["big_late_net"].mean() / 1e6
        avg_big_pct = subset["big_net_pct"].mean()
        up_pct = (subset["ret"] > 0).mean()
        print(f"  {label}: {len(subset)}天  "
              f"上涨率{up_pct:.0%}  日均大单{avg_big:+.1f}M  大单/成交额{avg_big_pct:+.1%}  尾盘{avg_late:+.1f}M")

    # 结合波动率 + RSI 分象限
    print(f"\n  【波动率×RSI 四象限（主力行为）】")
    df_valid = df.dropna(subset=["rsi","pos_20d"]).copy()
    for label, cond in [
        ("高波+低RSI (<45)  ← 恐慌/抄底区", (df_valid["amplitude"] > 0.04) & (df_valid["rsi"] < 45)),
        ("高波+高RSI (>60)  ← 亢奋/派发区", (df_valid["amplitude"] > 0.04) & (df_valid["rsi"] > 60)),
        ("低波+低RSI         ← 阴跌/无人区", (df_valid["amplitude"] <= 0.03) & (df_valid["rsi"] < 45)),
        ("低波+高RSI         ← 缩量上涨/诱多", (df_valid["amplitude"] <= 0.03) & (df_valid["rsi"] > 60)),
    ]:
        subset = df_valid[cond]
        if len(subset) == 0: continue
        avg_big = subset["big_net"].mean() / 1e6
        avg_late = subset["big_late_net"].mean() / 1e6
        fwd_5 = subset["fwd_5d"].dropna().mean()
        fwd_10 = subset["fwd_10d"].dropna().mean()
        print(f"  {label}: {len(subset)}天  "
              f"大单{avg_big:+.1f}M  尾盘{avg_late:+.1f}M  "
              f"→5d{fwd_5:+.2%}  10d{fwd_10:+.2%}")


def main(stock="HK.00700"):
    print(f"\n{'='*90}")
    print(f"  K线结构 + 主力资金行为分析 —— {stock}")
    print(f"{'='*90}")

    df = load_full_data(stock)
    print(f"样本: {len(df)} 天  ({df['date'].iloc[0].strftime('%Y-%m-%d')} ~ {df['date'].iloc[-1].strftime('%Y-%m-%d')})")

    # 基础统计
    total_ret = df["close"].iloc[-1] / df["close"].iloc[0] - 1
    max_dd = (df["close"] / df["close"].cummax() - 1).min()
    print(f"区间收益: {total_ret:+.2%}  最大回撤: {max_dd:+.2%}  "
          f"日均振幅: {df['amplitude'].mean():.1%}  单日最大振幅: {df['amplitude'].max():.1%}")

    # 1. 走势识别
    highs, lows = detect_swings(df)
    regimes = classify_regime(df)

    print_kline_table(df, regimes)

    # 2. 急跌分析
    drops = detect_sudden_drops(df)
    if len(drops) > 0:
        analyze_drop_behavior(df, drops)

    # 3. 震荡区间结构
    analyze_range_structure(df, highs, lows)

    # 4. 波动率 × 资金流
    analyze_volatility_flow(df)

    # 5. 量价背离
    print(f"\n{'─'*90}")
    print("【量价背离信号】")
    print(f"{'─'*90}")
    for i in range(1, len(df)):
        price_up = df["ret"].iloc[i] > 0.01
        price_down = df["ret"].iloc[i] < -0.01
        vol_up = df["vol_ratio"].iloc[i] > 1.3 if not pd.isna(df["vol_ratio"].iloc[i]) else False
        vol_down = df["vol_ratio"].iloc[i] < 0.7 if not pd.isna(df["vol_ratio"].iloc[i]) else False
        big_sign = df["big_net"].iloc[i]

        if price_up and big_sign < -200e6:
            print(f"  {df['date'].iloc[i].strftime('%m-%d')} 价涨{df['ret'].iloc[i]:+.2%} "
                  f"但大单净卖出{big_sign/1e6:+.0f}M  ← 拉高出货嫌疑")
        if price_down and big_sign > 200e6:
            print(f"  {df['date'].iloc[i].strftime('%m-%d')} 价跌{df['ret'].iloc[i]:+.2%} "
                  f"但大单净买入{big_sign/1e6:+.0f}M  ← 打压吸筹嫌疑")
        if price_up and vol_down:
            print(f"  {df['date'].iloc[i].strftime('%m-%d')} 价涨{df['ret'].iloc[i]:+.2%} "
                  f"但缩量(量比{df['vol_ratio'].iloc[i]:.1f}x)  ← 上涨乏力")

    # 6. 关键转折点
    print(f"\n{'─'*90}")
    print("【关键转折点】")
    print(f"{'─'*90}")
    if highs:
        print(f"  局部高点:")
        for h in highs[:8]:
            d = df.iloc[h]
            print(f"    {d['date'].strftime('%m-%d')} 高{d['high']:.1f}  "
                  f"大单{d['big_net']/1e6:+.1f}M  尾盘{d['big_late_net']/1e6:+.1f}M  "
                  f"RSI={d['rsi']:.0f}" if not pd.isna(d['rsi']) else "")
    if lows:
        print(f"  局部低点:")
        for l in lows[:8]:
            d = df.iloc[l]
            print(f"    {d['date'].strftime('%m-%d')} 低{d['low']:.1f}  "
                  f"大单{d['big_net']/1e6:+.1f}M  尾盘{d['big_late_net']/1e6:+.1f}M  "
                  f"RSI={d['rsi']:.0f}" if not pd.isna(d['rsi']) else "")

    print(f"\n{'='*90}\n")


if __name__ == "__main__":
    stock = sys.argv[1] if len(sys.argv) > 1 else "HK.00700"
    main(stock)
