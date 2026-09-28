"""snap_guard.py 单元测试（纯 mock：不连富途、不写库）。

背景（2026-09-21）：富途 get_market_snapshot 是整批原子接口，批内任一「未知股票」
废掉整批 400 只（HK.03888 断供事故）。本文件锁住快照自愈机制的四个安全阀：
  1. 只有「未知股票」错误允许拉黑（限流/断线绝不拉黑）；
  2. 提取的坏码必须落在当前批次内（防双前缀场景把正常码误拉黑）；
  3. **发现即拉黑**（2026-09-28 定稿）：提取到坏码当轮即写黑名单，不等重试成功——
     失败批次不白学，黑名单永久有效（人工 CLI 解除）；
  4. 修复有预算/轮次上限（防限流雪崩），但耗尽时坏码已落黑名单。
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import snap_guard as g  # noqa: E402

_RATE_LIMIT_MSG = "获取市场快照频率太高，请求失败，每30秒最多60次。"


class _SnapCtx:
    """模拟富途 get_market_snapshot：批内任一代码不在 valid 里 → 整批「未知股票」。

    复刻真实原子语义（一批里坏 1 个 → 整批 400 只全失败）。
    """

    def __init__(self, valid):
        self.valid = set(valid)
        self.batches = []

    def get_market_snapshot(self, batch):
        self.batches.append(list(batch))
        bad = [c for c in batch if c not in self.valid]
        if bad:
            return -1, f"未知股票 {bad[0]}"
        return 0, pd.DataFrame([{"code": c} for c in batch])


class _RateLimitCtx:
    def __init__(self):
        self.calls = 0

    def get_market_snapshot(self, batch):
        self.calls += 1
        return -1, _RATE_LIMIT_MSG


# ── 错误识别与代码提取 ──────────────────────────────────────────────

def test_is_unknown_stock_err():
    assert g.is_unknown_stock_err("未知股票 02900")
    assert g.is_unknown_stock_err("未知股票 HK.00001")
    assert not g.is_unknown_stock_err(_RATE_LIMIT_MSG)
    assert not g.is_unknown_stock_err("连接断开")


def test_is_rate_limit_err():
    assert g.is_rate_limit_err(_RATE_LIMIT_MSG)
    assert not g.is_rate_limit_err("未知股票 02900")


def test_extract_bad_codes():
    assert g.extract_bad_codes("未知股票 02900", market="HK") == ["HK.02900"]
    assert g.extract_bad_codes("未知股票 HK.00001") == ["HK.00001"]
    assert g.extract_bad_codes("未知股票 02900,02905", market="HK") == ["HK.02900", "HK.02905"]
    assert g.extract_bad_codes("未知股票 02900、02905", market="HK") == ["HK.02900", "HK.02905"]
    # 文案里其它数字（错误码/括号说明）不误提
    assert g.extract_bad_codes("未知股票 02900，（错误码 10001）", market="HK") == ["HK.02900"]
    # 非「未知股票」错误不提取
    assert g.extract_bad_codes(_RATE_LIMIT_MSG, market="HK") == []


def test_repair_budget():
    b = g.RepairBudget(2)
    assert b.take() and b.take()
    assert not b.take()          # 预算耗尽
    assert g.RepairBudget(0).take() is False


# ── 快照自愈闭环 ────────────────────────────────────────────────────

def test_snapshot_batch_repairs_bad_code_and_marks(monkeypatch):
    """批次含坏码 → 剔除重试成功 → 写入黑名单（且只写确认的坏码）。"""
    marked = []
    monkeypatch.setattr(g, "mark_snap_bad",
                        lambda codes, reason="", market=None: marked.extend(codes) or len(codes))
    ctx = _SnapCtx(valid=["HK.00700", "HK.03888"])
    budget = g.RepairBudget(5)

    ret, data, removed = g.snapshot_batch(
        ctx, ["HK.00700", "HK.02900", "HK.03888"], market="HK", budget=budget)

    assert ret == 0 and removed == ["HK.02900"]
    assert marked == ["HK.02900"]
    assert ctx.batches[0] == ["HK.00700", "HK.02900", "HK.03888"]   # 首次整批
    assert ctx.batches[1] == ["HK.00700", "HK.03888"]               # 剔除后重试
    assert len(data) == 2
    assert budget.left == 4                                          # 只消耗 1 次修复调用


def test_snapshot_batch_marks_even_if_retry_fails(monkeypatch):
    """发现坏码当轮即拉黑（2026-09-28 定稿）：剔除后重试即使再失败，黑名单也已落账。"""
    marked = []
    monkeypatch.setattr(g, "mark_snap_bad",
                        lambda codes, reason="", market=None: marked.extend(codes) or len(codes))
    monkeypatch.setattr(g.time, "sleep", lambda s: None)

    class _Ctx:
        def __init__(self):
            self.n = 0

        def get_market_snapshot(self, batch):
            self.n += 1
            if self.n == 1:
                return -1, "未知股票 02900"
            return -1, _RATE_LIMIT_MSG          # 剔除后重试撞限流

    ret, data, removed = g.snapshot_batch(_Ctx(), ["HK.00700", "HK.02900"],
                                          market="HK", budget=g.RepairBudget(5))
    assert ret != 0 and removed == ["HK.02900"]
    assert marked == ["HK.02900"]             # 证据成立即落账，不等重试成功


def test_snapshot_batch_rate_limit_never_marks(monkeypatch):
    """限流：退避重试一次仍失败即放弃，绝不拉黑。"""
    marked = []
    monkeypatch.setattr(g, "mark_snap_bad",
                        lambda codes, reason="", market=None: marked.extend(codes) or len(codes))
    monkeypatch.setattr(g.time, "sleep", lambda s: None)

    ctx = _RateLimitCtx()
    ret, data, removed = g.snapshot_batch(ctx, ["HK.00700"], market="HK", budget=g.RepairBudget(5))
    assert ret != 0 and removed == []
    assert ctx.calls == 2                     # 首次 + 退避重试一次
    assert marked == []


def test_snapshot_batch_double_prefix_not_mis_blacklisted(monkeypatch):
    """回归锁（2026-09-15 事故场景）：错误文案里的代码不在本批次 → 不误伤、不拉黑。

    HK.HK.00700 的报错是「未知股票 HK.00700」，但 HK.00700（正常码）不在批次里，
    若不做交集校验就会把正常码拉黑。
    """
    marked = []
    monkeypatch.setattr(g, "mark_snap_bad",
                        lambda codes, reason="", market=None: marked.extend(codes) or len(codes))
    ctx = _SnapCtx(valid=["HK.00700"])
    ret, data, removed = g.snapshot_batch(ctx, ["HK.HK.00700"], market="HK", budget=g.RepairBudget(5))
    assert ret != 0 and removed == []
    assert marked == []
    assert len(ctx.batches) == 1              # 无法定位 → 不做无意义重试


def test_snapshot_batch_budget_exhausted_still_marks(monkeypatch):
    """预算耗尽 → 不再重试，但已提取到的坏码仍落黑名单（失败批不白学）。"""
    marked = []
    monkeypatch.setattr(g, "mark_snap_bad",
                        lambda codes, reason="", market=None: marked.extend(codes) or len(codes))
    ctx = _SnapCtx(valid=["HK.00700"])
    ret, data, removed = g.snapshot_batch(ctx, ["HK.00700", "HK.02900"],
                                          market="HK", budget=g.RepairBudget(0))
    assert ret != 0 and removed == []
    assert marked == ["HK.02900"]


def test_snapshot_batch_max_rounds_exhausted_still_marks(monkeypatch):
    """轮次上限耗尽 → 整批放弃，但已暴露的坏码全部落账（含最后暴露、未及剔除的那个）。"""
    marked = []
    monkeypatch.setattr(g, "mark_snap_bad",
                        lambda codes, reason="", market=None: marked.extend(codes) or len(codes))
    ctx = _SnapCtx(valid=["HK.00700"])        # 批内 3 个坏码，max_rounds=2 必然耗尽
    ret, data, removed = g.snapshot_batch(
        ctx, ["HK.00700", "HK.02901", "HK.02902", "HK.02903"],
        market="HK", budget=g.RepairBudget(50), max_rounds=2)

    assert ret != 0                                          # 轮次耗尽 → 整批放弃
    assert removed == ["HK.02901", "HK.02902"]               # 只有被剔除的 2 个算 removed
    assert marked == ["HK.02901", "HK.02902", "HK.02903"]    # 暴露即落账（最后 1 个也落）


def test_snapshot_batch_other_error_passthrough(monkeypatch):
    """非「未知股票」错误原样返回（连接断开等），不重试、不拉黑。"""
    marked = []
    monkeypatch.setattr(g, "mark_snap_bad",
                        lambda codes, reason="", market=None: marked.extend(codes) or len(codes))

    class _Ctx:
        def get_market_snapshot(self, batch):
            return -1, "连接断开"

    ret, data, removed = g.snapshot_batch(_Ctx(), ["HK.00700"], market="HK", budget=g.RepairBudget(5))
    assert ret != 0 and removed == [] and marked == []


def test_snapshot_batch_all_codes_removed(monkeypatch):
    """极端：整批全被判未知股票 → 清空后返回失败，不死循环；暴露的坏码已落黑名单。"""
    marked = []
    monkeypatch.setattr(g, "mark_snap_bad",
                        lambda codes, reason="", market=None: marked.extend(codes) or len(codes))
    ctx = _SnapCtx(valid=[])                  # 所有代码都判未知
    ret, data, removed = g.snapshot_batch(ctx, ["HK.02900"], market="HK", budget=g.RepairBudget(5))
    assert ret != 0
    assert removed == ["HK.02900"]
    assert len(ctx.batches) == 1              # 剔除后批空 → 立即返回，不再调用
    assert marked == ["HK.02900"]             # 逐个暴露过程中即落账
