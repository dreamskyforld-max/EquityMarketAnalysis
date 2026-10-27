"""多空力量回合（force dynamics）· 状态机与指标计算（纯函数模块）

**口径唯一真源**：`stock-realtime/docs/force_dynamics_design.md`（下称「设计文档」）。
本文件是该文档的唯一生产实现；`stock-realtime/scripts/probe_force_dynamics.py` 是
降级后的研究脚本（保留探索用，不再承担口径定义），两边若漂移以本文件为准。

为什么按「回合」而不是「日」或「活跃簇」
----------------------------------------
- 日级聚合掩盖博弈的阵发性（10-21 全天净额 -20.7 亿，攻击只集中在 11 个时段）。
- 按「活跃分钟」切簇会把一个完整攻防过程切断（10-21 的 10:20 进攻 → 10:31 反击
  被 10:23-10:28 的间歇切成两段）。
- 因此按**相位**切回合：攻方发起 → 守方阻击 → 力量均衡（暂停）。

相位判据（设计文档 §1）
----------------------
进攻启动 : G >= G_act 且 R <= R_ATTACK 且 |net|/G >= IMB_ATTACK
阻击     : 守方该分钟绝对量 >= DEFEND_MULT × 前 10 分钟同向滚动中位数
均衡     : R >= R_BALANCE 且 |net|/G <= IMB_BALANCE，连续 BALANCE_HOLD 分钟
静默收口 : 连续 QUIET_MIN 分钟不再活跃
攻守易位 : 回合内出现反向进攻启动 → 收口当前回合并开启新回合

输入（分钟面板 rows，由调用方构建，本模块不做任何 IO）
------------------------------------------------------
    [{"t": "HH:MM", "price": float, "buy": float, "sell": float,
      "tiers": {"super": (in, out), "big": (...), "mid": (...), "small": (...)}}, ...]
buy/sell/tiers 均为**元**（成交额）；t 为 "HH:MM" 字符串，需按交易日连续。

输出（回合列表，每回合一个 dict）—— **全部为胜方视角**
--------------------------------
t0/t1/minutes          时段、时长
win_side               胜方：ΔP>0 → +1（多），ΔP<0 → -1（空），ΔP=0 → 0
                       「胜方」= 最终影响股价走向的一方（唯一视角，无攻守二元对立）
p0/p1/dp               起点价 / 终点价 / 位移（带符号）
inv_win/inv_lose       胜方投入 / 负方投入（均取该方向**全部档位主动成交额**）
eff / eff_pct          边际效率 λ（元/亿）/ λ_pct（每亿元推动的百分点，跨票口径）
peak_win/keep          胜方方向上的最大位移 / 冲击留存 = |ΔP| ÷ peak_win（0~1，恒正）
amplitude              回合最大振幅（不分方向，辅助观察激烈程度）
lq                     对手盘厚度 = 负方投入 ÷ 胜方投入
lq_main                同上，分母改用「特大+大」口径（仅诊断）
defend_max             负方量能相对其基线的最大放大倍数（负方是否反击）
r_bal                  均衡确认时的对称度
end                    均衡 / 静默 / 易位 / 超时 / 收盘

> 「胜负」标签已移除（2026-10-26）：胜方由 ΔP 符号定义，再输出「胜/弱/负」是冗余的；
> 回合内是否打得干净由 `keep`（冲击留存）这一连续量表达，不再分档。

调用方
------
`build_force_rounds.py`（盘中每分钟跑批落库 force_round_daily，单轮只算当天；
历史日期漏跑用 `python3 build_force_rounds.py --days N` 回填）。

窗口来源（2026-10-26 变更）
--------------------------
生产路径 = `rounds_by_trend`：窗口由**股价趋势段**决定（`trend_segmenter.layer1_segments`
切上涨/下跌/横盘大段），再叠加 `trend_segmenter.split_big_accel` 把「缓→急」的大加速
单独切出（每段最多一刀）；指标仍由 `_close` 计算。
`detect_rounds`（资金相位状态机）**已退役**，仅保留供与历史结论对照。
"""
from __future__ import annotations

import numpy as np

import trend_segmenter

TIERS = ["super", "big", "mid", "small"]

