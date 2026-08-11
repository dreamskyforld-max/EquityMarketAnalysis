#!/usr/bin/env python3
"""
宏观环境评分 — 股/债/汇三维宏观打分模型。

数据源: daily_benchmark（全球基准指数日频行情，由 get_global_benchmarks.py 采集）。

维度（每维 0-100，分数越高=越宽松/越友好）:
  - 风险偏好 RiskAppetite   : 股市表现 + VIX(反向)
  - 流动性   Liquidity      : 利率(反向) + 美元(反向) + 人民币(反向)
  - 估值分母 Valuation      : 美债10Y(反向) + 美元(反向) + 收益率曲线(正向)

标准化: 对每个指标取过去 WINDOW 个交易日的分位数(20/80 截尾), 映射 0-100。
        正向指标越高分越高; 反向指标越高分越低(score=100-score)。

综合分 = 三维等权平均。标签: <30偏紧 / 30-45紧 / 45-55中性 / 55-70偏松 / >70宽松。

结果落库 macro_environment_score (trade_date PK, 每日 upsert)。

用法:
  python3 macro_environment_score.py            # 评最新交易日
  python3 macro_environment_score.py --date 2026-08-11
  python3 macro_environment_score.py --backfill 60   # 回填最近 N 个交易日
"""
import argparse
import json
from datetime import date, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from db import get_conn, bulk_upsert
from psycopg2.extras import Json

WINDOW = 60  # 分位数回溯窗口（交易日）


# ── 指标模板（不含 dir/w，运行时按个股相关性动态生成）──
# dim: 所属维度
# kind:
#   change = 用当日 change_pct（短期动能，用于风险偏好）
#   zscore = 用当前值相对近期均值(window)的 z 分数偏离（用于流动性/估值分母，
#            业界口径：看利率/美元"在涨还是在跌、偏离均衡多少"，而非 250 天历史分位）
# 派生指标 bench 前缀 "SPREAD." 表示由两支基准计算（如 10Y-2Y 期限利差）。
# 注意: 美债/汇率/利差的 dir 由该股票与指标的 pearson 符号决定（见 build_indicators_for_stock）。
INDICATOR_TEMPLATES: List[Dict] = [
    # 风险偏好（动能）
    {"bench": "HK.800000",  "name": "恒生指数",     "dim": "risk", "kind": "change"},
    {"bench": "HK.800700",  "name": "恒生科技",     "dim": "risk", "kind": "change"},
    {"bench": "US.SP500",   "name": "标普500",      "dim": "risk", "kind": "change"},
    {"bench": "US.NASDAQCOM","name": "纳斯达克",    "dim": "risk", "kind": "change"},
    {"bench": "SH.000001",   "name": "上证指数",     "dim": "risk", "kind": "change"},
    {"bench": "SZ.399001",   "name": "深证成指",     "dim": "risk", "kind": "change"},
    {"bench": "US.VIXCLS",   "name": "VIX",         "dim": "risk", "kind": "change"},
    # 流动性（利率/汇率用相对近期均值的 z 分数偏离，反向=越高越紧）
    #   level_w: 水平偏离分权重; mom_w: 短期动能(变化速度)分权重。两者合计=1。
    #   动能解决"收益率持续上涨已久→z偏离小→分虚高"的盲区: 近期加速上行直接扣分。
    {"bench": "US.DGS10",    "name": "美债10Y",     "dim": "liq",  "kind": "zscore", "window": 120,
     "momentum": True,  "mom_window": 20, "level_w": 0.6, "mom_w": 0.4},
    {"bench": "US.DGS2",     "name": "美债2Y",      "dim": "liq",  "kind": "zscore", "window": 120,
     "momentum": True,  "mom_window": 20, "level_w": 0.6, "mom_w": 0.4},
    {"bench": "US.DTWEXBGS", "name": "美元指数",    "dim": "liq",  "kind": "zscore", "window": 120,
     "momentum": True,  "mom_window": 20, "level_w": 0.6, "mom_w": 0.4},
    {"bench": "FX.USDCNY",   "name": "离岸人民币",  "dim": "liq",  "kind": "zscore", "window": 120,
     "momentum": True,  "mom_window": 20, "level_w": 0.6, "mom_w": 0.4},
    # 估值分母（折现率 + 美元 + 期限利差）
    {"bench": "US.DGS10",    "name": "美债10Y",     "dim": "val",  "kind": "zscore", "window": 120},
    {"bench": "US.DTWEXBGS", "name": "美元指数",    "dim": "val",  "kind": "zscore", "window": 120},
    {"bench": "SPREAD.10Y2Y","name": "10Y-2Y利差",  "dim": "val",  "kind": "zscore", "window": 120},
]

