"""
主力行为阶段分析：将每日技术位置 + 资金流组合为「阶段」标签，
追踪阶段切换路径，评估各阶段的期望收益。

阶段定义（5 态）：
  - 底部吸筹 (ACCUMULATING): 低位 + 低RSI + 大单净买（尤其尾盘）
  - 确认拉升 (RALLYING): 价格 > MA20, RSI 50-70, 大单持续净买
  - 高位派发 (DISTRIBUTING): 高位 + 高RSI + 大单净卖
  - 下跌出清 (DECLINING): 价格 < MA20, RSI 下行, 大单净卖
  - 盘整观望 (CONSOLIDATING): 其余中性区间

输出：
  1. 逐日阶段标签表
  2. 阶段转移矩阵（下一日/5日/10日）
  3. 各阶段未来 N 日收益分布
  4. 当前日期所处的阶段 + 相似历史案例
"""
import sys
import numpy as np
import pandas as pd
from collections import defaultdict
from scipy.stats import spearmanr
from db import get_conn

# ── 阈值（对齐生产环境） ──────────────────────────────────
XL_VOL = 25000  # 特大单 ≥ 25000 股
LG_VOL = 7000   # 大单 7000-25000
MD_VOL = 300    # 中单 300-7000，小单 <300
LATE_START = "15:00:00"     # 尾盘起点（A+H 尾盘从 15:00 起）
AFTERNOON_START = "13:00:00"  # 下午开盘
MORNING_END = "12:00:00"      # 上午收盘

RSI_PERIOD = 14
MA_SHORT, MA_LONG = 5, 20


# ═══════════════════════════════════════════════════════════
# 1. 数据加载
# ═══════════════════════════════════════════════════════════