# —— 相位判据（设计文档 §1，调参前先读该节的边界稳定性结论）——
G_ACT_PCT = 75        # 活跃阈值：当日 G 的百分位
G_ACT_FLOOR = 0.3e8   # 活跃下限（元），防极缩量日大量伪回合
R_ATTACK = 0.35       # 单边（进攻）对称度上限
IMB_ATTACK = 0.5      # 进攻失衡下限 |net|/G
R_BALANCE = 0.75      # 均衡对称度下限
IMB_BALANCE = 0.3     # 均衡失衡上限
DEFEND_MULT = 3.0     # 守方量能放大倍数（相对前 10 分钟同向中位数）
DEFEND_WIN = 10       # 阻击倍数的滚动窗口（分钟）
BALANCE_HOLD = 2      # 均衡需连续确认的分钟数
QUIET_MIN = 5         # 连续 N 分钟不活跃即判定「静默收口」
MAX_ROUND_MIN = 90    # 单回合最长分钟数（兜底）
NEUTRAL_PCT = 0.001     # 「无胜方」中性区间：|ΔP| ÷ 起点价 < 0.1% 时判定为横盘，不指定胜方
                        # 依据：微小位移强行归给某一方会让「胜方」语义在震荡市失真
                        # （10-21 11:59-13:06 位移仅 -0.2 元/-0.05%，实为横盘微跌）


def _roll_median(vals: list[float], i: int, w: int) -> float:
    prev = vals[max(0, i - w):i]
    return float(np.median(prev)) if prev else 0.0


def _sym(r: dict) -> float:
    """对称度 R = min(买,卖)/max(买,卖)。R→1 双方量能对等，R→0 完全单边。"""
    lo, hi = min(r["buy"], r["sell"]), max(r["buy"], r["sell"])
    return (lo / hi) if hi > 0 else 0.0


def _close(rows: list[dict], cur: dict, reason: str,
           buys: list[float], sells: list[float]) -> dict:
    """收口一个回合 —— **统一按「胜方视角」出指标**（2026-10-26 口径重构）。

    胜方 = 最终影响股价走向的一方：ΔP>0 → 多方，ΔP<0 → 空方。
    视角从始至终只有一个（胜方），不再有「发起方/守方」两套并存的语义。
    回合切分仍需要「触发方向」（attack_side），但它只是切分工具、不对外输出。

    指标（分母口径 = 该方向全部主动成交额，选择依据见设计文档 §2.3：
    单回合 CV 0.54 / 跨日 CV 0.42，优于「特大+大」的 0.77/0.79 与「净额」的 0.58/0.52）：
      eff_pct   边际效率 = |ΔP| ÷ 胜方投入 × 100 ÷ 价格  （每亿元推动的百分点）
      keep      冲击留存 = |ΔP| ÷ 胜方方向上的最大位移     （0~1，恒正）
      lq        对手盘厚度 = 负方投入 ÷ 胜方投入
    """
    s, e = cur["start"], cur["end"]
    p0, p1 = cur["p0"], rows[e]["price"]      # 基准 = 回合启动前一分钟
    dp = p1 - p0
    ws = 1 if dp > 0 else (-1 if dp < 0 else 0)   # 胜方：谁最终影响了股价走向
    # 中性区间：股价几乎没动时不指定胜方（否则横盘微跌也会被归给空方）
    if p0 and p0 > 0 and abs(dp) / p0 < NEUTRAL_PCT:
        ws = 0
    seg = rows[s:e + 1]
    amplitude = max(x["price"] for x in seg) - min(x["price"] for x in seg)   # 不分方向的振幅

    if ws == 0:
        # 横盘：不产出效率类指标（inv_win / eff_pct / keep / lq 全为 NaN）
        nan = float("nan")
        return dict(
            t0=rows[s]["t"], t1=rows[e]["t"], minutes=e - s + 1,
            win_side=0, p0=p0, p1=p1, dp=dp,
            inv_win=nan, inv_lose=nan, eff=nan, eff_pct=nan,
            peak_win=nan, keep=nan, amplitude=amplitude,
            lq=nan, lq_main=nan, defend_max=nan, defend_t=None,
            r_bal=cur["r_bal"], end=reason,
        )

    buy_all = sum(x["buy"] for x in seg)
    sell_all = sum(x["sell"] for x in seg)
    inv_win = buy_all if ws > 0 else sell_all     # 胜方投入（全部档位主动成交额）
    inv_lose = sell_all if ws > 0 else buy_all    # 负方投入
    bm = sum(x["tiers"]["super"][0] + x["tiers"]["big"][0] for x in seg)
    sm = sum(x["tiers"]["super"][1] + x["tiers"]["big"][1] for x in seg)
    inv_win_main = bm if ws > 0 else sm           # 主力口径（仅作诊断）
    inv_lose_main = sm if ws > 0 else bm

    eff = abs(dp) / (inv_win / 1e8) if inv_win > 0 else float("nan")
    eff_pct = eff / p0 * 100 if (p0 and p0 > 0) else float("nan")

    # 冲击留存：胜方方向上的最大位移中，有多少最终留下（恒 0~1）
    if ws > 0:
        peak_win = max(x["price"] - p0 for x in seg)
    else:
        peak_win = max(p0 - x["price"] for x in seg)
    keep = (abs(dp) / peak_win) if peak_win > 0 else float("nan")

    lq = (inv_lose / inv_win) if inv_win > 0 else float("nan")
    lq_main = (inv_lose_main / inv_win_main) if inv_win_main > 0 else float("nan")

    # 负方量能放大（原「阻击倍数」，胜方视角下改看负方是否反击）
    defend, defend_t = 0.0, None
    if ws != 0:
        for k in range(s + 1, e + 1):
            opp = sells[k] if ws > 0 else buys[k]
            base = _roll_median(sells if ws > 0 else buys, k, DEFEND_WIN)
            mult = opp / base if base > 0 else 0.0
            if mult > defend:
                defend, defend_t = mult, rows[k]["t"]

    return dict(
        t0=rows[s]["t"], t1=rows[e]["t"], minutes=e - s + 1,
        win_side=ws,
        p0=p0, p1=p1, dp=dp,
        inv_win=inv_win, inv_lose=inv_lose,
        eff=eff, eff_pct=eff_pct,
        peak_win=peak_win, keep=keep, amplitude=amplitude,
        lq=lq, lq_main=lq_main,
        defend_max=defend, defend_t=defend_t,
        r_bal=cur["r_bal"], end=reason,
    )