# 默认方向(无相关性数据时的兜底): 正向指标越高=越松; 负向指标(利率/美元/汇率/VIX)越高=越紧
_DEFAULT_DIR = {
    "HK.800000": +1, "HK.800700": +1, "US.SP500": +1, "US.NASDAQCOM": +1,
    "SH.000001": +1, "SZ.399001": +1, "US.VIXCLS": -1,
    "US.DGS10": -1, "US.DGS2": -1, "US.DTWEXBGS": -1, "FX.USDCNY": -1,
}

# 相关性弱于此阈值(绝对值)的指标, 权重衰减为 MIN_W, 避免噪声主导评分
_WEAK_R = 0.10
_MIN_W = 0.05


def load_benchmark_history() -> pd.DataFrame:
    """读取 daily_benchmark 全历史 (bench_code, trade_date 排序)。"""
    with get_conn() as c:
        df = pd.read_sql(
            "SELECT bench_code, bench_name, trade_date, change_pct, last_price, close_20d_ago "
            "FROM daily_benchmark ORDER BY bench_code, trade_date",
            c,
        )
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def load_correlation(stock_code: str, calc_date: date) -> Dict[str, float]:
    """读取该股票与各个宏观指标的最新 pearson 相关系数。

    返回 {bench_code: pearson}。用于决定指标方向与权重。
    若某指标无相关性记录, 回退到 _DEFAULT_DIR 的方向, 权重用兜底值。
    """
    with get_conn() as c:
        df = pd.read_sql(
            "SELECT bench_code, pearson FROM benchmark_correlation "
            "WHERE stock_code=%s AND calc_date=%s",
            c, params=(stock_code, calc_date),
        )
    return {r["bench_code"]: (None if pd.isna(r["pearson"]) else float(r["pearson"]))
            for _, r in df.iterrows()}


def build_indicators_for_stock(stock_code: str, calc_date: date):
    """按个股与宏观指标的相关性动态生成 INDICATORS。

    返回 (indicators, has_corr):
    - dir: 由 pearson 符号决定 (正→+1 正向, 负→-1 反向)。
    - w:   维度内按 |pearson| 归一化; |pearson|<_WEAK_R 时权衰减为 _MIN_W。
    - has_corr: 是否读到该股票的相关性数据。无数据时 indicators 仍用 _DEFAULT_DIR
               兜底（平权默认方向），但 has_corr=False 供调用方决定是否跳过。
    """
    corr = load_correlation(stock_code, calc_date)
    has_corr = bool(corr)

    # 先按模板生成带原始权重的候选, 再在维度内归一化
    candidates = []
    for t in INDICATOR_TEMPLATES:
        bc = t["bench"]
        r = corr.get(bc)
        if r is None:
            # 无相关性数据: 用默认方向 + 中等权重(0.2)
            d = _DEFAULT_DIR.get(bc, +1)
            raw_w = 0.20
        else:
            d = +1 if r >= 0 else -1
            raw_w = _MIN_W if abs(r) < _WEAK_R else abs(r)
        candidates.append({**t, "dir": d, "w": raw_w})

    # 维度内归一化权重
    dim_sum: Dict[str, float] = {}
    for c in candidates:
        dim_sum[c["dim"]] = dim_sum.get(c["dim"], 0.0) + c["w"]
    for c in candidates:
        s = dim_sum.get(c["dim"], 0.0)
        c["w"] = round(c["w"] / s, 4) if s else 0.0
    return candidates, has_corr


