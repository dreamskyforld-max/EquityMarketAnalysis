"""sync_quote_universe.py 软删门控单元测试（假连接，不连库）。

背景（2026-10-26 事故）：HK/SH 拉 get_stock_basicinfo 连续超时（按设计「跳过、保留旧清单」），
但软删无条件全表执行——两市场 last_seen 早已停在 9 月，超 30 天窗口 → 6167 只被置
is_collectable=FALSE。次日起 v_quote_scope 只剩 realtime_collect_target 的 77 只港股 +
4 只沪股，hk/a_daily_quote 静默断供 4 个交易日，调度还记「成功」。

本文件锁住修复后的契约：
  1. 软删只作用于「本轮拉取 + 写入都成功」的市场（失败市场的 last_seen 是旧值，不能当退市）；
  2. 本轮无任何市场成功 → 一条 SQL 都不执行（绝不整表误杀）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sync_quote_universe as u  # noqa: E402


class _Cur:
    """记录 SQL/参数的最小 cursor（支持 with 语法）。"""

    def __init__(self, rowcount=7):
        self.sql = None
        self.params = None
        self.rowcount = rowcount

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, rowcount=7):
        self.cur = _Cur(rowcount)

    def cursor(self):
        return self.cur


def test_soft_delete_scoped_to_ok_markets():
    """只有本轮成功的市场才进软删 SQL（事故场景：HK/SH 失败、SZ 成功 → 仅 SZ）。"""
    conn = _Conn(rowcount=5)
    assert u.soft_delete_scope(conn, ["SZ"], 30) == 5
    assert "market = ANY(%s)" in conn.cur.sql
    assert conn.cur.params == (["SZ"], 30)


def test_soft_delete_skips_when_no_market_succeeded():
    """三个市场全部拉取失败 → 不软删任何行（旧实现会整表误杀）。"""
    conn = _Conn()
    assert u.soft_delete_scope(conn, [], 30) == 0
    assert conn.cur.sql is None


def test_soft_delete_still_uses_last_seen_window():
    """按市场门控不改变原有 30 天窗口语义（真退市仍能被软删）。"""
    conn = _Conn()
    u.soft_delete_scope(conn, ["HK", "SH", "SZ"], 30)
    assert "last_seen < NOW() - make_interval(days => %s)" in conn.cur.sql
    assert conn.cur.params == (["HK", "SH", "SZ"], 30)