def load_daily_features(stock):
    """加载每日技术 + 资金流特征"""
    with get_conn() as conn:
        cur = conn.cursor()

        # ── 日线价格 ──
        cur.execute("""
            SELECT trade_date, last_price, volume, turnover,
                   open_price, high_price, low_price
            FROM daily_quote WHERE stock_code=%s
            ORDER BY trade_date
        """, (stock,))
        price_rows = cur.fetchall()

        # ── 逐笔资金流（含尾盘区分） ──
        cur.execute("""
            SELECT DATE(tick_time) AS td,
                   -- 全天
                   COALESCE(SUM(CASE WHEN volume>=%s AND ticker_direction='BUY'  THEN turnover END),0) AS xl_in_full,
                   COALESCE(SUM(CASE WHEN volume>=%s AND ticker_direction='SELL' THEN turnover END),0) AS xl_out_full,
                   COALESCE(SUM(CASE WHEN volume>=%s AND volume<%s AND ticker_direction='BUY'  THEN turnover END),0) AS lg_in_full,
                   COALESCE(SUM(CASE WHEN volume>=%s AND volume<%s AND ticker_direction='SELL' THEN turnover END),0) AS lg_out_full,
                   -- 尾盘（15:00-16:00）
                   COALESCE(SUM(CASE WHEN volume>=%s AND tick_time::time>=%s::time AND ticker_direction='BUY'  THEN turnover END),0) AS xl_in_late,
                   COALESCE(SUM(CASE WHEN volume>=%s AND tick_time::time>=%s::time AND ticker_direction='SELL' THEN turnover END),0) AS xl_out_late,
                   COALESCE(SUM(CASE WHEN volume>=%s AND volume<%s AND tick_time::time>=%s::time AND ticker_direction='BUY'  THEN turnover END),0) AS lg_in_late,
                   COALESCE(SUM(CASE WHEN volume>=%s AND volume<%s AND tick_time::time>=%s::time AND ticker_direction='SELL' THEN turnover END),0) AS lg_out_late,
                   -- 下午（13:00-16:00）
                   COALESCE(SUM(CASE WHEN volume>=%s AND tick_time::time>=%s::time AND ticker_direction='BUY'  THEN turnover END),0) AS xl_in_pm,
                   COALESCE(SUM(CASE WHEN volume>=%s AND tick_time::time>=%s::time AND ticker_direction='SELL' THEN turnover END),0) AS xl_out_pm,
                   COALESCE(SUM(CASE WHEN volume>=%s AND volume<%s AND tick_time::time>=%s::time AND ticker_direction='BUY'  THEN turnover END),0) AS lg_in_pm,
                   COALESCE(SUM(CASE WHEN volume>=%s AND volume<%s AND tick_time::time>=%s::time AND ticker_direction='SELL' THEN turnover END),0) AS lg_out_pm,
                   COUNT(*) AS tick_cnt
            FROM tick_data
            WHERE stock_code=%s AND ticker_direction IN ('BUY','SELL')
            GROUP BY td ORDER BY td
        """, (
            XL_VOL, XL_VOL,
            LG_VOL, XL_VOL, LG_VOL, XL_VOL,
            XL_VOL, LATE_START, XL_VOL, LATE_START,
            LG_VOL, XL_VOL, LATE_START, LG_VOL, XL_VOL, LATE_START,
            XL_VOL, AFTERNOON_START, XL_VOL, AFTERNOON_START,
            LG_VOL, XL_VOL, AFTERNOON_START, LG_VOL, XL_VOL, AFTERNOON_START,
            stock,
        ))
        flow_rows = cur.fetchall()

    # ── 合并 ──
    price_df = pd.DataFrame(price_rows,
        columns=["date","close","vol","turnover","open","high","low"])
    price_df["date"] = pd.to_datetime(price_df["date"])

    flow_df = pd.DataFrame(flow_rows,
        columns=["date","xl_in","xl_out","lg_in","lg_out",
                 "xl_in_late","xl_out_late","lg_in_late","lg_out_late",
                 "xl_in_pm","xl_out_pm","lg_in_pm","lg_out_pm","tick_cnt"])
    flow_df["date"] = pd.to_datetime(flow_df["date"])

    df = price_df.merge(flow_df, on="date", how="inner").sort_values("date").reset_index(drop=True)

    # PG NUMERIC → float（避免 Decimal vs float 运算报错）
    num_cols = ["close","vol","turnover","open","high","low",
                "xl_in","xl_out","lg_in","lg_out",
                "xl_in_late","xl_out_late","lg_in_late","lg_out_late",
                "xl_in_pm","xl_out_pm","lg_in_pm","lg_out_pm","tick_cnt"]
    for c in num_cols:
        if c in df.columns:
            df[c] = df[c].astype(float)

    # ── 派生特征 ──
    df["xl_net"] = df["xl_in"] - df["xl_out"]
    df["lg_net"] = df["lg_in"] - df["lg_out"]
    df["xl_late_net"] = df["xl_in_late"] - df["xl_out_late"]
    df["lg_late_net"] = df["lg_in_late"] - df["lg_out_late"]
    df["xl_pm_net"] = df["xl_in_pm"] - df["xl_out_pm"]
    df["lg_pm_net"] = df["lg_in_pm"] - df["lg_out_pm"]
    df["big_net"] = df["xl_net"] + df["lg_net"]           # 超大+大合计
    df["big_late_net"] = df["xl_late_net"] + df["lg_late_net"]
    df["big_pm_net"] = df["xl_pm_net"] + df["lg_pm_net"]

    # 成交额加权归一化（net / turnover）
    df["xl_net_pct"] = df["xl_net"] / df["turnover"].replace(0, np.nan)
    df["lg_net_pct"] = df["lg_net"] / df["turnover"].replace(0, np.nan)
    df["big_net_pct"] = df["big_net"] / df["turnover"].replace(0, np.nan)
    df["big_late_pct"] = df["big_late_net"] / df["turnover"].replace(0, np.nan)

    # 收益率
    df["ret"] = df["close"].pct_change()

    # ── 技术指标（需要窗口，tail 补齐） ──
    # RSI(14) — Wilder 平滑法
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # MA
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()

    # 价格位置
    roll_low_20 = df["low"].rolling(20).min()
    roll_high_20 = df["high"].rolling(20).max()
    df["pos_20d"] = (df["close"] - roll_low_20) / (roll_high_20 - roll_low_20).replace(0, np.nan)
    df["dist_ma20"] = (df["close"] - df["ma20"]) / df["ma20"].replace(0, np.nan)

    # 5日累计涨跌幅
    df["ret_5d"] = df["close"].pct_change(5)

    # 量比（相对20日均量）
    df["vol_ma20"] = df["vol"].rolling(20).mean()
    df["vol_ratio"] = df["vol"] / df["vol_ma20"].replace(0, np.nan)

    # 前向 N 日收益（标签）
    for n in [1, 3, 5, 10, 20]:
        df[f"fwd_ret_{n}d"] = df["close"].shift(-n) / df["close"] - 1
        df[f"fwd_sign_{n}d"] = (df[f"fwd_ret_{n}d"] > 0).astype(int)

    # 大单尾盘占比（分 xl/lg 各自计算，用于识别"突击"模式）
    df["lg_late_pct"] = np.where(
        df["lg_net"].abs() > 1,
        df["lg_late_net"] / df["lg_net"].abs(),
        0
    )
    df["xl_late_pct"] = np.where(
        df["xl_net"].abs() > 1,
        df["xl_late_net"] / df["xl_net"].abs(),
        0
    )
    df["late_concentration"] = np.where(
        df["big_net"].abs() > 1,
        df["big_late_net"] / df["big_net"].abs(),
        0
    )

    # ── xl vs lg 背离信号（核心新增特征） ──
    df["xl_sign"] = np.sign(df["xl_net"])
    df["lg_sign"] = np.sign(df["lg_net"])
    df["diverge"] = (df["xl_sign"] != df["lg_sign"]).astype(int)
    # xl买+lg卖 = 造势出货
    df["xl_buy_lg_sell"] = ((df["xl_net"] > 0) & (df["lg_net"] < 0)).astype(int)
    # xl卖+lg买 = 压盘吸筹
    df["xl_sell_lg_buy"] = ((df["xl_net"] < 0) & (df["lg_net"] > 0)).astype(int)

    # 连续 N 日流向（分 lg 和 xl 各自追踪）
    df["big_net_sign"] = np.sign(df["big_net"])
    df["lg_net_sign"] = np.sign(df["lg_net"])
    df["xl_net_sign"] = np.sign(df["xl_net"])
    for w in [2, 3, 5]:
        df[f"big_net_cum{w}d"] = df["big_net"].rolling(w).sum()
        df[f"lg_net_cum{w}d"] = df["lg_net"].rolling(w).sum()
        df[f"xl_net_cum{w}d"] = df["xl_net"].rolling(w).sum()

    # ── 波动率维度（区分真吸筹 vs 恐慌 vs 过热） ──
    # 日收益率波动率（5日/10日窗口）
    df["volatility_5d"] = df["ret"].rolling(5).std()
    df["volatility_10d"] = df["ret"].rolling(10).std()
    # 日内振幅
    df["hl_range"] = (df["high"] - df["low"]) / df["close"]
    df["hl_range_ma5"] = df["hl_range"].rolling(5).mean()
    # 波动收敛/扩张信号
    df["vol_contracting"] = (df["volatility_5d"] < df["volatility_10d"] * 0.8).astype(int)
    df["vol_expanding"] = (df["volatility_5d"] > df["volatility_10d"] * 1.3).astype(int)
    # 3日前瞻大单信号（用于检测"拉高出货"前置模式）
    df["big_net_cum3d_fwd"] = df["big_net"].rolling(3).sum().shift(-3)

    # 只对参与分类的特征列 dropna（不含前向收益列，否则几乎全空）
    feature_cols = ["close","rsi","pos_20d","dist_ma20","big_net","big_net_pct",
                    "lg_net","lg_net_pct","lg_late_pct",
                    "big_late_pct","ret_5d","vol_ratio","xl_net","xl_net_pct",
                    "volatility_5d","volatility_10d","diverge"]
    df = df.dropna(subset=[c for c in feature_cols if c in df.columns]).reset_index(drop=True)
    return df


