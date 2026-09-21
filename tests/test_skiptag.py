"""标签弃权机制（SkipTag）测试。

校验的不再是「统一的源表健康检查」（该方案已废弃，职责下沉到各标签自身，
通用检查交给运维平台），而是引擎侧唯一的这条写库语义：

    raise SkipTag    = 「这次没法算」→ 不进 diff、不写库、现有版本一条不动
    返回空 DataFrame = 「算出来是没有」→ 是真实结果，正常参与 diff（缺失即关闭）

9/15 事故（单日关闭 55,035 行画像）正是混淆了这两种语义。

用真实库做断言（与 test_profiling.py 一致）；写库类用例在 finally 里 rollback。
"""
import os
import sys
from datetime import date, timedelta

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_conn  # noqa: E402
from profiling import engine, registry, tags  # noqa: F401,E402
from profiling.tags._base import RESULT_COLS, SkipTag, last_arrival, require_fresh  # noqa: E402

# 任意一个在线标签，用于跑通 engine 全链路（不依赖它原本有没有历史数据）
_TAG = next(m.code for m in registry.all_tags() if m.status == registry.ACTIVE)


def _empty_frame(as_of=None) -> pd.DataFrame:
    """空结果帧：语义是「算出来没有」，不是「算不了」。"""
    return pd.DataFrame({c: pd.Series(dtype="object") for c in RESULT_COLS})


def _count_rows(conn, tag_code, as_of) -> int:
    """as_of 当日有效（未被关闭）的版本数。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM profile.tag_value "
            "WHERE tag_code=%s AND eff_from<=%s AND (eff_to IS NULL OR eff_to>%s)",
            (tag_code, as_of, as_of),
        )
        return cur.fetchone()[0]


def test_last_arrival_returns_latest_date():
    with get_conn() as conn:
        latest = last_arrival(conn, "hk_daily_quote", "trade_date")
    assert latest is not None and isinstance(latest, date)


def test_require_fresh_ok_and_lagged():
    """max_lag 由标签自己定：容忍期内放行，超期则弃权。"""
    with get_conn() as conn:
        latest = last_arrival(conn, "hk_daily_quote", "trade_date")
        # 当天到货：容忍 0 天也放行
        assert require_fresh(conn, "hk_daily_quote", latest, max_lag=0) == latest
        # 滞后 2 天、容忍 3 天：放行
        assert require_fresh(conn, "hk_daily_quote", latest + timedelta(days=2), max_lag=3) == latest
        # 滞后 10 天、容忍 0 天：弃权
        with pytest.raises(SkipTag):
            require_fresh(conn, "hk_daily_quote", latest + timedelta(days=10), max_lag=0)


def test_skiptag_skips_and_writes_nothing(monkeypatch):
    """核心：标签 raise SkipTag → skipped，且现有版本一条都不动。"""

    def _boom(as_of):
        raise SkipTag(f"测试弃权 as_of={as_of}")

    monkeypatch.setattr(registry, "get_func", lambda code: _boom)
    as_of = date.today()
    # 必须用 with 持有连接：get_conn 是 @contextmanager，手动 __enter__() 会让
    # 生成器被回收时在 finally 里 conn.close()，后续断言拿到的就是已关闭的连接
    with get_conn() as conn:
        before = _count_rows(conn, _TAG, as_of)
        res = engine.compute_tag(conn, _TAG, as_of=as_of, force=True)
        assert res["status"] == "skipped", res
        assert "保留现有版本" in res["message"]
        assert res["rows_closed"] == 0 and res["rows_new"] == 0
        assert _count_rows(conn, _TAG, as_of) == before, "弃权不应关闭/修改任何现有版本"
        conn.rollback()


def test_empty_result_is_not_skip(monkeypatch):
    """返回空帧 ≠ 弃权：它是真实结果，必须照常走到 diff 阶段。"""
    monkeypatch.setattr(registry, "get_func", lambda code: _empty_frame)
    with get_conn() as conn:
        # dry_run 不落库，但足以验证它没被当成弃权、且确实算到了 diff
        res = engine.compute_tag(conn, _TAG, as_of=date.today(), force=True, dry_run=True)
        assert res["status"] != "skipped", res
        assert res["rows_total"] == 0
        assert "rows_closed" in res, "未进入 diff 阶段"
        conn.rollback()
