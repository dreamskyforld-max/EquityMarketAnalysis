"""§4.1 股债性价比的「两套标准化」测试（ERP 分位 vs ERP z-score）。

背景：4.1 列了 5 项，其中 **Fed Model**（「ERP 变体，与历史均值比较」）与 **ERP 历史分位**
是同一个 ERP 的两种标准化，方向可能分歧，故两者都保留（`VAL.ERP.percentile` 与 `VAL.ERP_Z`）。
本文件钉住 z-score 的三条性质：

  ① **无前视**：t 点的 z-score 只依赖 t 及之前的观测 —— 追加未来数据不得改变历史值
     （与 `_expanding_percentile` 同一纪律；这两者都必须在**全历史**上算，故调用点在
     写库管线 `_write_market` 的 combined 上，而不是 calc_* 内部的窗口序列上）；
  ② 样本不足 `min_periods` 个有效观测时为 NaN（其中标准差为 0 时同样为 NaN，避免除零爆值）；
  ③ 与分位的一致性：z-score > 0 ⟺ 值高于历史均值；分位 > 50 ⟺ 高于历史中位 —— 二者符号
     方向必须一致（除非分布严重偏斜）。
"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_state_daily import _expanding_percentile, _expanding_zscore


def _s(vals) -> pd.Series:
    return pd.Series([float(v) for v in vals])


class TestExpandingZScore(unittest.TestCase):
    def test_nan_until_min_periods(self):
        z = _expanding_zscore(_s(range(10)), min_periods=5)
        self.assertTrue(z.iloc[:4].isna().all(), "前 4 个样本不足 5 个 → 应为 NaN")
        self.assertFalse(np.isnan(z.iloc[4]), "第 5 个样本起应有值")

    def test_zero_std_is_nan(self):
        z = _expanding_zscore(_s([3.0] * 10), min_periods=3)
        self.assertTrue(np.isnan(z.iloc[-1]), "常数列标准差为 0 → 结果应为 NaN（不除零）")

    def test_sign_follows_mean(self):
        # [10, 1, 1, 20]，min_periods=2：i=1 时累计均值 5.5（值 1 在下）；i=3 时均值 8（值 20 在上）
        z = _expanding_zscore(_s([10.0, 1.0, 1.0, 20.0]), min_periods=2)
        self.assertLess(z.iloc[1], 0, "从 10 跌到 1 → 低于累计均值 → 负")
        self.assertGreater(z.iloc[3], 0, "跳到 20 → 高于累计均值 → 正")

    def test_no_lookahead(self):
        """追加未来数据不得改写历史 z-score（扩展统计量的核心性质）。"""
        base = [1.0, 2.0, 3.0, 2.5, 4.0, 3.5]
        short = _expanding_zscore(_s(base), min_periods=2)
        long_ = _expanding_zscore(_s(base + [100.0, 200.0]), min_periods=2)
        pd.testing.assert_series_equal(short, long_.iloc[:len(base)], check_names=False)

    def test_direction_consistent_with_percentile(self):
        """z-score 与分位对「高于中枢」的判定方向必须一致。"""
        s = _s([1, 2, 3, 2, 4, 2, 5, 3, 2, 6])
        z = _expanding_zscore(s, min_periods=3)
        p = _expanding_percentile(s)
        # 取一个明显上行的时点：z 与分位应同时显示「高于中枢」
        for i in (6, 9):
            self.assertGreater(z.iloc[i], 0)
            self.assertGreater(p.iloc[i], 50.0)


if __name__ == "__main__":
    unittest.main()