# ═══════════════════════════════════════════════════════════
# 2. 阶段分类器
# ═══════════════════════════════════════════════════════════

def classify_phase(row):
    """
    规则引擎：将每日特征映射到阶段。

    核心设计原则（基于 2026-08-10 实证结论）：
      - 主信号 = 大单(lg, 300-1000万): IC 0.48→0.57(3d) 有持续中期预测力，代表"真金白银"
      - 辅助信号 = 特大单(xl, ≥1000万): IC≈0 无预测力，当日冲击瞬发不持续，代表"造势/做市"
      - 背离信号 = xl vs lg 方向相反: 42%交易日出现，每次背离后续均为负收益
        · xl买+lg卖 = "造势出货"（11天仅1天正收益）
        · xl卖+lg买 = "压盘吸筹"（样本小但趋势正向）

    优先级（从高到低）：
      0. xl/lg方向背离 — 最高优先，直接判定出货/接盘意图
      1. 高位冲顶(过热) — 放量+lg净卖或背离 → 顶部风险
      2. 恐慌出清 — 低位+高波+lg净卖
      3. 低波吸筹 — 低位+低波+lg净买（真吸筹）
      4. 高位派发 — 高位+lg净卖
      5. 确认拉升 — MA20上方+RSI中性+lg净买
      6. 下跌出清 — MA20下方+RSI低
      7. 盘整 — 兜底
    """

    pos = row["pos_20d"]
    rsi = row["rsi"]
    dist_ma = row["dist_ma20"]

    # 主信号：大单（300-1000万，真正的机构行为甜区）
    lg_net = row["lg_net"]
    lg_pct = row["lg_net_pct"]
    lg_late_pct = row.get("lg_late_pct", 0)

    # 辅助信号：特大单（≥1000万，做市/造势工具）
    xl_net = row["xl_net"]
    xl_pct = row.get("xl_net_pct", 0)

    # 合计（仅用于量级判断和展示）
    big_net = row["big_net"]
    big_pct = row["big_net_pct"]

    ret_5d = row["ret_5d"]
    vol_ratio = row.get("vol_ratio", 1.0)
    vol_5d = row.get("volatility_5d", 0.02)
    vol_10d = row.get("volatility_10d", 0.02)
    hl_range = row.get("hl_range", 0.02)

    # ── 优先级 0：xl/lg背离 — 最高优先，揭示主力真实意图 ──
    if xl_net > 0 and lg_net < 0:
        # 特大单买+大单卖 = 用特大单造势拉高，大单悄悄出货
        # 数据支持: n=11天, 1d avg=-1.6%, 正天数仅 1/11
        if pos > 0.65:
            return "高位诱多(背离)"
        if vol_ratio > 1.5:
            return "放量诱多(背离)"
        return "量价背离(出货)"

    if xl_net < 0 and lg_net > 0:
        # 特大单卖+大单买 = 用特大单压盘制造恐慌，大单悄悄接货
        # 数据支持: n=5天, 5d avg=+2.6%
        if pos < 0.35:
            return "压盘吸筹(背离)"
        return "量价背离(接盘)"

    # ── 优先级 1：高位冲顶(过热) ──
    # 高位+放量+大单在卖 = 拉高出货
    # 07/16: xl=+1331M, lg=-174M → 被背离逻辑捕获
    # 08/03: xl=+1444M, lg=-316M → 被背离逻辑捕获
    if pos > 0.7:
        if vol_ratio > 1.8 and lg_net < 0 and lg_pct < -0.02:
            return "高位冲顶(过热)"
        if vol_ratio > 1.5 and lg_net < 0 and rsi > 65:
            return "高位冲顶(放量)"
        # 高位+整体买盘很弱 → 主力不跟了
        if lg_pct < 0.005 and vol_ratio < 0.6:
            return "高位缩量(危险)"

    # ── 优先级 2：恐慌出清 ──
    if pos < 0.4 and rsi < 50:
        if lg_pct < -0.05 and lg_net < 0:
            if vol_5d > vol_10d * 1.2 or hl_range > 0.04:
                return "恐慌出清(高波)"
            return "恐慌出清"
        if lg_net < 0:
            return "持续下跌"

    # ── 优先级 3：低波吸筹(真) ──
    if pos < 0.4 and rsi < 55:
        if lg_net > 0 and lg_pct > 0.02:
            if vol_5d < vol_10d * 0.9:
                if lg_late_pct > 0.05:
                    return "低波吸筹(尾盘)"
                return "低波吸筹"
            return "底部试探"
        if lg_net <= 0 and vol_5d < vol_10d * 0.8:
            return "底部磨底"
        return "底部观望"

    # ── 优先级 4：高位派发 ──
    if pos > 0.65 and rsi > 55:
        if lg_pct < -0.04 and lg_net < 0:
            return "高位派发(加速)"
        if lg_net < 0 and lg_late_pct < -0.02:
            return "高位滞涨(尾盘出货)"
        if lg_net < 0:
            return "高位派发"
        # 高位+大单净买但量缩且不坚定 → 危险
        if lg_pct < 0.01 and vol_ratio < 0.6:
            return "高位缩量(危险)"

    # ── 优先级 5：确认拉升 ──
    if dist_ma > 0.005 and 40 < rsi < 75:
        if lg_pct > 0.04 and lg_net > 0:
            return "放量拉升"
        if lg_pct > 0.02 and lg_late_pct > 0.02:
            return "稳步拉升"
        if lg_net > 0:
            if pos > 0.7:
                return "高位弱势"
            return "温和上涨"
        if lg_net < 0 and vol_ratio < 0.8:
            return "缩量退潮"
        if pos > 0.7 and vol_ratio < 0.8:
            return "高位缩量(危险)"
        return "缩量上涨"

    # ── 优先级 6：下跌出清 ──
    if dist_ma < -0.01 and rsi < 55:
        if lg_pct > 0.02 and lg_net > 0:
            return "下跌中接盘"
        if lg_net < 0:
            return "阴跌"
        return "阴跌(观望)"

    # ── 盘整 ── 兜底
    if abs(dist_ma) < 0.02 and abs(ret_5d) < 0.04:
        if lg_net > 0:
            return "盘整(偏买)"
        elif lg_net < 0:
            return "盘整(偏卖)"
        return "窄幅盘整"

    return "盘整观望"


