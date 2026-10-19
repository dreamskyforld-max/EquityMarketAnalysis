"""指标来源接线测试（指标层的两条入湖通路）。

背景：指标层有两条入湖通路，映射以**模块常量**声明在 `market_state_daily.py`：

  · `PIT_MAP_CN` / `PIT_MAP_HK`：`regime.macro_series` → 指标（月/季频按 `release_time` PIT 填充）
  · `BENCH_MAP` / `MOM_MAP`    ：`public.daily_benchmark` → 指标（市场报价，同日对齐；
     `MOM_MAP` 含 20 日变化，pct=百分比 / diff=收益率的绝对 pp 变化）
  · 其余 42 个指标由公式从多源计算（来源见 `indicator_dict.formula`）

**源编码写错是静默故障**：`_macro_pit_map` / `_bench` 查不到就返回空序列，指标照常登记进
`indicator_dict`、照常写库，只是永远没有数据、不抛任何错。本文件钉住三件事：

  ① 正向：映射引用的每个源编码**必须已注册**——macro 侧查 `regime.macro_series_meta`、
     bench 侧查 `public.daily_benchmark.bench_code`；
  ② 反向：映射的键必须都在 `INDICATORS` 里（防止改了指标编码却留下孤儿映射）；
  ③ 端到端：两条通路对每个接线指标都真的算出**非空**序列（挡「注册了但查不到数据」，
     如 revision 过滤、时间窗、序列为空等）；
  ④ `source` 串不得超 `indicator_dict.source` 的 VARCHAR(64)——超长会被 `_ensure_dict`
     静默截断。
"""
import datetime
import os
import sys
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_conn
from market_state_daily import (
    INDICATOR_SOURCES, INDICATORS, LEVEL_MAP, MOM_MAP, PIT_MAP_CN, PIT_MAP_HK,
    _global_block, _macro_pit_map,
)

SOURCE_MAX_LEN = 64          # indicator_dict.source 列宽（见 _ensure_dict 的 lim）


def _macro_refs() -> dict:
    """指标 → macro_series 源编码**集合**（PIT 映射 + 水平/动量里 kind='macro' 的）。

    ⚠ 值是集合而非单值：同一指标可在 CN/HK 两侧取**不同源**（如 `VAL.DIV_SPREAD`：CN 用
    沪深等权股息率、HK 用恒指股息率）—— 若用 dict.update 覆盖，先出现的那条就不被校验了。
    """
    refs: dict = {}
    for mp in (PIT_MAP_CN, PIT_MAP_HK):
        for ind, src in mp.items():
            refs.setdefault(ind, set()).add(src)
    for c, s, kind in LEVEL_MAP:
        if kind == "macro":
            refs.setdefault(c, set()).add(s)
    for c, s, kind, _mode in MOM_MAP:
        if kind == "macro":
            refs.setdefault(c, set()).add(s)
    return refs


def _bench_refs() -> dict:
    """指标 → daily_benchmark 源编码**集合**（水平直连 + 动量里 kind='bench' 的）。"""
    refs: dict = {}
    for c, s, kind in LEVEL_MAP:
        if kind == "bench":
            refs.setdefault(c, set()).add(s)
    for c, s, kind, _mode in MOM_MAP:
        if kind == "bench":
            refs.setdefault(c, set()).add(s)
    return refs


def _recent_idx() -> pd.DatetimeIndex:
    """近 400 个自然工作日（覆盖月/季频指标的发布周期，且不受「今天」是否为交易日影响）。"""
    end = pd.Timestamp(datetime.date.today())
    return pd.bdate_range(end=end, periods=400)