def detect_rounds(rows: list[dict]) -> list[dict]:
    """相位状态机：识别攻防回合并给出每回合的强度/阻击/结果。"""
    if not rows:
        return []
    g_act = max(float(np.percentile([r["g"] for r in rows], G_ACT_PCT)), G_ACT_FLOOR)
    buys = [r["buy"] for r in rows]
    sells = [r["sell"] for r in rows]

    def attack_side(i: int) -> int:
        r = rows[i]
        if r["g"] < g_act or r["g"] <= 0:
            return 0
        if _sym(r) <= R_ATTACK and abs(r["net"]) / r["g"] >= IMB_ATTACK:
            return 1 if r["net"] > 0 else -1
        return 0

    def balanced(i: int) -> bool:
        r = rows[i]
        return r["g"] > 0 and _sym(r) >= R_BALANCE and abs(r["net"]) / r["g"] <= IMB_BALANCE

    def new_round(i: int, side: int) -> dict:
        # p0 取「回合启动前一分钟」的价：位移必须从启动前的基准算，
        # 否则首击那一分钟的价格已包含冲击，会把位移记成 0（10-23 09:31 即踩此坑）。
        # side 仅作切分用的「触发方向」，不对外输出（对外统一为胜方视角）。
        return dict(start=i, side=side, r_bal=None,
                    p0=rows[max(0, i - 1)]["price"], last_active=i)

    rounds: list[dict] = []
    cur: dict | None = None
    for i, r in enumerate(rows):
        side = attack_side(i)

        # 攻守易位：当前分钟的反向进攻开启新回合（不计入旧回合）
        if cur is not None and side and side != cur["side"]:
            cur["end"] = max(cur["start"], i - 1)
            rounds.append(_close(rows, cur, "易位", buys, sells))
            cur = None

        if cur is None:
            if not side:
                continue
            cur = new_round(i, side)
            continue

        if r["g"] >= g_act:
            cur["last_active"] = i

        # 均衡确认
        if i - cur["start"] >= BALANCE_HOLD and all(
                balanced(j) for j in range(i - BALANCE_HOLD + 1, i + 1)):
            cur["end"] = i
            cur["r_bal"] = _sym(r)
            rounds.append(_close(rows, cur, "均衡", buys, sells))
            cur = None
            continue

        # 静默结束：连续 QUIET_MIN 分钟不再活跃 → 回合自然熄灭（避免长回合吞掉整段行情）
        if i - cur["last_active"] >= QUIET_MIN:
            cur["end"] = cur["last_active"]
            rounds.append(_close(rows, cur, "静默", buys, sells))
            cur = None
            continue

        if i - cur["start"] >= MAX_ROUND_MIN:
            cur["end"] = i
            rounds.append(_close(rows, cur, "超时", buys, sells))
            cur = None

    if cur is not None:
        cur["end"] = len(rows) - 1
        rounds.append(_close(rows, cur, "收盘", buys, sells))
    return rounds


def _flat_round(rows: list[dict], s: int, e: int, reason: str) -> dict:
    """横盘段的无胜方回合（字段结构与 `_close` 的 ws==0 分支完全一致）。"""
    nan = float("nan")
    seg = rows[s:e + 1]
    p0, p1 = rows[s]["price"], rows[e]["price"]
    return dict(
        t0=rows[s]["t"], t1=rows[e]["t"], minutes=e - s + 1,
        win_side=0, p0=p0, p1=p1, dp=p1 - p0,
        inv_win=nan, inv_lose=nan, eff=nan, eff_pct=nan,
        peak_win=nan, keep=nan,
        amplitude=max(x["price"] for x in seg) - min(x["price"] for x in seg),
        lq=nan, lq_main=nan, defend_max=nan, defend_t=None,
        r_bal=None, end=reason,
    )