# ═══════════════════════════════════════════════════════════
# 3. 转移矩阵
# ═══════════════════════════════════════════════════════════

def build_transition_matrix(df, horizon_days=1):
    """构建阶段 → 阶段 转移矩阵（N 日后）"""
    phase_col = "phase"
    horizon = f"phase_{horizon_days}d"
    df_copy = df.copy()
    df_copy[horizon] = df_copy[phase_col].shift(-horizon_days)
    df_copy = df_copy.dropna(subset=[horizon])

    phases = sorted(df[phase_col].unique())
    n = len(phases)
    mat = pd.DataFrame(0, index=phases, columns=phases, dtype=float)

    for _, row in df_copy.iterrows():
        src = row[phase_col]
        dst = row[horizon]
        mat.loc[src, dst] += 1

    # 行归一化
    row_sum = mat.sum(axis=1)
    mat_pct = mat.div(row_sum, axis=0).fillna(0)
    return mat_pct.round(3)


# ═══════════════════════════════════════════════════════════
# 4. 阶段收益分析
# ═══════════════════════════════════════════════════════════

def phase_forward_returns(df, horizon_days=[1, 3, 5, 10, 20]):
    """各阶段未来 N 日收益统计"""
    results = []
    for phase in sorted(df["phase"].unique()):
        subset = df[df["phase"] == phase]
        n = len(subset)
        row = {"phase": phase, "count": n}
        for h in horizon_days:
            col = f"fwd_ret_{h}d"
            vals = subset[col].dropna()
            if len(vals) == 0:
                row[f"avg_{h}d"] = np.nan
                row[f"win_{h}d"] = np.nan
            else:
                row[f"avg_{h}d"] = vals.mean()
                row[f"win_{h}d"] = (vals > 0).mean()
        results.append(row)
    return pd.DataFrame(results).sort_values("count", ascending=False)


# ═══════════════════════════════════════════════════════════
# 4b. 朴素基线分类器（用于增量信息检验）
# ═══════════════════════════════════════════════════════════

def classify_technical_only(row):
    """Baseline A：纯技术指标分类（无资金流）"""
    pos, rsi, dist_ma = row["pos_20d"], row["rsi"], row["dist_ma20"]
    if pos < 0.3 and rsi < 50:
        return "T_低位"
    if pos > 0.7 and rsi > 55:
        return "T_高位"
    if dist_ma > 0.005 and 40 < rsi < 75:
        return "T_上升"
    if dist_ma < -0.01 and rsi < 55:
        return "T_下跌"
    return "T_盘整"

def classify_flow_only(row):
    """Baseline B：纯资金流分类（无技术指标位置）"""
    big_pct = row["big_net_pct"]
    big_late = row["big_late_pct"]
    if big_pct > 0.04:
        return "F_强买" if big_late > 0.02 else "F_偏买"
    if big_pct > 0.01:
        return "F_偏买"
    if big_pct < -0.04:
        return "F_强卖"
    if big_pct < -0.01:
        return "F_偏卖"
    return "F_中性"


# ── 预期收益排序（用于 Spearman 单调性检验） ──

# 我们的分类器：吸筹>拉升>退潮>观望>下跌>派发
CATEGORY_RANK = {"吸筹": 1, "拉升": 2, "退潮": 3, "观望": 4, "下跌": 5, "派发": 6}
# Baseline A（纯技术）：低位>上升>盘整>高位>下跌
BASELINE_A_RANK = {"T_低位": 1, "T_上升": 2, "T_盘整": 3, "T_高位": 4, "T_下跌": 5}
# Baseline B（纯资金）：强买>偏买>中性>偏卖>强卖
BASELINE_B_RANK = {"F_强买": 1, "F_偏买": 2, "F_中性": 3, "F_偏卖": 4, "F_强卖": 5}


