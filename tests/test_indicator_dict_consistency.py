"""指标字典的**声明**与指标数据的**实际产出**是否一致（分市场维度）。

背景：`indicator_dict.market_scope` 是「该指标在哪些市场产出」的**声明**，但产出与否实际由
计算层决定（`calc_cn` / `calc_hk` 是否给它赋值、接线映射是否含该市场）。两者相互独立，
声明会静默漂移 —— 实测踩过：

    RISK.CREDIT_SPREAD  声明 market_scope='CN'，但 PIT_MAP_HK 里也接了它
                        → HK 侧实际有 1,915 行；而 HK 的 FSI 合成正在用这些行
                        → 应用层若按 market_scope 过滤，会**看不到**它们

本文件钉住三条契约：
  ① `market_scope` 与 `indicator_value.market` 的**取值域**封闭（新增取值必须显式处理）；
  ② 声明的市场集 == 实产的市场集（CN / HK / CN+HK 各有约束，GLOBAL 默认两侧）；
  ③ 每个已声明的指标**至少有一个市场**产出（防「登记了但从未产出」）。

**附：已否决的方案 —— 应用层入口视图 `regime.v_indicator_latest`（2026-10-16 建后又撤）**

曾建过一个视图：`每个 (指标, 市场) 最新一行 + 指标字典元数据`，用于给应用层一个稳定入口。
撤除理由（避免后人重造）：

  · 它的**硬价值只有一条**：带出 `direction` —— `indicator_value.percentile` 明确「未做方向
    调整」，消费方必须配合 `indicator_dict.direction` 才能解读（RISK.RV20 高=坏、VAL.ERP 高=好）。
    其余都是便利（封装 latest 语义 / 元数据一次取回 / 给基表列演进留空间），成本近零但非必需。
  · 它顺带带出的 `market_scope` 是**把「声明」混进「事实」**：视图每行已是具体的
    (指标, 市场) 组合，**行存在本身即证明该市场有数据**，`market_scope` 冗余（同指标的各行
    该列完全相同），更糟的是会诱导消费方写出「按声明过滤」—— 那正是本文件 ② 要防的漏行事故。
  · 故应用层改为：直读 `regime.indicator_value`（带 `market` 过滤）+ JOIN `indicator_dict`
    取 `direction`/`unit`；`market_scope` 只作为字典表的**声明**存在，其与事实的一致性由
    本文件校验。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_conn

# `market_scope` 的合法取值 → 期望的实产市场集
SCOPE_RULES = {
    "CN": {"CN"},
    "HK": {"HK"},
    "CN+HK": {"CN", "HK"},
}
# `GLOBAL` 的语义是「变量本身是全球性的」，**暴露侧**仍由接线决定；本项目的 GLOBAL 指标
#   绝大多数经 `_global_block` 在 CN/HK 两侧扇出，故默认按 {CN, HK} 约束。
#   ⚠ 只在单侧暴露的必须在此登记（保持「GLOBAL = 两侧」这条契约可校验）。
GLOBAL_ONE_SIDED = {
    "MACRO.CLI_GLOBAL",     # OECD G20 领先指标：走 PIT_MAP_CN，未接 HK
}
VALID_MARKETS = {"CN", "HK"}


def _dict_rows() -> list:
    """(indicator_code, market_scope) 全量。"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT indicator_code, market_scope FROM regime.indicator_dict")
        return cur.fetchall()


def _market_counts() -> dict:
    """{indicator_code: {market: rows}}（只含 >0 的组合由调用方筛）。"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""SELECT indicator_code, market, count(*)
                       FROM regime.indicator_value GROUP BY 1, 2""")
        out: dict = {}
        for code, mkt, n in cur.fetchall():
            out.setdefault(code, {})[mkt] = n
        return out


def _actual_markets(counts: dict, code: str) -> set:
    return {m for m, n in counts.get(code, {}).items() if n > 0}


class TestScopeAndDataAgree(unittest.TestCase):
    def setUp(self):
        self.dict_rows = _dict_rows()
        self.counts = _market_counts()
        self.assertTrue(self.dict_rows, "indicator_dict 不应为空")

    def test_scope_values_are_known(self):
        """market_scope 取值域封闭：新增取值必须在 SCOPE_RULES / 下面的分支里显式处理。"""
        unknown = sorted({s for _, s in self.dict_rows} - set(SCOPE_RULES) - {"GLOBAL"})
        self.assertEqual(
            unknown, [],
            f"出现未处理的 market_scope 取值 {unknown} —— 请在本测试里定义其期望市场集")

    def test_market_column_values_are_known(self):
        """indicator_value.market 只应是 CN / HK（列注释里承诺的两个字面量）。"""
        got = {m for c in self.counts.values() for m in c}
        self.assertEqual(got - VALID_MARKETS, set(),
                         f"indicator_value.market 出现未登记取值: {sorted(got - VALID_MARKETS)}")

    def test_market_scope_matches_actual_rows(self):
        """声明的市场集必须等于实产的市场集（本文件的核心断言）。"""
        bad = []
        for code, scope in self.dict_rows:
            got = _actual_markets(self.counts, code)
            if scope == "GLOBAL":
                expect_ok = (got == {"CN", "HK"}) or (
                    code in GLOBAL_ONE_SIDED and got in ({"CN"}, {"HK"}))
            else:
                expect_ok = got == SCOPE_RULES[scope]
            if not expect_ok:
                bad.append(dict(indicator_code=code, declared=scope, actual=sorted(got)))
        self.assertEqual(
            bad, [],
            "以下指标「声明 vs 实际产出」不一致 —— 修 market_scope，或把接线补/去掉：\n"
            + "\n".join(f"  {b['indicator_code']:32s} 声明={b['declared']:6s} "
                        f"实际={b['actual']}" for b in bad))

    def test_every_indicator_has_rows(self):
        """每个声明的指标至少一个市场有产出（防「登记了但从没产出」）。"""
        empty = sorted(code for code, _ in self.dict_rows
                       if not _actual_markets(self.counts, code))
        self.assertEqual(empty, [], f"以下指标已登记但无任何数据（接线缺失/未回填）: {empty}")


if __name__ == "__main__":
    unittest.main()
