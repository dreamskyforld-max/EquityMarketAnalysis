"""get_hk_market_turnover.py 代码格式与失败门禁单元测试（纯 mock，不连富途/不写库）。

背景（2026-09-15 事故）：清单源从 akshare 现货清单（纯数字 00700）换成
v_quote_scope（带前缀 HK.00700）后，采集侧仍无条件拼 "HK." → HK.HK.00700 →
富途整批判「未知股票」→ 全市场港股日线静默断供 5 个交易日。本文件锁住两点：
  1. 传给富途的代码永远是合法 HK.xxxxx（幂等拼接，绝不双前缀）；
  2. 全批次失败必须抛错（不能「0 数据却记成功」）。
"""
import os
import re
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import get_hk_market_turnover as m  # noqa: E402

_HK_RE = re.compile(r"HK\.\d{5,6}")


class FakeCtx:
    """模拟富途 get_market_snapshot：批内任一代码非法 → 整批 RET_ERROR。

    复刻真实富途行为（否则测试会漏掉「双前缀」这类整批失效问题）。
    """

    def __init__(self, ok=True):
        self.batches = []
        self.ok = ok

    def get_market_snapshot(self, batch):
        self.batches.append(list(batch))
        if not self.ok:
            return -1, "连接断开"
        bad = [c for c in batch if not _HK_RE.fullmatch(str(c))]
        if bad:
            return -1, f"未知股票 {bad[0]}"
        rows = [{
            "code": c,
            "turnover": 1234.0,
            "volume": 1000,
            "update_time": "2026-09-21 16:30:00",
            "last_price": 1.0,
            "prev_close_price": 0.9,
            "turnover_rate": 0.01,
            "volume_ratio": 1.0,
            "highest52weeks_price": 2.0,
            "lowest52weeks_price": 0.5,
            "total_market_val": 1e8,
            "circular_market_val": 1e8,
            "pe_ratio": 10.0,
            "pe_ttm_ratio": 10.0,
            "pb_ratio": 1.0,
            "dividend_ratio_ttm": 0.0,
        } for c in batch]
        return 0, pd.DataFrame(rows)


def test_norm_hk_code_idempotent():
    """幂等：带前缀原样返回，纯数字补前缀（两种清单口径都要能吞下）。"""
    assert m._norm_hk_code("HK.00700") == "HK.00700"
    assert m._norm_hk_code("00700") == "HK.00700"
    # 连续归一化不应叠加前缀
    assert m._norm_hk_code(m._norm_hk_code("00700")) == "HK.00700"


def test_fetch_sends_legal_codes_for_prefixed_list():
    """v_quote_scope 口径（带前缀）：传给富途的代码必须全部合法、无双前缀。"""
    ctx = FakeCtx()
    rec = m.fetch_market_snapshot_batch(["HK.00700", "HK.00001"], ctx=ctx)
    assert [c for b in ctx.batches for c in b] == ["HK.00700", "HK.00001"]
    assert rec["ok_batches"] == 1 and rec["failed_batches"] == 0
    assert rec["trade_date"] is not None
    assert rec["quote_rows"][0]["stock_code"] == "HK.00700"


def test_fetch_sends_legal_codes_for_bare_list():
    """旧 akshare 口径（纯数字）：仍要自动补成合法代码。"""
    ctx = FakeCtx()
    rec = m.fetch_market_snapshot_batch(["00700"], ctx=ctx)
    assert ctx.batches == [["HK.00700"]]
    assert rec["ok_batches"] == 1


def test_double_prefix_fails_all_batches():
    """回归锁：非幂等拼接（旧实现 f"HK.{c}"）产出的双前缀代码会被富途整批拒绝。

    校验「一旦上游又产生 HK.HK.xxxxx，采集侧立刻 zero ok batch」，
    由 run() 门禁升级为任务失败，而不是静默产出 0 数据。
    """
    broken = [f"HK.{c}" for c in ["HK.00700", "HK.01401"]]  # 旧实现产物
    assert broken == ["HK.HK.00700", "HK.HK.01401"]
    ctx = FakeCtx()
    rec = m.fetch_market_snapshot_batch(broken, ctx=ctx)
    assert rec["ok_batches"] == 0
    assert rec["failed_batches"] == 1
    assert rec["quote_rows"] == []


def test_run_raises_when_every_batch_failed(monkeypatch):
    """全批次失败必须抛错（调度器据此标记失败），不能静默返回。"""
    monkeypatch.setattr(m, "_hk_code_list", lambda: ["HK.00700", "HK.00001"])
    monkeypatch.setattr(m, "get_shared_ctx", lambda: FakeCtx(ok=False))
    with pytest.raises(RuntimeError, match="全部批次失败"):
        m.run()