def eval_classifier(df, label_col, rank_map, horizons=[1, 3, 5, 10, 20]):
    """
    评估一个分类器的排序单调性 & 分层能力。

    返回:
      spearman: {horizon: (ρ, p-value)}  — 排序单调性
      eta_sq:   {horizon: η²}            — 分层解释力（组间方差/总方差）
      detail:   {horizon: {label: (n, mean_ret)}} — 每组详情
    """
    labels_present = [l for l in rank_map if l in df[label_col].values]
    if len(labels_present) < 3:
        return None  # 样本太少，跳过

    spearman = {}
    eta_sq = {}
    detail = {}

    for h in horizons:
        col = f"fwd_ret_{h}d"
        valid = df.dropna(subset=[col])

        # 每组平均收益
        group_stats = {}
        for label in labels_present:
            subset = valid[valid[label_col] == label][col]
            if len(subset) >= 2:
                group_stats[label] = (len(subset), subset.mean())

        # Spearman：预期排名 vs 实际收益排名
        labels_ranked = sorted(group_stats.keys(), key=lambda l: group_stats[l][1], reverse=True)
        expected = np.array([rank_map[l] for l in labels_ranked])
        actual = np.arange(1, len(labels_ranked) + 1)  # 实际收益排名（1=最高收益）
        if len(set(actual)) >= 3:
            rho, p = spearmanr(expected, actual)
            # 若只有 2 个不同值则报 nan
            spearman[h] = (rho if not np.isnan(rho) else 0.0, p if not np.isnan(p) else 1.0)
        else:
            spearman[h] = (0.0, 1.0)

        # η² = 组间方差 / 总方差
        all_vals = valid[col].values
        grand_mean = all_vals.mean()
        ss_total = np.sum((all_vals - grand_mean) ** 2)
        ss_between = 0
        for label, (n, mean_r) in group_stats.items():
            ss_between += n * (mean_r - grand_mean) ** 2
        eta_sq[h] = ss_between / ss_total if ss_total > 0 else 0

        detail[h] = group_stats

    return {"spearman": spearman, "eta_sq": eta_sq, "detail": detail}


# ═══════════════════════════════════════════════════════════
# 5. 主力意图追踪（连续日聚合）
# ═══════════════════════════════════════════════════════════

def track_capital_intent(df):
    """
    把同一连续阶段段聚合成「事件段」，
    输出每段：起始日、天数、累计大单净流入、段内涨跌幅、段后收益。
    """
    df = df.copy()
    df["phase_group"] = (df["phase"] != df["phase"].shift()).cumsum()

    segments = []
    for gid, group in df.groupby("phase_group"):
        seg = {
            "phase": group["phase"].iloc[0],
            "start": group["date"].iloc[0].strftime("%m-%d"),
            "end": group["date"].iloc[-1].strftime("%m-%d"),
            "days": len(group),
            "ret_in": group["close"].iloc[-1] / group["close"].iloc[0] - 1,
            "cum_big_net": group["big_net"].sum(),
            "cum_big_late": group["big_late_net"].sum(),
            "cum_xl_net": group["xl_net"].sum(),
            "avg_rsi": group["rsi"].mean(),
            "avg_pos": group["pos_20d"].mean(),
        }
        # 段后收益（取段结束后第 N 天）
        last_idx = group.index[-1]
        for n in [1, 3, 5, 10]:
            fut_idx = last_idx + n
            if fut_idx < len(df):
                seg[f"fwd_{n}d"] = df.loc[fut_idx, f"fwd_ret_{n}d"]
            else:
                seg[f"fwd_{n}d"] = np.nan
        segments.append(seg)

    return pd.DataFrame(segments)


# ═══════════════════════════════════════════════════════════
# 6. 主力阶段大类（聚合到 5 大类）
# ═══════════════════════════════════════════════════════════

CATEGORY_MAP = {
    # 背离类（最高优先级，揭示主力真实意图）
    "高位诱多(背离)": "派发",       # xl买+lg卖+高位：造假拉升出货（11天仅1天正收益）
    "放量诱多(背离)": "派发",       # xl买+lg卖+放量：加大诱多力度
    "量价背离(出货)": "派发",       # xl买+lg卖（非高位但信号仍偏空）
    "压盘吸筹(背离)": "吸筹",       # xl卖+lg买+低位：压盘制造恐慌，大单悄悄接（5d均值+2.6%）
    "量价背离(接盘)": "底部",       # xl卖+lg买（非低位）：中线底部试探信号
    # 吸筹类
    "底部吸筹(尾盘突击)": "吸筹",
    "底部吸筹": "吸筹",
    "底部试探": "吸筹",
    "低波吸筹": "吸筹",
    "低波吸筹(尾盘)": "吸筹",
    "下跌中接盘": "吸筹",
    # 派发/顶部类
    "高位派发(加速)": "派发",
    "高位派发": "派发",
    "高位滞涨(尾盘出货)": "派发",
    "高位冲顶(过热)": "派发",
    "高位冲顶(放量)": "派发",
    "高位缩量(危险)": "派发",
    # 拉升类
    "放量拉升": "拉升",
    "稳步拉升": "拉升",
    "温和上涨": "拉升",
    # 缩量退潮类
    "缩量退潮": "退潮",
    "缩量上涨": "退潮",
    "高位弱势": "退潮",
    # 下跌类
    "恐慌出清": "下跌",
    "恐慌出清(高波)": "下跌",
    "持续下跌": "下跌",
    "阴跌": "下跌",
    "阴跌(观望)": "下跌",
    # 盘整类
    "底部磨底": "吸筹",
    "底部观望": "观望",
    "高位观望": "观望",
    "盘整(偏买)": "观望",
    "盘整(偏卖)": "观望",
    "窄幅盘整": "观望",
    "盘整观望": "观望",
}


def add_category(df):
    df = df.copy()
    df["category"] = df["phase"].map(CATEGORY_MAP).fillna("观望")
    return df


# ═══════════════════════════════════════════════════════════
# 7. 主入口
# ═══════════════════════════════════════════════════════════

