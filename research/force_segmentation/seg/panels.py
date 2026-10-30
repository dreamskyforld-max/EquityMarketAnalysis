"""分时切分研究 · 数据面板加载（只读，复用生产的 load_panel 口径）"""
from __future__ import annotations

import datetime as dt

import numpy as np

import db
import build_force_rounds as bfr


def load_panel(code: str, day: dt.date):
    """返回 (tt: list[str], p: np.ndarray)；分钟面板与生产 `rounds_by_trend` 同口径"""
    with db.get_conn() as conn, conn.cursor() as cur:
        th = bfr._thresholds(cur, code)
        rows = bfr.load_panel(cur, code, day, th)
    if not rows:
        return [], np.array([])
    return [r["t"] for r in rows], np.array([r["price"] for r in rows], float)


def recent_days(code: str, n: int = 10):
    """该股票最近 n 个有分钟面板的交易日（正序）"""
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT snapshot_date FROM trend_snapshot
                       WHERE stock_code=%s ORDER BY 1 DESC LIMIT %s""", (code, n * 2))
        days = sorted(r[0] for r in cur.fetchall())
    out = []
    for d in days:
        tt, p = load_panel(code, d)
        if len(tt) >= 60:
            out.append(d)
        if len(out) >= n:
            break
    return out


def codes_with_ticks(days: int = 14, limit: int = 20):
    """近 N 天有 tick_data 的股票（按 tick 数降序）"""
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT stock_code, COUNT(*) FROM tick_data
                       WHERE tick_time >= NOW() - INTERVAL '%s days'
                       GROUP BY 1 ORDER BY 2 DESC LIMIT %s""", (days, limit))
        return [r[0] for r in cur.fetchall()]
