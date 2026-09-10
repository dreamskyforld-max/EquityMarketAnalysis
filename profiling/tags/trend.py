#!/usr/bin/env python3
"""⑩ 趋势状态标签域

域定义：回答「个股当前处在走势的什么阶段」。
判定标准：由**量价走势的可观测结构**判定；用**个股时序分位**（自己跟自己的历史区间比），
而非 ⑦ 交易特征的横截面分位；同一时点互斥（见下方 tst_stage 说明）。

与 ⑦ 交易特征的分工（重要）：
    ⑦ 是「要素测量」——横截面统计暴露，不互斥、确定性、无需验证（温度/湿度/风速）
    ⑩ 是「状态判断」——个股时序状态机，互斥、判断性、需验证闭环（晴/雨/多云）
    ⑩ 的判定不读取 ⑦ 的分档结果，两者交叉组合产生增量筛选价值。

为什么本域判定标准与 ⑦ 不同：
    ⑦ 明确「采用横截面分位（同日全市场比较）」，回答的是「在同类中排第几」；
    ⑩ 明确「个股时序分位（自己跟自己历史比）」，回答的是「自己处在自身区间的哪一段」。
    两者虽都叫「波动率 / 动量 / 位置」，但计算口径完全不同，不可混用。

建设进度（本域是唯一需要「无前视定义 + 前瞻收益验证」闭环的橙域，全量落地较晚）：
    ✅ tst_price_position_52w  位置要素：确定性规则，可由量价直接算出
    ⏳ tst_stage  6 阶段状态机（低位吸筹 / 拉升初期 / 上升趋势确认 / 高位派发 / 下跌 / 震荡）
       待建：需走「规则标注 → 前瞻收益验证(t+20 / t+60) → 回改规则」迭代至显著分离，
       短期无法收敛，故暂不注册（若注册须用 status=planned_no_data + blocked_reason）

    ⇒ 先落「股价 52 周位置」这条共享输入，收益是双重的：
      ① 让 ⑩ 立刻具备可用性——已可独立筛选，也可与其他域交叉；
      ② 6 阶段定义里各自重复的「价格分位 <30% / >80%」有了统一口径来源，避免将来各写一套。

口径约定：
    · 位置 = (收盘价 − 52周最低) / (52周最高 − 52周最低) × 100，落在 0-100
    · **窗口自己回算，不读 high_52w / low_52w 快照列**——快照列在 a_daily_quote 只回填了最近
      十几个交易日（历史覆盖≈0，详见 _load_history 注释），直接读会让本标签在绝大部分历史日期
      静默产出空值，回测时才发现；自己算则任意历史 as_of 都成立
    · 窗口统一按**交易日**计（与 ⑦ technical.py 一致）：252 交易日≈1 年
      供应商快照列按自然 52 周算，故两者会有细微差异（实测位置差中位数 0.09pp，可接受）
    · 最高/最低用**日内 high / low**（标准的 52 周高低点定义），收盘位置用 close
    · 采用**个股自身的绝对区间**分档，而不是市场内五等分：本域判定标准是「个股跟自己比」，
      做成横截面就退化成 ⑦ 的口径了；也不做市场分组，A 股与港股位置值可直接横向比较
    · 区间宽度取 20 个百分点等宽，与 6 阶段内部锚点相容：30% 落在档 2，80% 恰为档 5 下沿
    · 边界左闭右开：[0,20)→1 … [80,100]→5，pos 先 clip 到 [0,100] 兜住数据滞后造成的越界
    · **窗口不足 1 年不打标签**（次新股没有真正的「52 周」区间，标了名不副实；
      与 ⑦ 动量档要求 n>252 的门槛保持一致）
    · 剔除区间退化（high ≤ low）与空值样本

MIN_OBS 门槛的三重作用（2026-09-09 实测）：同时过滤掉了三类不该打标签的标的
    ① 次新股（港股窝轮/牛熊证最典型：如 HK.02900 全生命周期仅 24 行，天然无 52 周区间）
    ② 长期停牌后复牌者（如 HK.00374 窗口内仅 48 行）
    ③ 已停止交易者——停牌/退市后不再累积行数，很快跌破 252
    ⇒ 实测通关的 6215 只里 6214 只在 as_of 当日有成交、无一只滞后超过 20 天，
      故本标签无需额外的「新鲜度」过滤逻辑

数据来源：a_daily_quote / hk_daily_quote（high / low / close）
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from ..registry import tag, TIER, UNIT_PCTL
from ._base import _conn, _read_sql, _frame

DOMAIN = "趋势状态"

W_1Y = 252          # 1 年窗口（交易日），与 ⑦ technical.py 同约定
MIN_OBS = W_1Y      # 窗口完整度门槛：不足 1 年不打标签
_W = 20             # 位置区间宽度（百分点）：0-20 / 20-40 / 40-60 / 60-80 / 80-100

_QUOTE_TABLES = (("A", "a_daily_quote"), ("HK", "hk_daily_quote"))

# 位置区间 → 档次说明。注意这里的 20% 是**价格位置区间的宽度**，
# 不是 ⑦ 那种「占全市场 20% 的股票」的横截面口径，故写全区间避免误读。
_POSITION_RANGE = {
    "1": "最低区间 0-20%（贴近 52 周低点）",
    "2": "次低区间 20-40%",
    "3": "中间区间 40-60%",
    "4": "次高区间 60-80%",
    "5": "最高区间 80-100%（贴近 52 周高点）",
}


def _load_history(conn, as_of: date) -> pd.DataFrame:
    """加载截止 as_of 的近一年个股行情（high / low / close）。

    交易日历从 a_daily_quote 推，限定精确窗口，避免全表扫描。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MIN(d) FROM (SELECT DISTINCT trade_date d FROM a_daily_quote "
            "WHERE trade_date <= %s ORDER BY d DESC LIMIT %s) t",
            (as_of, W_1Y + 1),
        )
        start = cur.fetchone()[0]
    if start is None:
        return pd.DataFrame()

    frames = []
    for mkt, table in _QUOTE_TABLES:
        f = _read_sql(
            conn,
            f"SELECT stock_code, trade_date, high::float8 AS high, low::float8 AS low, "
            f"close::float8 AS close FROM {table} WHERE trade_date BETWEEN %s AND %s",
            (start, as_of),
        )
        f["market"] = mkt
        frames.append(f)
    return pd.concat(frames, ignore_index=True)