def main(stock="HK.00700"):
    print(f"\n{'='*70}")
    print(f"  主力行为阶段分析 —— {stock}")
    print(f"{'='*70}\n")

    df = load_daily_features(stock)
    print(f"有效样本: {len(df)} 天  ({df['date'].iloc[0].strftime('%Y-%m-%d')} ~ {df['date'].iloc[-1].strftime('%Y-%m-%d')})")

    df["phase"] = df.apply(classify_phase, axis=1)
    df = add_category(df)

    # ── 1. 阶段分布 ──
    print(f"\n{'─'*70}")
    print("【阶段分布】")
    phase_counts = df["phase"].value_counts()
    for p, c in phase_counts.items():
        cat = CATEGORY_MAP.get(p, "?")
        bar = "█" * max(1, int(c / phase_counts.max() * 30))
        print(f"  {p:<24s} [{cat}] {c:3d} 天  {bar}")

    # ── 2. 大类分布 ──
    print(f"\n{'─'*70}")
    print("【大类分布】")
    cat_counts = df["category"].value_counts()
    for c, n in cat_counts.items():
        bar = "█" * max(1, int(n / cat_counts.max() * 30))
        print(f"  {c:<8s} {n:3d} 天  {bar}")

    # ── 3. 逐日阶段表 ──
    print(f"\n{'─'*70}")
    print("【逐日阶段详情】")
    cols_show = ["date","close","rsi","pos_20d","dist_ma20",
                 "xl_net","lg_net","lg_late_pct","diverge","phase","category"]
    print(df[cols_show].to_string(max_rows=100, formatters={
        "date": lambda x: x.strftime("%m-%d") if hasattr(x, 'strftime') else str(x),
        "close": "{:.1f}".format,
        "rsi": "{:.1f}".format,
        "pos_20d": "{:.2f}".format,
        "dist_ma20": "{:+.2%}".format,
        "xl_net": lambda x: f"{x/1e6:+.0f}M" if abs(x)>1e6 else f"{x/1e6:+.1f}M",
        "lg_net": lambda x: f"{x/1e6:+.0f}M" if abs(x)>1e6 else f"{x/1e6:+.1f}M",
        "lg_late_pct": "{:+.2%}".format,
        "diverge": lambda x: "⚠" if x==1 else "",
    }))

    # ── 4. 阶段收益 ──
    print(f"\n{'─'*70}")
    print("【各阶段未来收益】")
    returns_df = phase_forward_returns(df)
    fmts = {
        "avg_1d": "{:+.2%}", "win_1d": "{:.0%}",
        "avg_3d": "{:+.2%}", "win_3d": "{:.0%}",
        "avg_5d": "{:+.2%}", "win_5d": "{:.0%}",
        "avg_10d": "{:+.2%}", "win_10d": "{:.0%}",
        "avg_20d": "{:+.2%}", "win_20d": "{:.0%}",
    }
    for col, fmt in fmts.items():
        if col in returns_df.columns:
            returns_df[col] = returns_df[col].apply(lambda x: fmt.format(x) if not pd.isna(x) else "N/A")
    print(returns_df.to_string(index=False))

    # ── 4.5. 波动象限分析 ──
    print(f"\n{'─'*70}")
    print("【波动象限分析】高/低波 × 高/低RSI × 大单方向")
    quadrants = {}
    for _, r in df.iterrows():
        vol_label = "高波" if r["volatility_5d"] > r["volatility_10d"] else "低波"
        rsi_label = "高RSI(>50)" if r["rsi"] > 50 else "低RSI(<50)"
        key = f"{vol_label}×{rsi_label}"
        if key not in quadrants:
            quadrants[key] = {"n": 0, "big_net": 0, "ret_5d": [], "ret_10d": [], "ret_next": []}
        quadrants[key]["n"] += 1
        quadrants[key]["big_net"] += r["big_net"]
        if not pd.isna(r.get("fwd_ret_5d")):
            quadrants[key]["ret_5d"].append(r["fwd_ret_5d"])
        if not pd.isna(r.get("fwd_ret_10d")):
            quadrants[key]["ret_10d"].append(r["fwd_ret_10d"])
        if not pd.isna(r.get("ret")):
            quadrants[key]["ret_next"].append(r["ret"])

    for key in sorted(quadrants.keys()):
        q = quadrants[key]
        avg_5 = np.mean(q["ret_5d"]) if q["ret_5d"] else np.nan
        avg_10 = np.mean(q["ret_10d"]) if q["ret_10d"] else np.nan
        avg_next = np.mean(q["ret_next"]) if q["ret_next"] else np.nan
        n_5, n_10 = len(q["ret_5d"]), len(q["ret_10d"])
        print(f"  {key:<26s} {q['n']:2d}天  "
              f"日均大单{q['big_net']/q['n']/1e6:+.1f}M  "
              f"→1d {avg_next:+.2%}  "
              f"→5d {avg_5:+.2%}(n={n_5})  "
              f"→10d {avg_10:+.2%}(n={n_10})")

    # ── 4.6. 分类器评估：排序单调性 + 增量信息检验 ──
    print(f"\n{'─'*70}")
    print("【分类器回测评估】排序单调性(ρ) & 分层解释力(η²)")
    print("  ρ=1.0 → 收益排序完全符合预期方向")
    print("  ρ<0  → 排序反了，分类器方向判断有问题")
    print("  η²  → 组间方差占比，越高说明分类器对收益的分层越强")

    # 生成两个基线的标签
    df["phase_tech"] = df.apply(classify_technical_only, axis=1)
    df["phase_flow"] = df.apply(classify_flow_only, axis=1)

    # 三个分类器的评估
    classifiers = [
        ("我们的分类器", "category", CATEGORY_RANK),
        ("Baseline A 纯技术", "phase_tech", BASELINE_A_RANK),
        ("Baseline B 纯资金", "phase_flow", BASELINE_B_RANK),
    ]
    horizons = [1, 3, 5, 10, 20]
    all_results = {}

    for name, col, rank_map in classifiers:
        result = eval_classifier(df, col, rank_map, horizons)
        all_results[name] = result

    # ── 表头 ──
    header = f"  {'':<20s}"
    for h in horizons:
        header += f"  {'ρ'+str(h)+'d':>8s}  {'η²'+str(h)+'d':>7s}"
    print(header)
    print(f"  {'─'*20}{'─'*17 * len(horizons)}")

    # ── 逐行输出 ──
    for name, col, rank_map in classifiers:
        result = all_results[name]
        if result is None:
            print(f"  {name:<20s}  (样本不足，跳过)")
            continue
        line = f"  {name:<20s}"
        for h in horizons:
            rho = result["spearman"].get(h, (0, 1))[0]
            eta = result["eta_sq"].get(h, 0)
            rho_str = f"{rho:+.2f}" if not np.isnan(rho) else "  N/A"
            eta_str = f"{eta:.3f}" if not np.isnan(eta) else "  N/A"
            line += f"  {rho_str:>8s}  {eta_str:>7s}"
        print(line)

    # ── 增量分析 ──
    print(f"\n  增量分析（vs 纯技术基线）:")
    for h in horizons:
        our_eta = all_results["我们的分类器"]["eta_sq"].get(h, 0)
        tech_eta = all_results["Baseline A 纯技术"]["eta_sq"].get(h, 0)
        flow_eta = all_results["Baseline B 纯资金"]["eta_sq"].get(h, 0)
        delta_vs_tech = our_eta - tech_eta
        delta_vs_flow = our_eta - flow_eta
        tech_str = f"vs技术{delta_vs_tech:+.3f}" if abs(delta_vs_tech) > 0.001 else "vs技术持平"
        flow_str = f"vs资金{delta_vs_flow:+.3f}" if abs(delta_vs_flow) > 0.001 else "vs资金持平"
        print(f"    {h}d: η²={our_eta:.3f}  ({tech_str} | {flow_str})")

    # ── 详细分层（仅我们的分类器） ──
    print(f"\n  各阶段分层详情（我们的分类器）:")
    our_detail = all_results["我们的分类器"]["detail"]
    for h in [5, 10, 20]:
        print(f"    {h:2d}日:")
        groups = our_detail.get(h, {})
        # 按收益降序
        for label in sorted(groups.keys(), key=lambda l: groups[l][1], reverse=True):
            n, avg = groups[label]
            rank = CATEGORY_RANK.get(label, 0)
            arrow = "✓" if (rank <= 3 and avg > 0) or (rank >= 5 and avg < 0) else \
                    ("✗" if (rank <= 3 and avg < 0) or (rank >= 5 and avg > 0) else "~")
            print(f"      {label:<6s} (rank={rank}) n={n:2d}  avg={avg:+.2%}  {arrow}")

    # ── 5. 转移矩阵（大类别→大类别） ──
    for h in [1, 5, 10]:
        print(f"\n{'─'*70}")
        print(f"【大类转移矩阵 ({h}日后)】")
        cat_df = df.copy()
        cat_df["phase"] = cat_df["category"]
        tm = build_transition_matrix(cat_df, h)
        print(tm.to_string(float_format=lambda x: f"{x:.0%}" if x > 0 else "·"))

    # ── 6. 主力意图追踪（事件段） ──
    print(f"\n{'─'*70}")
    print("【主力意图事件段追踪】")
    seg_df = track_capital_intent(df)
    for _, seg in seg_df.iterrows():
        cat = CATEGORY_MAP.get(seg["phase"], "?")
        lg_str = f" LG+{seg['cum_lg_net']/1e6:.1f}M" if abs(seg['cum_lg_net']) > 1e6 else ""
        xl_str = f" XL+{seg['cum_xl_net']/1e6:.1f}M" if abs(seg['cum_xl_net']) > 1e6 else ""
        line = (f"  {seg['phase']:<22s} [{cat}] {seg['start']}→{seg['end']} "
                f"({seg['days']:2d}天) "
                f"段内{seg['ret_in']:+.2%}  "
                f"大单累计{seg['cum_lg_net']/1e6:+.1f}M{lg_str}{xl_str}")
        fwd_10 = seg.get('fwd_10d', np.nan)
        if not pd.isna(fwd_10):
            line += f"  →10d {fwd_10:+.2%}"
        fwd_5 = seg.get('fwd_5d', np.nan)
        if not pd.isna(fwd_5):
            line += f"  →5d {fwd_5:+.2%}"
        print(line)

    # ── 7. 当前状态 ──
    print(f"\n{'─'*70}")
    print("【当前状态】")
    last = df.iloc[-1]
    vol_5 = last.get("volatility_5d", np.nan)
    vol_10 = last.get("volatility_10d", np.nan)
    vol_trend = "收敛" if not pd.isna(vol_5) and not pd.isna(vol_10) and vol_5 < vol_10 else \
                ("扩张" if not pd.isna(vol_5) and not pd.isna(vol_10) and vol_5 > vol_10 else "持平")

    print(f"  日期: {last['date'].strftime('%Y-%m-%d')}")
    print(f"  收盘: {last['close']:.2f}  |  涨跌: {last['ret']:+.2%}")
    print(f"  RSI: {last['rsi']:.1f}  |  20日位置: {last['pos_20d']:.2f}  |  MA20偏离: {last['dist_ma20']:+.2%}")
    print(f"  波动率: 5日{vol_5:.1%} / 10日{vol_10:.1%}  [{vol_trend}]")
    # 分拆展示 xl vs lg
    xl_m = last['xl_net']/1e6
    lg_m = last['lg_net']/1e6
    diverge_flag = " ⚠背离" if last.get('diverge', 0) == 1 else ""
    print(f"  特大单(xl): {xl_m:+.2f}M  |  大单(lg): {lg_m:+.2f}M  |  合计(big): {(xl_m+lg_m):+.2f}M{diverge_flag}")
    print(f"  尾盘大单(lg): {last['lg_late_net']/1e6:+.2f}M  |  尾盘占比(lg): {last['lg_late_pct']:+.2%}")
    print(f"  当前阶段: {last['phase']}  [{CATEGORY_MAP.get(last['phase'], '?')}]")
    print(f"  5日涨跌: {last['ret_5d']:+.2%}  |  量比(20日均): {last['vol_ratio']:.1f}x")

    # ── xl/lg 背离历史参考 ──
    if last.get('diverge', 0) == 1:
        hist_div = df[df['diverge'] == 1]
        if last['xl_net'] > 0 and last['lg_net'] < 0:
            subset = hist_div[(hist_div['xl_net'] > 0) & (hist_div['lg_net'] < 0)]
            print(f"\n  ⚠ 当前为「特大单买+大单卖」背离模式")
            if len(subset) > 1:
                print(f"     历史出现 {len(subset)} 次, 次日均值 {subset['fwd_ret_1d'].dropna().mean():+.2%}"
                      f" / 3日均 {subset['fwd_ret_3d'].dropna().mean():+.2%}"
                      f"  (正次数 {int((subset['fwd_ret_1d'].dropna()>0).sum())}/{len(subset.dropna(subset=['fwd_ret_1d']))})")
        elif last['xl_net'] < 0 and last['lg_net'] > 0:
            subset = hist_div[(hist_div['xl_net'] < 0) & (hist_div['lg_net'] > 0)]
            print(f"\n  ⚡ 当前为「特大单卖+大单买」背离模式（潜在吸筹信号）")
            if len(subset) > 1:
                print(f"     历史出现 {len(subset)} 次, 次日均值 {subset['fwd_ret_1d'].dropna().mean():+.2%}"
                      f" / 5日均 {subset['fwd_ret_5d'].dropna().mean():+.2%}"
                      f"  (正次数 {int((subset['fwd_ret_5d'].dropna()>0).sum())}/{len(subset.dropna(subset=['fwd_ret_5d']))})")

    # ── 顶部风险评估 ──
    print(f"\n  顶部风险评估:")
    risk_signals = []
    if last["pos_20d"] > 0.65:
        risk_signals.append(("高位位置", f"pos={last['pos_20d']:.2f}"))
        # 放量+大单(lg)净卖 = 冲顶出货信号
        if last["vol_ratio"] > 1.4 and last["lg_net"] < 0 and last["lg_net_pct"] < -0.02:
            risk_signals.append(("高位放量出货(冲顶)", f"量比{last['vol_ratio']:.1f}x, 大单{last['lg_net']/1e6:+.1f}M"))
        # xl买+lg卖背离 = 造势出货
        if last.get('diverge', 0) == 1 and last['xl_net'] > 0 and last['lg_net'] < 0:
            risk_signals.append(("特大单买+大单卖背离", f"典型造势出货模式, 历史正次数极低"))
        # 缩量+lg不在场 → 主力离场
        if last["vol_ratio"] < 0.5 and abs(last["lg_net_pct"]) < 0.01:
            risk_signals.append(("高位极度缩量(主力离场)", f"量比仅{last['vol_ratio']:.1f}x"))
        # 查历史：高位冲顶后发生过什么（改用 lg 判断）
        hist_toppers = df[(df["pos_20d"] > 0.65) & (df["vol_ratio"] > 1.3) &
                          (df["lg_net"] < 0) & (df["lg_net_pct"] < -0.02)]
        if len(hist_toppers) > 0:
            fwd5_vals = hist_toppers["fwd_ret_5d"].dropna()
            fwd10_vals = hist_toppers["fwd_ret_10d"].dropna()
            risk_signals.append(("历史高位出货参考", f"{len(hist_toppers)}次, →5d {fwd5_vals.mean():+.2%} / →10d {fwd10_vals.mean():+.2%}"))

    if len(risk_signals) >= 2:
        risk_level = "⚠️ 高风险" if len(risk_signals) >= 3 else "⚡ 注意"
        print(f"    {risk_level}: " + " | ".join(f"{k}:{v}" for k, v in risk_signals))
    elif len(risk_signals) == 1:
        print(f"    低风险: {risk_signals[0][0]}={risk_signals[0][1]}")
    else:
        print(f"    无顶部风险信号")

    # 找历史上相似阶段
    same_phase = df[df["phase"] == last["phase"]]
    if len(same_phase) > 1:
        print(f"\n  历史上出现 {len(same_phase)} 次此阶段:")
        for _, r in same_phase.iterrows():
            fwd_5 = r.get("fwd_ret_5d", np.nan)
            fwd_10 = r.get("fwd_ret_10d", np.nan)
            line = (f"    {r['date'].strftime('%m-%d')}  "
                    f"RSI={r['rsi']:.0f}  pos={r['pos_20d']:.2f}  "
                    f"xl={r['xl_net']/1e6:+.1f}M lg={r['lg_net']/1e6:+.1f}M")
            line += f"  →5d {fwd_5:+.2%}" if not pd.isna(fwd_5) else "  →5d N/A"
            line += f"  10d {fwd_10:+.2%}" if not pd.isna(fwd_10) else "  10d N/A"
            print(line)

    print(f"\n{'='*70}\n")


if __name__ == "__main__":
    stock = sys.argv[1] if len(sys.argv) > 1 else "HK.00700"
    main(stock)
