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
    · **不做「满 52 周」覆盖率门槛**——上市不足一年的股票（如 HK.02513 窗口内仅 165 行）也照常出值，
      用其窗口内的可用历史区间计算位置。本标签定位是「看股价处于自身历史区间的哪一段」，而非严格的
      52 周统计；与个股概览页（直接用 high_52w/low_52w 快照列、不过滤）同口径，过滤太严会让大量
      次新/近期上市股丢失该标签、与页面显示不一致。
    · 仅剔除**退化/空值**样本：区间须有真实宽度（high > low，单日股 high==low 自然排除）、
      端点为正、收盘为正；无需额外的「新鲜度」门槛，停牌几天仍保留、长期退市者因无有效行自然排除。
    · 先剔除坏快照行（high/low/close≤0 或 high<low，港股常见 high=0,low=0 脏数据，
      占比约 2.2%、波及半数港股）再做区间极值计算，否则单条 high=0 让 min(low)=0 毒化整只股票

数据来源：a_daily_quote / hk_daily_quote（high / low / close）
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from ..registry import tag, TIER, UNIT_PCTL
from ._base import _conn, _read_sql, _frame

DOMAIN = "趋势状态"

W_1Y = 252          # 1 年窗口（交易日），与 ⑦ technical.py 同约定；回算区间用，但不再作为纳入门槛
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
    """全市场个股的 52 周位置（0-100），仅剔除退化/空值样本。

    与个股概览页同口径：直接用 (close − low) / (high − low) 给位置，**不要求满 52 周交易史**——
    上市不足一年的股票用其窗口内可用历史区间照样出值（概览页正是如此；过滤太严会让大量次新/
    近期上市股丢失该标签，也背离「看股价处于什么区间」的初衷）。保留基于原始 high/low 的回算
    是为 PIT 正确：快照列 high_52w/low_52w 在 a_daily_quote 历史覆盖≈0，直接读会破坏历史回填。
    """
    with _conn() as conn:
        q = _load_history(conn, as_of)
    if q.empty:
        return pd.DataFrame(columns=["stock_code", "market", "pos"])

    # 先剔除坏快照行：high/low/close<=0 或 high<low（如 HK 某日 high=0,low=0 的脏数据）。
    # 否则单条 high=0 会让 min(low)=0，毒化整只股票的区间把它整只丢掉。
    valid = (q["high"] > 0) & (q["low"] > 0) & (q["close"] > 0) & (q["high"] >= q["low"])
    q = q[valid].sort_values(["stock_code", "trade_date"])

    g = q.groupby("stock_code", sort=False)
    agg = g.agg(hi=("high", "max"), lo=("low", "min"))
    # 末行即 as_of 当日收盘（已按日期升序，tail(1) 取最后一笔）
    agg["close"] = g["close"].last()
    agg["market"] = g["market"].last()

    # 仅剔除退化/空值样本：区间须有真实宽度（high>low）、端点为正、收盘为正。
    # 不再卡「满 252 交易日」覆盖率——否则上市不足一年的股票（如 HK.02513 仅 165 行）整只丢失。
    ok = (
        agg["hi"].notna() & agg["lo"].notna() & agg["close"].notna()
        & (agg["hi"] > agg["lo"]) & (agg["lo"] > 0) & (agg["close"] > 0)
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
        "剔除区间退化（high ≤ low，含单日股）与空值/非正样本，不产出标签值；"
        "不要求满 52 周交易史——上市不足一年的股票用其窗口内可用历史区间出值，"
        "与个股概览页（直接读 high_52w / low_52w 快照列、不过滤）同口径，"
        "避免大量次新/近期上市股丢失该标签。"
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