def _value_for(ind: Dict, row: pd.Series) -> Optional[float]:
    """取指标的"原始当前值":
    - change: 当日 change_pct
    - zscore/level: 当日 last_price（z-score 在 score_one_day 内做窗口标准化）
    - SPREAD.10Y2Y: 由当日 DGS10 - DGS2 派生（需 row 含两列，见 _spread_row）
    """
    if ind["bench"].startswith("SPREAD."):
        a, b = row.get("_sub_a"), row.get("_sub_b")
        if a is None or b is None:
            return None
        return float(a) - float(b)
    if ind["kind"] == "change":
        v = row.get("change_pct")
    else:  # zscore / level 都用 last_price 作为原始水平
        v = row.get("last_price")
    return None if (v is None or pd.isna(v)) else float(v)


def _score_from_series(current: float, hist: pd.Series, direction: int, kind: str) -> float:
    """把当前值相对历史标准化到 0-100。
    - change/level: 20/80 分位数截尾
    - zscore: (current - mean) / std, 再线性映射到 0-100 (±2σ 截断)
    direction<0 时翻转(100-score)。
    """
    vals = hist.dropna().astype(float)
    if len(vals) < 5:
        return 50.0
    if kind == "zscore":
        mu, sd = float(vals.mean()), float(vals.std())
        if sd == 0 or np.isnan(sd):
            s = 50.0
        else:
            z = (current - mu) / sd
            s = 50.0 + z * 25.0          # 1σ ≈ 25 分
            s = max(0.0, min(100.0, s))
    else:
        lo, hi = np.percentile(vals, 20), np.percentile(vals, 80)
        if hi == lo:
            s = 50.0
        else:
            s = (current - lo) / (hi - lo) * 100.0
            s = max(0.0, min(100.0, s))
    if direction < 0:
        s = 100.0 - s
    return round(s, 1)


def _momentum_score(bench: pd.DataFrame, bench_code: str, trade_date: date,
                     window: int, direction: int) -> Optional[float]:
    """短期变化动能分（业界"变化速度"维度）。

    取最近 window 个交易日的累计变化量（美债用 bp、美元/人民币用 %），
    相对自身过去变化序列做 z-score，再映射 0-100（1σ≈25分）。
    direction<0 时翻转（如美债收益率上行=利空→低分）。
    返回 None 表示数据不足。
    """
    sub = bench[(bench["bench_code"] == bench_code) &
                (bench["trade_date"] <= pd.Timestamp(trade_date))]
    if sub.empty or len(sub) < window + 5:
        return None
    prices = sub["last_price"].astype(float).reset_index(drop=True)
    # 累计变化序列：每点 = 距 window 日前的变化
    changes = prices.diff(window)
    changes = changes.dropna()
    if len(changes) < 5:
        return None
    cur_chg = float(changes.iloc[-1])
    hist = changes.iloc[:-1]
    mu, sd = float(hist.mean()), float(hist.std())
    if sd == 0 or np.isnan(sd):
        s = 50.0
    else:
        z = (cur_chg - mu) / sd
        s = 50.0 + z * 25.0
        s = max(0.0, min(100.0, s))
    if direction < 0:
        s = 100.0 - s
    return round(s, 1)


