"""backfill_hk_market_val.py 性能改造回归测试（纯 mock，不连百度/不写库）。

背景（2026-10-17）：旧版全量 2833 只串行跑 8~12 小时起步、百度限流后更慢。
根因：① 每股票 8 次串行 HTTP 且 min_pts=30 误判限流白跑指数退避；
② 每股票 3 次 DB 连接（含循环内冗余 _ensure_table）；
③ _rolling_52w 每交易日重建 252 元素列表（全量约 18 亿次操作）。

本文件锁住改造后的行为，且 _rolling_52w 必须与朴素参考实现逐值相等：
  1. _fetch_baidu 的限流/截断/确认无数据/短历史判定与退避次数；
  2. _build_col_series 的并发抓取不改合并语义（近三年优先、全部补缺）；
  3. _rolling_52w 单调队列与朴素 O(n·252) 参考完全一致（含 None）。
"""
import os
import random
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backfill_hk_market_val as m  # noqa: E402


# ---------------------------------------------------------------------------
# 百度响应 mock
# ---------------------------------------------------------------------------
class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    """按队列依次返回响应；Exception 元素表示 get() 抛错。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, headers=None, timeout=None):
        self.calls += 1
        item = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return FakeResp(item)


def _payload(body):
    """构造百度正常响应外壳。"""
    return {"ResultCode": 0,
            "Result": [{"DisplayData": {"resultData": {"tplData": {
                "result": {"chartInfo": [{"body": body}]}}}}}]}


def _body(n, start="2026-01-05"):
    """n 个 (日期, 值) 原始行。"""
    from datetime import timedelta
    d0 = date.fromisoformat(start)
    return [[(d0 + timedelta(days=i)).isoformat(), "1.5"] for i in range(n)]


def _patch_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(m.time, "sleep", lambda s: slept.append(s))
    return slept


# ---------------------------------------------------------------------------
# 1. _fetch_baidu
# ---------------------------------------------------------------------------
class TestFetchBaidu:
    def test_rate_limit_resultcode_nonzero_retries_then_none(self, monkeypatch):
        slept = _patch_sleep(monkeypatch)
        sess = FakeSession([{"ResultCode": -1, "Result": []}])
        monkeypatch.setattr(m, "_SESSION", sess)
        assert m._fetch_baidu("总市值", "00700", "近三年") is None
        assert sess.calls == 3            # 固定 2s × 最多 3 次
        assert slept == [2.0, 2.0, 2.0]

    def test_truncated_body_is_rate_limit_retry_then_ok(self, monkeypatch):
        slept = _patch_sleep(monkeypatch)
        # 前两次 body 被截断成 2 点（限流特征），第三次正常
        sess = FakeSession([_payload(_body(2)), _payload(_body(2)),
                            _payload(_body(40))])
        monkeypatch.setattr(m, "_SESSION", sess)
        out = m._fetch_baidu("总市值", "00700", "近三年")
        assert len(out) == 40
        assert out[0] == (date(2026, 1, 5), 1.5)
        assert sess.calls == 3
        assert slept == [2.0, 2.0]

    def test_short_but_valid_body_accepted_without_retry(self, monkeypatch):
        """上市历史短、点数本就少的正常响应必须直接采纳（旧版 min_pts=30 误判限流）。"""
        slept = _patch_sleep(monkeypatch)
        sess = FakeSession([_payload(_body(5))])
        monkeypatch.setattr(m, "_SESSION", sess)
        out = m._fetch_baidu("总市值", "00700", "近三年")
        assert len(out) == 5
        assert sess.calls == 1
        assert slept == []

    def test_no_result_means_confirmed_empty(self, monkeypatch):
        slept = _patch_sleep(monkeypatch)
        sess = FakeSession([{"ResultCode": 0, "Result": []}])
        monkeypatch.setattr(m, "_SESSION", sess)
        assert m._fetch_baidu("总市值", "00700", "近三年") == []
        assert sess.calls == 1            # 确认无数据不重试
        assert slept == []

    def test_empty_body_means_confirmed_empty(self, monkeypatch):
        monkeypatch.setattr(m, "_SESSION", FakeSession([_payload([])]))
        assert m._fetch_baidu("总市值", "00700", "近三年") == []

    def test_all_zero_values_means_confirmed_empty(self, monkeypatch):
        body = [[f"2026-01-{d:02d}", "0"] for d in range(5, 10)]
        monkeypatch.setattr(m, "_SESSION", FakeSession([_payload(body)]))
        assert m._fetch_baidu("总市值", "00700", "近三年") == []

    def test_network_error_retries_then_none(self, monkeypatch):
        slept = _patch_sleep(monkeypatch)
        sess = FakeSession([OSError("conn reset")])
        monkeypatch.setattr(m, "_SESSION", sess)
        assert m._fetch_baidu("总市值", "00700", "近三年") is None
        assert sess.calls == 3
        assert slept == [2.0, 2.0, 2.0]

    def test_structural_broken_body_retries(self, monkeypatch):
        slept = _patch_sleep(monkeypatch)
        sess = FakeSession([{"ResultCode": 0, "Result": [{"DisplayData": {}}]}])
        monkeypatch.setattr(m, "_SESSION", sess)
        assert m._fetch_baidu("总市值", "00700", "近三年") is None
        assert sess.calls == 3
        assert slept == [2.0, 2.0, 2.0]

    def test_uses_shared_session(self, monkeypatch):
        """必须走模块级 Session（连接复用），不能每次 requests.get。"""
        sess = FakeSession([_payload(_body(30))])
        monkeypatch.setattr(m, "_SESSION", sess)
        m._fetch_baidu("总市值", "00700", "近三年")
        assert sess.calls == 1


# ---------------------------------------------------------------------------
# 2. _build_col_series（并发不改合并语义）
# ---------------------------------------------------------------------------
class TestBuildColSeries:
    def test_merge_order_preserved_and_all_jobs_fired(self, monkeypatch):
        calls = []

        def fake_fetch(indicator, code5, period):
            calls.append((indicator, period))
            if indicator == "总市值" and period == "近三年":
                return [(date(2026, 1, 5), 1.0)]
            if indicator == "总市值" and period == "全部":
                return [(date(2026, 1, 5), 999.0), (date(2025, 1, 5), 2.0)]
            return None

        monkeypatch.setattr(m, "_fetch_baidu", fake_fetch)
        got = m._build_col_series("00700")

        # 8 个请求全部发起（4 指标 × 2 period）
        assert len(calls) == 8
        assert {c[0] for c in calls} == set(m.INDICATORS)
        assert {c[1] for c in calls} == set(m.PERIODS)
        # 近三年已填充的日期不被"全部"的降采样值覆盖；缺口由全部补上
        assert got["total_market_val"] == {date(2026, 1, 5): 1e8,
                                           date(2025, 1, 5): 2e8}
        # 无数据的指标保持空 dict
        assert got["pe_ratio"] == {}
        assert got["pb_ratio"] == {}

    def test_confirmed_empty_contributes_nothing(self, monkeypatch):
        monkeypatch.setattr(m, "_fetch_baidu",
                            lambda ind, c, p: [] if ind == "市盈率(静)" else None)
        got = m._build_col_series("00700")
        assert got["pe_ratio"] == {}


# ---------------------------------------------------------------------------
# 3. _rolling_52w（单调队列 == 朴素参考）
# ---------------------------------------------------------------------------
def _naive_rolling_52w(highs, lows, win):
    h52, l52 = [], []
    for i in range(len(highs)):
        lo = max(0, i - win + 1)
        wh = [x for x in highs[lo:i + 1] if x is not None]
        wl = [x for x in lows[lo:i + 1] if x is not None]
        h52.append(max(wh) if wh else None)
        l52.append(min(wl) if wl else None)
    return h52, l52


class TestRolling52w:
    def test_matches_naive_reference_random(self):
        rng = random.Random(42)
        for trial in range(20):
            n = rng.randint(1, 600)
            highs = [None if rng.random() < 0.1 else round(rng.uniform(1, 100), 2)
                     for _ in range(n)]
            lows = [None if rng.random() < 0.1 else round(rng.uniform(1, 100), 2)
                    for _ in range(n)]
            h52, l52 = m._rolling_52w(highs, lows)
            eh, el = _naive_rolling_52w(highs, lows, m.WIN_52W)
            assert h52 == eh, f"trial {trial} high mismatch"
            assert l52 == el, f"trial {trial} low mismatch"

    def test_all_none_returns_all_none(self):
        highs = [None] * 300
        lows = [None] * 300
        h52, l52 = m._rolling_52w(highs, lows)
        assert h52 == [None] * 300
        assert l52 == [None] * 300

    def test_window_eviction(self):
        """第 253 个交易日起，窗口必须滑出最早的值。"""
        highs = [float(i) for i in range(300)]
        lows = [float(i) for i in range(300)]
        h52, l52 = m._rolling_52w(highs, lows)
        assert h52[251] == 251.0        # 窗口 [0, 251]
        assert h52[252] == 252.0        # 窗口 [1, 252]，0 被滑出
        assert h52[299] == 299.0
        assert l52[252] == 1.0
