#!/usr/bin/env python3
"""
买入/卖出指数 — 特征工程 + 权重训练（方案A：未来收益回归）
HK.00700，数据窗口 2026-06-15 ~ 2026-06-26

特征（12个）：
  1-4: 四档资金方向（特大/大/中/小单，净买占比 (buy-sell)/(buy+sell) → [-1,1]）
  5:   背离信号（大跌+大资金净买→+0.5，大涨+大资金净卖→-0.5）
  6-7: RSI_6 / RSI_24 → (50-RSI)/50 钳位 [-1,1]
  8:   港股通 → tanh(净流入/2亿)
  9:   沽空 → -tanh((ratio-mean)/std)
  10:  RSI6背离（价新低+RSI6未新低→+1，价新高+RSI6未新高→-1，窗口5天）
  11:  52周位置（指数衰减，k=14，f12=贴底衰减-贴顶衰减）→ [-1,1]
  12:  沽空变化 → -tanh(Δratio/0.05)，上升→看跌，下降→看涨（回补）

阈值（股数）：小<400, 中<7000, 大<25000, 特大≥25000
Label: next_day_return = (次日收盘 - 当日收盘) / 当日收盘
"""
import numpy as np
from datetime import date, timedelta
from db import get_conn
import warnings
warnings.filterwarnings("ignore")

# ============ 配置 ============
STOCK = "HK.00700"

# 股数阈值（图 2 配置）
SMALL_MAX = 400          # <400 = 小单
MID_MAX = 7_000          # 400~7000 = 中单
BIG_MAX = 25_000         # 7000~25000 = 大单
# ≥25000 = 特大单

# ============ 1. 数据提取 ============

def fetch_daily_quote():
    """拉取 daily_quote：收盘价、涨跌幅、52周高低点"""
    sql = f"""
        SELECT trade_date, last_price, change_pct, high_52w, low_52w
        FROM daily_quote
        WHERE stock_code = '{STOCK}'
        ORDER BY trade_date
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        return cur.fetchall()  # [(date, price, pct, high52, low52), ...]


def fetch_tick_flow(trade_date):
    """汇总当日 4 档主动买卖股数"""
    sql = f"""
        SELECT
            ticker_direction,
            CASE
                WHEN volume < {SMALL_MAX} THEN 'small'
                WHEN volume < {MID_MAX} THEN 'mid'
                WHEN volume < {BIG_MAX} THEN 'big'
                ELSE 'super'
            END AS size,
            SUM(volume) AS total_vol
        FROM tick_data
        WHERE stock_code = '{STOCK}'
          AND tick_time::date = %s
          AND ticker_direction IN ('BUY', 'SELL')
        GROUP BY ticker_direction,
            CASE
                WHEN volume < {SMALL_MAX} THEN 'small'
                WHEN volume < {MID_MAX} THEN 'mid'
                WHEN volume < {BIG_MAX} THEN 'big'
                ELSE 'super'
            END
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (trade_date,))
        rows = cur.fetchall()
    
    # 初始化
    flow = {
        "super_buy": 0, "super_sell": 0,
        "big_buy": 0, "big_sell": 0,
        "mid_buy": 0, "mid_sell": 0,
        "small_buy": 0, "small_sell": 0,
    }
    for direction, size, total_vol in rows:
        key = f"{size}_{direction.lower()}"
        flow[key] = int(total_vol)
    return flow