def score_one_day(stock_code: str, trade_date: date, bench: pd.DataFrame,
                   indicators: List[Dict]) -> Optional[Dict]:
    """计算单日三维评分(针对单只股票)。返回落库行 dict 或 None (数据不足)。"""
    # 各指标当日值
    latest = {}
    for ind in indicators:
        bc = ind["bench"]
        # SPREAD 派生: 需要从两根基准拼出含两列的 row
        if bc.startswith("SPREAD."):
            pair = bc.split(".", 1)[1]      # e.g. "10Y2Y"
            a, b = "US.DGS10", "US.DGS2"    # 仅支持 10Y-2Y 利差
            sa = bench[(bench["bench_code"] == a) & (bench["trade_date"] <= pd.Timestamp(trade_date))]
            sb = bench[(bench["bench_code"] == b) & (bench["trade_date"] <= pd.Timestamp(trade_date))]
            if sa.empty or sb.empty:
                continue
            cur = sa.iloc[-1].copy()
            cur["_sub_a"], cur["_sub_b"] = sa.iloc[-1]["last_price"], sb.iloc[-1]["last_price"]
            cur["_a"], cur["_b"] = a, b
            ind = {**ind, "_sub_a": "_sub_a", "_sub_b": "_sub_b"}
            sub = sa  # 仅用于取窗口长度, 真实历史由下方分别取
            hist_a = sa.iloc[:-1].tail(ind.get("window", WINDOW))
            hist_b = sb.iloc[:-1].tail(ind.get("window", WINDOW))
            if hist_a.empty or hist_b.empty:
                continue
            hv = pd.Series([
                (getattr(ra, "last_price") - getattr(rb, "last_price"))
                for ra, rb in zip(hist_a.itertuples(), hist_b.itertuples())
            ])
        else:
            sub = bench[(bench["bench_code"] == bc) & (bench["trade_date"] <= pd.Timestamp(trade_date))]
            if sub.empty:
                continue
            cur = sub.iloc[-1]
            hist = sub.iloc[:-1].tail(ind.get("window", WINDOW))
            if hist.empty:
                continue
            hv = hist.apply(lambda r: _value_for(ind, r), axis=1).dropna()

        v = _value_for(ind, cur)
        if v is None or hv.dropna().empty:
            continue
        s = _score_from_series(v, hv, ind["dir"], ind["kind"])
        # 流动性维度: 若配置了短期动能, 将水平偏离分与动能分按权重合并
        if ind.get("momentum") and ind["dim"] == "liq":
            ms = _momentum_score(bench, bc, trade_date,
                                 ind.get("mom_window", 20), ind["dir"])
            if ms is not None:
                lw = ind.get("level_w", 0.6)
                mw = ind.get("mom_w", 0.4)
                s = round(s * lw + ms * mw, 1)
        latest[(ind["dim"], ind["name"])] = (s, ind["w"])

    if not latest:
        return None

    # 按维度加权
    dim_scores: Dict[str, float] = {}
    dim_label = {"risk": "风险偏好", "liq": "流动性", "val": "估值分母"}
    for dim in ("risk", "liq", "val"):
        items = [(sc, w) for (d, _), (sc, w) in latest.items() if d == dim]
        if not items:
            continue
        wsum = sum(w for _, w in items)
        dim_scores[dim] = round(sum(sc * w for sc, w in items) / wsum, 1) if wsum else 50.0

    if len(dim_scores) < 2:
        return None

    total = round(sum(dim_scores.values()) / len(dim_scores), 1)
    label = _label(total)
    summary = _build_summary(dim_scores, label, bench, trade_date)
    detail = {dim_label[d]: {"score": s, "components": {
        n: sc for (dm, n), (sc, _) in latest.items() if dm == d}}
        for d, s in dim_scores.items()}

    # 各维度中文解释
    risk_note = _dim_note("risk", dim_scores.get("risk"),
                          {n: sc for (dm, n), (sc, _) in latest.items() if dm == "risk"})
    liq_note = _dim_note("liq", dim_scores.get("liq"),
                         {n: sc for (dm, n), (sc, _) in latest.items() if dm == "liq"})
    val_note = _dim_note("val", dim_scores.get("val"),
                         {n: sc for (dm, n), (sc, _) in latest.items() if dm == "val"})

    return {
        "stock_code": stock_code,
        "trade_date": trade_date,
        "total_score": total,
        "risk_score": dim_scores.get("risk"),
        "liquidity_score": dim_scores.get("liq"),
        "valuation_score": dim_scores.get("val"),
        "label": label,
        "summary": summary,
        "risk_note": risk_note,
        "liquidity_note": liq_note,
        "valuation_note": val_note,
        "detail_json": Json(detail),
    }