class TestSourceRegistration(unittest.TestCase):
    """① 正向 + ② 反向 + ④ 列宽。"""

    def test_macro_sources_registered(self):
        refs = _macro_refs()
        self.assertTrue(refs, "macro 映射不应为空")
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT series_code FROM regime.macro_series_meta")
            registered = {r[0] for r in cur.fetchall()}
        missing = {k: sorted(v) for k, v in refs.items() if not v <= registered}
        self.assertEqual(
            missing, {},
            f"以下指标的 macro_series 源编码未注册（写错会静默产出空指标）: {missing}")

    def test_bench_sources_registered(self):
        refs = _bench_refs()
        self.assertTrue(refs, "bench 映射不应为空")
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT bench_code FROM public.daily_benchmark")
            registered = {r[0] for r in cur.fetchall()}
        missing = {k: sorted(v) for k, v in refs.items() if not v <= registered}
        self.assertEqual(
            missing, {},
            f"以下指标的 daily_benchmark 源编码未注册: {missing}")

    def test_map_keys_are_declared_indicators(self):
        codes = {d["code"] for d in INDICATORS}
        wired = set(_macro_refs()) | set(_bench_refs())
        orphans = sorted(wired - codes)
        self.assertEqual(
            orphans, [],
            f"以下映射键不在 INDICATORS 里（孤儿映射，改了指标编码未同步）: {orphans}")

    def test_indicator_sources_derivation(self):
        """INDICATOR_SOURCES 必须是映射的**全集投影**（bench 侧带 bench: 前缀）。

        跨市场不同源时投影形如 `"CN.A / HK.B"`（见 `_build_indicator_sources`）—— 解析后
        两侧都必须与映射**逐一相等**，否则会出现「登记了 A 却漏了 B」的静默缺口。
        """
        macro, bench = _macro_refs(), _bench_refs()
        self.assertEqual(set(INDICATOR_SOURCES), set(macro) | set(bench))
        for code, src in INDICATOR_SOURCES.items():
            parts = src.split(" / ")
            got_macro = {p for p in parts if not p.startswith("bench:")}
            got_bench = {p[6:] for p in parts if p.startswith("bench:")}
            self.assertEqual(got_macro, macro.get(code, set()), f"{code} 的 macro 源不一致")
            self.assertEqual(got_bench, bench.get(code, set()), f"{code} 的 bench 源不一致")

    def test_source_fits_column(self):
        too_long = {k: v for k, v in INDICATOR_SOURCES.items() if len(v) > SOURCE_MAX_LEN}
        self.assertEqual(
            too_long, {},
            f"以下 source 串超过 indicator_dict.source 的 VARCHAR({SOURCE_MAX_LEN})，会被截断: {too_long}")


class TestWiredSourcesReturnData(unittest.TestCase):
    """③ 端到端：两条通路对每个接线指标都必须产出非空序列。"""

    def test_pit_map_returns_data(self):
        idx = _recent_idx()
        empty = {}
        with get_conn() as conn:
            for market, mp in (("CN", PIT_MAP_CN), ("HK", PIT_MAP_HK)):
                out = _macro_pit_map(conn, idx, mp)
                for code, src in mp.items():
                    s = out.get(code)
                    if s is None or not s.notna().any():
                        empty[f"{market}:{code}"] = src
        self.assertEqual(
            empty, {},
            f"以下接线指标的序列全空（源编码错 / 无数据 / revision 不匹配）: {empty}")

    def test_level_and_mom_maps_return_data(self):
        """水平直连（bench 与 macro 两层）与动量都必须产出非空序列。

        水平用 `_align_close`（同日对齐，无 PIT）；动量在源市场交易日上算 20 日变动。
        源编码写错 → `_bench`/`_macro` 返回空序列 → `_align_close` 全 NaN，此处即失败。
        """
        idx = _recent_idx()
        with get_conn() as conn:
            out = _global_block(conn, idx)
        empty = {}
        # LEVEL_MAP 3 元组、MOM_MAP 4 元组（含变化口径）→ 取前两位即可
        for code, src, *_ in tuple(LEVEL_MAP) + tuple(MOM_MAP):
            s = out.get(code)
            if s is None or not s.notna().any():
                empty[code] = src
        self.assertEqual(
            empty, {},
            f"以下水平 / 动量指标的序列全空（源编码错或无数据）: {empty}")


if __name__ == "__main__":
    unittest.main()