def fetch_ggt(trade_date):
    """拉取当日港股通净流入（亿港元）"""
    sql = """
        SELECT est_net_inflow FROM daily_ggt_hold
        WHERE stock_code = %s AND trade_date = %s
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (STOCK, trade_date))
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None


def fetch_all_short_data(all_dates):
    """批量拉取所有日期的沽空金额+成交额，一次查询完成，返回 {date: (short_amt_亿, turnover_亿)}"""
    result = {}
    if not all_dates:
        return result
    
    placeholders = ','.join(['%s'] * len(all_dates))
    
    with get_conn() as conn:
        cur = conn.cursor()
        
        # 批量沽空金额
        cur.execute(
            f"SELECT trade_date, short_selling_amt FROM daily_short_selling "
            f"WHERE stock_code=%s AND trade_date IN ({placeholders})",
            [STOCK] + list(all_dates)
        )
        short_map = {row[0]: float(row[1]) for row in cur.fetchall() if row[1] is not None}
        
        # 批量成交额
        cur.execute(
            f"SELECT trade_date, turnover FROM daily_quote "
            f"WHERE stock_code=%s AND trade_date IN ({placeholders})",
            [STOCK] + list(all_dates)
        )
        turnover_map = {row[0]: float(row[1]) / 1e8 for row in cur.fetchall() if row[1] is not None}
    
    for d in all_dates:
        result[d] = (short_map.get(d), turnover_map.get(d))
    
    return result


# ============ 2. 特征计算 ============

def rsi(prices, period):
    """标准 RSI 计算"""
    if len(prices) < period + 1:
        return None
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1 + rs)


def rsi_score(rsi_val):
    """RSI → [-1, 1]，50=0，100=-1，0=1"""
    if rsi_val is None:
        return 0.0
    return max(-1.0, min(1.0, (50 - rsi_val) / 50))


def direction_bias_booster(daily_return, large_net_buy):
    """
    背离加分：大跌(<-1%)+大买→+0.5，大涨(>+1%)+大卖→-0.5
    """
    if daily_return is None:
        return 0.0
    if daily_return < -0.01 and large_net_buy:
        return 0.5
    if daily_return > 0.01 and not large_net_buy:
        return -0.5
    return 0.0


# ============ 3. 主流程 ============

def build_dataset():
    quotes = fetch_daily_quote()
    if len(quotes) < 30:
        print(f"⚠️  daily_quote 只有 {len(quotes)} 条，RSI 需要足够历史数据")
    
    # 建立价格索引
    price_map = {}
    pct_map = {}
    high52w_map = {}
    low52w_map = {}
    for d, price, pct, high52, low52 in quotes:
        price_map[d] = float(price) if price else None
        pct_map[d] = float(pct) if pct else None
        high52w_map[d] = float(high52) if high52 else None
        low52w_map[d] = float(low52) if low52 else None
    
    # 候选日期：tick_data 覆盖范围 6/15~6/29，取 6/15~6/26（沽空/港股通交集到6/26）
    all_dates = sorted(price_map.keys())
    start = max(date(2026, 6, 15), all_dates[0])
    end = all_dates[-1]  # 包含全部有 daily_quote 的日期（自动找次日 label）
    
    features = []
    labels = []
    dates_used = []
    meta = []  # 保存原始值供调试
    rsi6_history = {}  # 用于计算 RSI6 背离窗口
    prev_short_ratio = None  # 用于计算沽空变化
    short_data_map = fetch_all_short_data(all_dates)  # 批量拉取沽空+成交额，替换 N+1 查询
    
    for d in all_dates:
        if d < start or d > end:
            continue
        current_price = price_map.get(d)
        if current_price is None:
            continue
        
        # 找下一个交易日（计算 label）
        next_d = None
        for nd in all_dates:
            if nd > d:
                next_d = nd
                break
        if next_d is not None:
            next_price = price_map.get(next_d)
            if next_price is not None:
                next_return = (next_price - current_price) / current_price
            else:
                next_return = np.nan
        else:
            next_return = np.nan  # 今日，无次日收盘价
        
        # === 资金流向特征 ===
        flow = fetch_tick_flow(d)
        total = sum(flow.values())
        if total == 0:
            continue  # 无交易数据，跳过
        
        # F1-F4: 各档净买占比 = (buy-sell)/(buy+sell)，连续值 [-1, 1]
        def net_ratio(buy, sell):
            total = buy + sell
            return (buy - sell) / total if total > 0 else 0.0

        f1 = net_ratio(flow["super_buy"], flow["super_sell"])
        f2 = net_ratio(flow["big_buy"], flow["big_sell"])
        f3 = net_ratio(flow["mid_buy"], flow["mid_sell"])
        f4 = net_ratio(flow["small_buy"], flow["small_sell"])
        
        # 大资金净买（股数）
        large_net = (flow["super_buy"] + flow["big_buy"]) - (flow["super_sell"] + flow["big_sell"])
        large_net_buy = large_net > 0
        
        # 背离信号
        daily_pct = pct_map.get(d, 0) or 0
        daily_return_decimal = daily_pct / 100.0  # change_pct 是百分比
        f5 = direction_bias_booster(daily_return_decimal, large_net_buy)
        
        # === RSI（去掉高度相关的 RSI12，保留 RSI6 短动量 + RSI24 中动量）===
        prices_before = []
        for pd_ in all_dates:
            if pd_ <= d and price_map.get(pd_) is not None:
                prices_before.append(price_map[pd_])
        
        rsi6 = rsi(prices_before, 6)
        rsi24 = rsi(prices_before, 24)
        
        f6 = rsi_score(rsi6)
        f7 = rsi_score(rsi24)

        # === RSI6 背离（5天窗口）===
        N_DIV = 5
        rsi6_history[d] = rsi6
        window_dates = [pd_ for pd_ in all_dates if pd_ <= d and pd_ in rsi6_history][-N_DIV:]
        if len(window_dates) >= 2 and rsi6 is not None:
            recent_prices = [price_map[pd_] for pd_ in window_dates if price_map.get(pd_) is not None]
            recent_rsi6 = [rsi6_history[pd_] for pd_ in window_dates if rsi6_history.get(pd_) is not None]
            if recent_prices and recent_rsi6:
                price_min, price_max = min(recent_prices), max(recent_prices)
                rsi6_min, rsi6_max = min(recent_rsi6), max(recent_rsi6)
                if current_price <= price_min and rsi6 > rsi6_min:
                    f11 = 1.0   # 底背离：价格新低但 RSI6 未新低 → 看涨
                elif current_price >= price_max and rsi6 < rsi6_max:
                    f11 = -1.0  # 顶背离：价格新高但 RSI6 未新高 → 看跌
                else:
                    f11 = 0.0
            else:
                f11 = 0.0
        else:
            f11 = 0.0

        # === 52周位置（指数衰减，k=14，半衰距~5%range）===
        K_52W = 14.0
        high52 = high52w_map.get(d)
        low52 = low52w_map.get(d)
        if high52 and low52 and high52 > low52 and current_price:
            rg = high52 - low52
            raw_low = np.exp(-K_52W * (current_price - low52) / rg)   # 贴底→1
            raw_high = np.exp(-K_52W * (high52 - current_price) / rg) # 贴顶→1
            f12 = raw_low - raw_high  # [-1,1]，贴底看涨，贴顶看跌
        else:
            raw_low = 0.0
            raw_high = 0.0
            f12 = 0.0

        # === 港股通 ===
        ggt_inflow = fetch_ggt(d)
        if ggt_inflow is not None:
            f9 = np.tanh(ggt_inflow / 2.0)  # 2 亿阈值
        else:
            f9 = 0.0
        
        # === 沽空比率（批量查询，内存计算，无 N+1）===
        short_amt, turnover = short_data_map.get(d, (None, None))
        
        if short_amt is not None and turnover and turnover > 0:
            current_ratio = short_amt / turnover
            
            # 用历史沽空比计算 mean/std（全部在内存中计算）
            ratios = []
            for hist_d in all_dates:
                if hist_d < d:
                    hist_short, hist_turnover = short_data_map.get(hist_d, (None, None))
                    if hist_short is not None and hist_turnover and hist_turnover > 0:
                        ratios.append(hist_short / hist_turnover)
            
            if len(ratios) >= 3:
                mean_r = np.mean(ratios)
                std_r = np.std(ratios) if np.std(ratios) > 0 else 0.01
                f10 = -np.tanh((current_ratio - mean_r) / std_r)
            else:
                f10 = 0.0
        else:
            f10 = 0.0

        # === 沽空变化（相对前一日）===
        if short_amt is not None and turnover and turnover > 0:
            current_ratio = short_amt / turnover
            if prev_short_ratio is not None:
                delta = current_ratio - prev_short_ratio
                f14 = -np.tanh(delta / 0.05)  # 上升→看跌，下降→看涨
            else:
                f14 = 0.0
            prev_short_ratio = current_ratio
        else:
            f14 = 0.0
        
        feat_vec = np.array([f1, f2, f3, f4, f5, f6, f7, f9, f10, f11, f12, f14])
        features.append(feat_vec)
        labels.append(next_return)
        dates_used.append(d)
        meta.append({
            "date": d,
            "flow": flow,
            "rsi6": rsi6, "rsi24": rsi24,
            "f11_divergence": f11,
            "f12_low": raw_low, "f12_high": raw_high, "f12": f12,
            "f14_short_delta": f14,
            "ggt": ggt_inflow,
            "short_ratio": short_amt / turnover if (short_amt and turnover) else None,
            "daily_return": daily_pct,
            "next_return": (next_return * 100) if not np.isnan(next_return) else None,  # %
            "large_net": large_net,
        })
    
    return np.array(features), np.array(labels), dates_used, meta


# ============ 3.5 打印辅助 ============

def cjk_width(s):
    """计算字符串显示宽度（中文/全角=2，ASCII=1）"""
    w = 0
    for ch in str(s):
        if '\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f' or '\uff00' <= ch <= '\uffef':
            w += 2
        elif ch in '▲▼✅❌↑↓':
            w += 2
        else:
            w += 1
    return w

def pad_to(s, width):
    """右对齐补齐到指定显示宽度"""
    dw = cjk_width(s)
    need = width - dw
    if need > 0:
        return ' ' * need + str(s)
    return str(s)

def pad_left(s, width):
    """左对齐补齐到指定显示宽度"""
    dw = cjk_width(s)
    need = width - dw
    if need > 0:
        return str(s) + ' ' * need
    return str(s)

# ============ 4. 分析 ============

def analyze():
    X, y, dates, meta = build_dataset()
    
    if len(X) < 1:
        print("❌ 样本不足")
        return
    
    # 区分有/无 label 的行
    valid_mask = ~np.isnan(y)
    X_valid = X[valid_mask]
    y_valid = y[valid_mask]
    
    print(f"样本: {len(X)} 天（含 label: {len(y_valid)} 天）")
    print(f"日期: {dates[0]} ~ {dates[-1]}")
    if len(y_valid) > 0:
        print(f"Label(次日收益): mean={np.mean(y_valid)*100:.2f}%, std={np.std(y_valid)*100:.2f}%")
    print()
    
    feature_names = [
        "特大单方向", "大单方向", "中单方向", "小单方向",
        "背离信号",
        "RSI_6", "RSI_24",
        "港股通", "沽空", "RSI6背离",
        "52周位置", "沽空变化"
    ]
    feature_short = [
        "特大单", "大单", "中单", "小单",
        "背离",
        "RSI6", "RSI24",
        "港股通", "沽空", "RSI6背",
        "52周位", "沽变"
    ]
    
    # === 4a. 相关系数 ===
    print("=" * 65)
    print("相关系数矩阵（特征 vs 次日收益率）")
    print("-" * 65)
    corrs = []
    for i, name in enumerate(feature_names):
        if len(y_valid) < 2 or np.std(X_valid[:, i]) == 0:
            corr = 0.0
        else:
            corr = np.corrcoef(X_valid[:, i], y_valid)[0, 1]
        corrs.append((name, corr))
    
    corrs.sort(key=lambda x: abs(x[1]), reverse=True)
    for name, c in corrs:
        direction = "↑买" if c > 0 else "↓卖"
        print(f"  {pad_left(name,12)}: {c:+.4f}  ({direction})")
    
    # === 4b. 特征矩阵（0-100 分制）===
    print()
    print("=" * 85)
    print("每日特征矩阵（0-100分，50=中性，>50看多，<50看空）")
    print("-" * 85)
    header = f"{pad_to('日期',10)}"
    for n in feature_short:
        header += f" {pad_to(n,6)}"
    header += "  label%  综合"
    print(header)
    for i, d in enumerate(dates):
        row = f"{str(d):>10}"
        for j in range(12):
            score = 50 + X[i, j] * 50
            row += f" {score:6.0f}"
        avg_score = 50 + np.mean(X[i]) * 50
        if np.isnan(y[i]):
            row += f" {'N/A':>6s} {avg_score:5.0f}"
        else:
            row += f" {y[i]*100:+6.2f} {avg_score:5.0f}"
        print(row)
    
    # === 4c. Ridge 回归 ===
    print()
    print("=" * 65)
    print("Ridge 回归（L2 正则化，alpha=1.0）")
    print("-" * 65)
    
    from sklearn.linear_model import Ridge
    from sklearn.preprocessing import StandardScaler
    
    if len(X_valid) >= 3:
        # 标准化
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X_valid)
        
        ridge = Ridge(alpha=1.0)
        ridge.fit(X_scaled, y_valid)
        
        # 系数（标准化后可比大小）
        coefs = list(zip(feature_names, ridge.coef_))
        coefs.sort(key=lambda x: abs(x[1]), reverse=True)
        
        for name, c in coefs:
            direction = "↑买" if c > 0 else "↓卖"
            print(f"  {pad_left(name,12)}: {c:+.4f}  ({direction})")
        
        y_pred = ridge.predict(X_scaled)
        ss_res = np.sum((y_valid - y_pred) ** 2)
        ss_tot = np.sum((y_valid - np.mean(y_valid)) ** 2)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
        print(f"\n  R² = {r2:.4f}")
        
        # 方向准确率
        y_valid_nozero = y_valid[y_valid != 0]
        y_pred_nozero = y_pred[y_valid != 0]
        if len(y_valid_nozero) > 0:
            correct = np.sum(np.sign(y_pred_nozero) == np.sign(y_valid_nozero))
            total = len(y_valid_nozero)
            acc = correct / total
            print(f"  方向准确率 = {correct}/{total} = {acc:.1%}")
    
    # === 4d. 等权回测（0-100 分制）===
    print()
    print("=" * 65)
    print("等权指数回测（均权平均 → 0-100分，50=中性）")
    print("-" * 65)
    equal_raw = np.mean(X, axis=1)  # [-1, 1]
    equal_score = 50 + equal_raw * 50  # [0, 100]
    
    print(f"{'日期':>10}  {'得分':>6}  {'次日收益%':>10}  {'方向匹配':>8}")
    print("-" * 42)
    matches = 0
    valid_matches = 0
    for i, d in enumerate(dates):
        s = equal_score[i]
        if np.isnan(y[i]):
            direction = "▲看涨" if s > 50 else ("▼看跌" if s < 50 else "─中性")
            print(f"{str(d):>10}  {s:6.1f}  {'N/A':>10s}  {pad_to(direction,6)}  -")
        else:
            ret = y[i] * 100
            match = (s > 50 and ret > 0) or (s < 50 and ret < 0)
            if match:
                matches += 1
            valid_matches += 1
            direction = "▲看涨" if s > 50 else ("▼看跌" if s < 50 else "─中性")
            print(f"{str(d):>10}  {s:6.1f}  {ret:+10.2f}  {pad_to(direction,6)} {'✅' if match else '❌'}")
    if valid_matches > 0:
        print(f"\n方向命中: {matches}/{valid_matches} = {matches/valid_matches:.1%}")
    
    # === 4e. 原始数据附录 ===
    print()
    print("=" * 65)
    print("原始数据附录（供人工校验）")
    print("-" * 65)
    for m in meta:
        f = m["flow"]
        print(f"\n{m['date']}:")
        next_ret_str = f"{m['next_return']:+.2f}%" if m['next_return'] is not None else "N/A"
        print(f"  涨跌幅: {m['daily_return']:+.2f}%  →  次日: {next_ret_str}")
        print(f"  特大买:{f['super_buy']:,}  特大卖:{f['super_sell']:,}  净:{f['super_buy']-f['super_sell']:+,}")
        print(f"  大单买:{f['big_buy']:,}  大单卖:{f['big_sell']:,}  净:{f['big_buy']-f['big_sell']:+,}")
        print(f"  中单买:{f['mid_buy']:,}  中单卖:{f['mid_sell']:,}  净:{f['mid_buy']-f['mid_sell']:+,}")
        print(f"  小单买:{f['small_buy']:,}  小单卖:{f['small_sell']:,}  净:{f['small_buy']-f['small_sell']:+,}")
        print(f"  大资金净股数: {m['large_net']:+,}")
        print(f"  RSI6={m['rsi6']:.1f}  RSI24={m['rsi24']:.1f}")
        print(f"  RSI6背离信号: {m['f11_divergence']:+.0f}")
        print(f"  52周位置: 低点衰减={m['f12_low']:.3f}  高点衰减={m['f12_high']:.3f}  f12={m['f12']:+.3f}({50+m['f12']*50:.0f}分)")
        print(f"  沽空变化: {m['f14_short_delta']:+.3f}({50+m['f14_short_delta']*50:.0f}分)")
        print(f"  港股通净流入: {m['ggt']:.2f}亿" if m['ggt'] else "  港股通: 无数据")
        print(f"  沽空比: {m['short_ratio']*100:.1f}%" if m['short_ratio'] else "  沽空: 无数据")


if __name__ == "__main__":
    analyze()