def _label(score: float) -> str:
    if score < 30:
        return "偏紧"
    if score < 45:
        return "紧"
    if score <= 55:
        return "中性"
    if score <= 70:
        return "偏松"
    return "宽松"


def _build_summary(dim_scores: Dict[str, float], label: str, bench: pd.DataFrame,
                   trade_date: date) -> str:
    """规则模板生成一句话结论。"""
    risk = dim_scores.get("risk")
    liq = dim_scores.get("liq")
    val = dim_scores.get("val")

    parts = []
    # 风险偏好
    if risk is not None:
        if risk >= 55:
            parts.append("股市情绪偏强")
        elif risk <= 45:
            parts.append("股市情绪偏弱")
    # 流动性 / 估值分母
    tight = []
    if liq is not None and liq <= 45:
        tight.append("流动性偏紧")
    if val is not None and val <= 45:
        tight.append("估值分母承压")
    if tight:
        parts.append("但" + "、".join(tight))
        # 关键驱动: 美债10Y 是否在 4.2% 上方
        dgs10 = bench[(bench["bench_code"] == "US.DGS10") &
                      (bench["trade_date"] <= pd.Timestamp(trade_date))]
        if not dgs10.empty:
            y10 = float(dgs10.iloc[-1]["last_price"])
            if y10 >= 4.2:
                parts.append(f"美债10Y({y10:.2f}%)高位压制估值")
                parts.append("关注能否回落至4.2%下方")
    if not parts:
        parts.append("各维度均衡")
    return "，".join(parts) + "。"


def _dim_note(dim: str, score: Optional[float],
              components: Dict[str, float]) -> str:
    """生成单个维度的中文解释（基于分数 + 成分明细）。

    components: {指标名: 得分}; 得分越高=越友好。
    逻辑: 找出该维度内得分最低(最拖累)与最高(最支撑)的指标, 结合分数档位措辞。
    """
    if score is None or not components:
        return "数据不足"
    # 排序: 升序(最不利在前)
    ordered = sorted(components.items(), key=lambda kv: kv[1])
    lo_name, lo_sc = ordered[0]
    hi_name, hi_sc = ordered[-1]

    if score >= 55:
        band = "偏松/友好"
    elif score <= 45:
        band = "偏紧/不利"
    else:
        band = "中性"
        return f"中性（{hi_name}偏友好、{lo_name}偏弱，多空大致抵消）。"

    # 找出拖累项(<=40)与支撑项(>=60)
    drags = [n for n, s in ordered if s <= 40]
    props = [n for n, s in ordered if s >= 60]
    if band == "偏松/友好":
        if props:
            return f"偏松（主要受{('、'.join(props))}支撑，环境友好）。"
        return f"偏松（{hi_name}较强）。"
    else:  # 偏紧/不利
        if drags:
            return f"偏紧（受{('、'.join(drags))}拖累，环境不利）。"
        return f"偏紧（{lo_name}偏弱）。"


def ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS macro_environment_score (
            stock_code       TEXT    NOT NULL,
            trade_date        DATE    NOT NULL,
            total_score       DOUBLE PRECISION,
            risk_score        DOUBLE PRECISION,
            liquidity_score   DOUBLE PRECISION,
            valuation_score   DOUBLE PRECISION,
            label             TEXT,
            summary           TEXT,
            risk_note         TEXT,
            liquidity_note    TEXT,
            valuation_note    TEXT,
            detail_json       JSONB,
            updated_at        TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (stock_code, trade_date)
        )
        """)
        conn.commit()


def _to_py(v):
    """numpy 标量 / NaN / inf → Python 原生类型（psycopg2 不可识别 np.float64）。"""
    if v is None:
        return None
    if isinstance(v, dict):
        return {k: _to_py(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_to_py(x) for x in v]
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if (f != f or f in (float("inf"), float("-inf"))) else f
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    return v


def save_scores(rows: List[Dict]) -> int:
    if not rows:
        return 0
    rows = [_to_py(r) for r in rows]
    with get_conn() as conn:
        ensure_table(conn)
        bulk_upsert(conn, "macro_environment_score", rows, conflict_cols=["stock_code", "trade_date"])
    print(f"✅ 已落库 {len(rows)} 行 → macro_environment_score")
    return len(rows)


def main(stock_code: str = "HK.00700", target_date: Optional[str] = None,
         backfill: int = 0, fallback: bool = True) -> None:
    """计算并落库某只股票的宏观环境评分。

    fallback: 当该股票无 benchmark_correlation 数据时——
      True  (手动 --stock 默认): 用 _DEFAULT_DIR 平权兜底跑（标注来源）。
      False (scheduler 自动 run): 跳过，避免写出与定制无关的雷同通用分。
    """
    bench = load_benchmark_history()
    if bench.empty:
        print("❌ daily_benchmark 无数据，请先跑 get_global_benchmarks.py")
        return

    all_dates = sorted(bench["trade_date"].dt.date.unique())
    if target_date:
        dates = [pd.Timestamp(target_date).date()]
    elif backfill:
        dates = all_dates[-backfill:]
    else:
        dates = [all_dates[-1]]

    rows = []
    for d in dates:
        # 用该股票当日最新相关性记录决定指标方向/权重
        indicators, has_corr = build_indicators_for_stock(stock_code, d)
        if not has_corr and not fallback:
            print(f"  {stock_code} {d}  无相关性数据，跳过（需先跑 benchmark_correlation_daily.py）")
            continue
        r = score_one_day(stock_code, d, bench, indicators)
        if r:
            rows.append(r)
            tag = "" if has_corr else " [默认权重建模]"
            print(f"  {stock_code} {d}  综合={r['total_score']}  风险={r['risk_score']}  "
                  f"流动={r['liquidity_score']} 估值={r['valuation_score']}  "
                  f"[{r['label']}]{tag}  {r['summary']}")
            print(f"     风险偏好: {r['risk_note']}")
            print(f"     流动性:   {r['liquidity_note']}")
            print(f"     估值分母: {r['valuation_note']}")
        else:
            print(f"  {stock_code} {d}  数据不足，跳过")
    save_scores(rows)


def run(codes=None, ctx=None):
    """常驻调用入口（由 market_scheduler 通过 collector_runtime 调用）。

    codes: 股票代码列表; 为空时回退到默认 HK.00700。
    ctx: 共享行情上下文（本分析仅读数据库，不使用，保留以对齐常驻入口约定）。
    """
    targets = codes if codes else ["HK.00700"]
    seen = set()
    for sc in targets:
        if sc in seen:
            continue
        seen.add(sc)
        try:
            # 自动模式: 无相关性数据的票跳过(避免雷同通用分)
            main(sc, fallback=False)
        except Exception as e:  # 单只失败不影响其他标的
            print(f"❌ {sc} 宏观环境评分失败: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="宏观环境三维评分（按个股相关性加权）")
    ap.add_argument("--stock", default="HK.00700", help="股票代码，如 HK.00700")
    ap.add_argument("--date", help="指定交易日 YYYY-MM-DD")
    ap.add_argument("--backfill", type=int, default=0, help="回填最近 N 个交易日")
    args = ap.parse_args()
    main(args.stock, args.date, args.backfill, fallback=True)
