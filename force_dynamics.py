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

输出（回合列表，每回合一个 dict）
--------------------------------
t0/t1/minutes/side     时段、时长、方向（+1 多 / -1 空）
attack_vol/counter_vol 攻方同向投入合计 / 回合内反向流量
net_vol                攻方向净得手
defend_max             守方量能相对其基线的最大放大倍数（阻击意图）
impact/keep_ratio      冲击（攻方视角最大位移）/ 留存率 = 冲击 ÷ 最终位移
eff / eff_pct          λ（元/亿）/ λ_pct（每亿元推动的百分点，跨票口径）
lq                     对手盘厚度 = 守方四档总投入 ÷ 攻方特大+大投入
win                    胜 / 弱 / 负（按留存率分档）
end                    均衡 / 静默 / 易位 / 超时 / 收盘
is_main                是否主回合（投入 >= 当日门槛）

调用方
------
`build_force_rounds.py`（每日 16:40 跑批落库 force_round_daily）。
"""
from __future__ import annotations

import numpy as np

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
MAIN_VOL_FLOOR = 1.0e8  # 主回合投入下限（元）
MAIN_VOL_PCT = 0.015    # 主回合门槛 = max(下限, 当日双边额 × 该比例)
                        # 绝对金额门槛对低价股/低流动性票不公平（设计文档 §5.1：
                        # HK.03690 仅 8 个回合、SH.600941/SH.520900 为 0），故用相对口径


def _roll_median(vals: list[float], i: int, w: int) -> float:
    prev = vals[max(0, i - w):i]
    return float(np.median(prev)) if prev else 0.0


def _sym(r: dict) -> float:
    """对称度 R = min(买,卖)/max(买,卖)。R→1 双方量能对等，R→0 完全单边。"""
    lo, hi = min(r["buy"], r["sell"]), max(r["buy"], r["sell"])
    return (lo / hi) if hi > 0 else 0.0


def main_vol_threshold(rows: list[dict]) -> float:
    """当日「主回合」投入门槛（元）。"""
    return max(MAIN_VOL_FLOOR, sum(r["g"] for r in rows) * MAIN_VOL_PCT)


def _close(rows: list[dict], cur: dict, reason: str) -> dict:
    """收口一个回合：位移、效率、分档指标、胜负。"""
    s, e = cur["start"], cur["end"]
    p0, p1 = cur["p0"], rows[e]["price"]      # 基准 = 进攻发起前一分钟
    dp = p1 - p0
    signed = dp * cur["side"]                 # 攻方视角：>0 表示攻方推动成功
    vol = cur["attack"]
    eff = abs(signed) / (vol / 1e8) if vol > 0 else float("nan")
    impact = cur["peak"]                      # 攻方视角的最大位移（冲击）
    keep_ratio = (signed / impact) if impact > 0 else float("nan")
    # λ_pct：每亿元投入推动的百分点（跨票可比口径）。原始 λ（元/亿）跨票差 64 倍且与
    # 分钟波动成正比，除以价格后缩到约 1.5 倍（设计文档 §5.1）。
    eff_pct = (abs(signed) / (vol / 1e8)) / p0 * 100 if (p0 and p0 > 0 and vol > 0) else float("nan")
    # 分档口径：攻方投入 = 攻方向「特大+大」绝对额（重骑兵+骑兵，主导力量）；
    # LQ 对手盘厚度 = 守方四档总投入 ÷ 攻方特大+大。LQ 是 Kyle(1985) 的流动性供给：
    # 对手盘越厚，攻方每亿元推动的价格越多、完成得越干净。
    seg = rows[s:e + 1]
    bm = sum(x["tiers"]["super"][0] + x["tiers"]["big"][0] for x in seg)
    sm = sum(x["tiers"]["super"][1] + x["tiers"]["big"][1] for x in seg)
    br = sum(x["tiers"]["mid"][0] + x["tiers"]["small"][0] for x in seg)
    sr = sum(x["tiers"]["mid"][1] + x["tiers"]["small"][1] for x in seg)
    atk_main, def_main, def_ret = (sm, bm, br) if cur["side"] < 0 else (bm, sm, sr)
    lq = ((def_main + def_ret) / atk_main) if atk_main > 0 else float("nan")
    # 结果按「留存率」分档：冲击再大，守不住就不算赢（对应「试探 vs 决战」）
    if impact <= 0:
        win = "负"
    elif keep_ratio >= 0.5:
        win = "胜"
    elif keep_ratio >= 0.15:
        win = "弱"
    else:
        win = "负"
    return dict(
        t0=rows[s]["t"], t1=rows[e]["t"], minutes=e - s + 1, side=cur["side"],
        attack_vol=vol, counter_vol=cur["counter"], net_vol=cur["net"] * cur["side"],
        defend_max=cur["defend"], defend_t=cur["defend_t"], r_bal=cur["r_bal"],
        p0=p0, p1=p1, dp=dp, signed=signed, impact=impact, keep_ratio=keep_ratio, eff=eff,
        eff_pct=eff_pct,
        lq=lq, def_main_ratio=(def_main / atk_main) if atk_main > 0 else float("nan"),
        def_ret_ratio=(def_ret / atk_main) if atk_main > 0 else float("nan"),
        win=win, end=reason,
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
        # p0 取「进攻发起前一分钟」的价：冲击幅度必须从发起前的基准算，
        # 否则首击那一分钟的价格已包含冲击，会把冲击记成 0（10-23 09:31 即踩此坑）。
        return dict(start=i, side=side, attack=0.0, counter=0.0, net=0.0,
                    defend=0.0, defend_t=None, r_bal=None,
                    p0=rows[max(0, i - 1)]["price"], peak=0.0, last_active=i)

    def acc(cur: dict, r: dict) -> None:
        """把一分钟计入回合计量：同向记 attack、反向记 counter、代数和记 net；
        并追踪「攻方视角的最大位移」（冲击幅度，区别于回合结束时的留存）。"""
        v = r["net"] * cur["side"]
        if v > 0:
            cur["attack"] += v
        else:
            cur["counter"] += -v
        cur["net"] += r["net"]
        peak = (r["price"] - cur["p0"]) * cur["side"]
        if peak > cur["peak"]:
            cur["peak"] = peak

    rounds: list[dict] = []
    cur: dict | None = None
    for i, r in enumerate(rows):
        side = attack_side(i)

        # 攻守易位：当前分钟的反向进攻开启新回合（不计入旧回合）
        if cur is not None and side and side != cur["side"]:
            cur["end"] = max(cur["start"], i - 1)
            rounds.append(_close(rows, cur, "易位"))
            cur = None

        if cur is None:
            if not side:
                continue
            cur = new_round(i, side)
            acc(cur, r)          # 启动分钟必须计入，否则会漏掉最重的一击
            continue

        acc(cur, r)
        if r["g"] >= g_act:
            cur["last_active"] = i

        # 守方量能（相对其自身基线；启动分钟不计，避免把首击算成阻击）
        if i > cur["start"]:
            opp = sells[i] if cur["side"] > 0 else buys[i]
            base = _roll_median(sells if cur["side"] > 0 else buys, i, DEFEND_WIN)
            mult = opp / base if base > 0 else 0.0
            if mult > cur["defend"]:
                cur["defend"], cur["defend_t"] = mult, r["t"]

        # 均衡确认
        if i - cur["start"] >= BALANCE_HOLD and all(
                balanced(j) for j in range(i - BALANCE_HOLD + 1, i + 1)):
            cur["end"] = i
            cur["r_bal"] = _sym(r)
            rounds.append(_close(rows, cur, "均衡"))
            cur = None
            continue

        # 静默结束：连续 QUIET_MIN 分钟不再活跃 → 回合自然熄灭（避免长回合吞掉整段行情）
        if i - cur["last_active"] >= QUIET_MIN:
            cur["end"] = cur["last_active"]
            rounds.append(_close(rows, cur, "静默"))
            cur = None
            continue

        if i - cur["start"] >= MAX_ROUND_MIN:
            cur["end"] = i
            rounds.append(_close(rows, cur, "超时"))
            cur = None

    if cur is not None:
        cur["end"] = len(rows) - 1
        rounds.append(_close(rows, cur, "收盘"))
    # 主回合门槛直接标记到回合上，避免各调用方用不同口径
    th = main_vol_threshold(rows)
    for r in rounds:
        r["is_main"] = r["attack_vol"] >= th
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