def rounds_by_trend(rows: list[dict], **seg_kwargs) -> list[dict]:
    """按**股价趋势段**切回合（2026-10-26 新增，替代 `detect_rounds` 的资金相位切分）。

    为什么换：原相位切分只要「资金静默 5 分钟」就收口，一段连续下跌会被切成十几个碎块
    （HK.00700 一天几十个回合），与「一段下跌 = 一次空方攻击」的直觉不符。改为用
    `trend_segmenter.layer1_segments` 切出的「上涨 / 下跌 / 横盘」大段作为回合窗口。

    在此基础上叠加 L1.5 大加速切分（`trend_segmenter.split_big_accel`）：趋势段只认
    「方向反转」，不认「跌速加速」，于是把「缓→急」的大加速单独切出（每段最多一刀，
    只放行又陡又长的大加速）。10-21 的 10:52~15:22 即被切为 10:52~14:40（缓跌）与
    14:41~15:22（急跌）两个回合。

    语义映射（窗口换了，指标口径不变）：
      上涨段 → 多方自然为胜方（ΔP>0）；下跌段 → 空方；横盘段 → 不指定胜方（win_side=0，
      效率类指标为 NaN，前端本来就不画三角、柱高为 0，只留底纹带）。
    窗口之外的一切仍走 `_close`，λ / LQ / 冲击留存的计算完全复用，不另立口径。

    额外字段（前端可选用，不影响现有渲染）：
      trend     段方向（上涨/下跌/横盘）
      chg_pct   段内累计涨跌幅（%）
      rhythm    L2 节奏主标签（段内最长子段，如 急跌/缓跌/反弹/停顿）
      rhythm_n  L2 子段数
    """
    if not rows:
        return []
    prices = np.array([r["price"] for r in rows], dtype=float)
    segs = trend_segmenter.layer1_segments(prices, **seg_kwargs)
    # L1.5：把「缓→急」的大加速单独切出（每段最多一刀，只放行又陡又长的大加速）。
    # 10-21 的 10:52~15:22 后半段急跌即靠这一步从整段下跌中切出（见 trend_segmenter.split_big_accel）。
    segs = trend_segmenter.split_big_accel(prices, segs)
    buys = [r["buy"] for r in rows]
    sells = [r["sell"] for r in rows]

    rounds: list[dict] = []
    for seg in segs:
        if seg.direction == "横盘":
            r = _flat_round(rows, seg.s, seg.e, "趋势")
        else:
            cur = dict(start=seg.s, end=seg.e,
                       side=1 if seg.direction == "上涨" else -1,
                       r_bal=None, p0=rows[seg.s]["price"], last_active=seg.e)
            r = _close(rows, cur, "趋势", buys, sells)
        rhythm, n_sub, _ = trend_segmenter.layer2_rhythm(prices, seg.s, seg.e, seg.direction)
        r["trend"] = seg.direction
        r["chg_pct"] = round(float(seg.chg_pct), 4)
        r["rhythm"] = rhythm
        r["rhythm_n"] = n_sub
        rounds.append(r)

    return rounds


def state_series(rows: list[dict]) -> list[dict]:
    """分钟级攻守状态序列（前端「攻守状态带」用，纯价格+资金可算，无需回合）。

    返回 [{t, r_dir, er, g_norm}]：
      r_dir  = sign(net) × R，范围 [-1,1]；>0.5 多方主导、<-0.5 空方主导
      er     = 滚动 10 分钟趋势效率 |净位移| ÷ Σ|分钟位移|（>0.4 单边、<0.3 稳态）
      g_norm = 该分钟双边额 ÷ 当日 G 中位数（参与度）
    """
    if not rows:
        return []
    n = len(rows)
    px = [r["price"] for r in rows]
    g_ref = float(np.median([r["g"] for r in rows])) or 1.0
    out = []
    for i in range(n):
        r = rows[i]
        r_dir = (1 if r["net"] > 0 else (-1 if r["net"] < 0 else 0)) * _sym(r)
        a = max(0, i - 9)
        seg = px[a:i + 1]
        path = sum(abs(seg[j] - seg[j - 1]) for j in range(1, len(seg)))
        er = abs(seg[-1] - seg[0]) / path if path else 0.0
        out.append(dict(t=r["t"], r_dir=r_dir, er=er, g_norm=r["g"] / g_ref))
    return out