def _positions(as_of: date) -> pd.DataFrame:
    """全市场个股的 52 周位置（0-100），无效样本已剔除。"""
    with _conn() as conn:
        q = _load_history(conn, as_of)
    if q.empty:
        return pd.DataFrame(columns=["stock_code", "market", "pos"])

    q = q.sort_values(["stock_code", "trade_date"])
    g = q.groupby("stock_code", sort=False)
    agg = g.agg(n=("close", "size"), hi=("high", "max"), lo=("low", "min"))
    # 末行即 as_of 当日收盘（已按日期升序，tail(1) 取最后一笔）
    agg["close"] = g["close"].last()
    agg["market"] = g["market"].last()

    ok = (
        agg["hi"].notna() & agg["lo"].notna() & agg["close"].notna()
        & (agg["n"] >= MIN_OBS) & (agg["hi"] > agg["lo"]) & (agg["lo"] > 0) & (agg["close"] > 0)
    )
    agg = agg[ok]
    if agg.empty:
        return pd.DataFrame(columns=["stock_code", "market", "pos"])

    pos = (agg["close"] - agg["lo"]) / (agg["hi"] - agg["lo"]) * 100
    agg["pos"] = pos.clip(lower=0.0, upper=100.0)
    return agg.reset_index()


# ⚠️ 本标签刻意保持 is_exclusive=False / confidence_req=False（默认值）。
# 这两个标记在设计稿里是留给将来的 tst_stage（6 阶段状态机）的识别信号：
#   is_exclusive    标志着「这就是那个互斥状态机」
#   confidence_req  标志着「判断性标签，必须带置信度」
# 位置档是**确定性的要素**（给定量价即唯一确定，置信度 1.0），虽然同时点也只有一个取值，
# 但它不是状态机本身；占掉这两个标记会让将来真正的阶段标签失去区分手段。
@tag(
    code="tst_price_position_52w", name="股价52周位置档", domain=DOMAIN,
    num_unit=UNIT_PCTL,
    value_type=TIER, value_range=dict(_POSITION_RANGE),
    source_type="rule", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote"],
    compute_logic=(
        "股价52周位置 = (收盘价 − 窗口最低价) / (窗口最高价 − 窗口最低价) × 100，落在 0-100；"
        "窗口 = 截至 as_of 的近 252 个交易日，最高/最低取日内 high / low，收盘取当日 close。"
        "窗口自行回算而非读取 high_52w / low_52w 快照列（该列在 A 股仅回填最近十几个交易日，"
        "历史覆盖≈0，直接读会导致绝大部分历史日期静默产出空值）。"
        "按 20 个百分点等宽切五档：1=[0,20)，2=[20,40)，3=[40,60)，4=[60,80)，5=[80,100]，"
        "num_value 保留连续位置值以免分档损失精度。"
        "采用个股自身区间的绝对分档（本域判定标准），而非 ⑦ 的市场内五等分；"
        "不做市场分组，A 股与港股位置值可直接横向比较。"
        "剔除窗口不足 252 个交易日（次新股无真正 52 周区间）、"
        "区间退化（high ≤ low）与空值样本，不产出标签值。"
    ),
    pit_capable=True, owner="profiling",
)
def tst_price_position_52w(as_of: date) -> pd.DataFrame:
    df = _positions(as_of)
    if df.empty:
        return _frame([], [])
    # 左闭右开分档；pos 已 clip 到 [0,100]，100 会被 100/20+1=6 溢出，故再夹到 5
    band = np.minimum(np.floor(df["pos"].to_numpy() / _W) + 1, 5).astype(int)
    return _frame(df["stock_code"], band.astype(str), df["pos"].round(2))
