#!/usr/bin/env python3
"""
健康检查 —— 每次改动后跑一次，确保核心链路不炸
用法：python3 health_check.py
"""
import sys, os, importlib, traceback
from datetime import datetime

PASS, FAIL = "✓", "✗"
results = []


def check(name):
    def decorator(fn):
        def wrapper():
            try:
                fn()
                results.append((PASS, name))
            except Exception as e:
                results.append((FAIL, f"{name} -> {e}"))
        return wrapper
    return decorator


# ── 1. 数据库连通 ──
@check("数据库连通")
def check_db():
    from db import get_conn
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        conn.commit()


# ── 2. import 完整性（不触发 Futu/Aibot 连接） ──
@check("get_quote.py 语法/import")
def check_get_quote():
    import get_quote
    assert hasattr(get_quote, 'sys')


@check("get_benchmark.py 语法/import")
def check_get_benchmark():
    import get_benchmark


@check("get_excess_return.py 语法/import")
def check_get_excess_return():
    import get_excess_return


@check("backfill_benchmark.py 语法/import")
def check_backfill_benchmark():
    import backfill_benchmark


@check("market_scheduler.py 语法/import")
def check_market_scheduler():
    from market_scheduler import SCHEDULES, is_trading_hours
    assert len(SCHEDULES) >= 1


@check("wecom_server_collector.py 语法/import")
def check_wecom_server():
    import wecom_server_collector
    # 确认 generate_req_id 可访问（上次的 bug）
    assert wecom_server_collector.generate_req_id is not None
    # 确认关键函数存在
    assert hasattr(wecom_server_collector, 'handle_collect')
    assert hasattr(wecom_server_collector, 'handle_realtime')
    assert hasattr(wecom_server_collector, 'handle_trend')


# ── 3. 数据库表/视图存在性 ──
@check("核心表 daily_quote 存在")
def check_table_quote():
    from db import get_conn
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM daily_quote LIMIT 0")
        conn.commit()


@check("核心表 daily_benchmark 存在")
def check_table_benchmark():
    from db import get_conn
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM daily_benchmark LIMIT 0")
        conn.commit()


@check("视图 v_daily_excess_return 存在")
def check_view_excess():
    from db import get_conn
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM v_daily_excess_return LIMIT 0")
        conn.commit()


# ── 4. 关键数据完整性 ──
@check("daily_benchmark 覆盖 recent 交易日")
def check_benchmark_coverage():
    from db import get_conn
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM daily_benchmark
            WHERE bench_code = 'HK.800000'
              AND trade_date >= (SELECT MIN(trade_date) FROM daily_quote WHERE stock_code = 'HK.00700')
        """)
        cnt = cur.fetchone()[0]
        conn.commit()
    assert cnt > 0, "benchmark 没有覆盖 daily_quote 的日期范围"


@check("视图 v_daily_excess_return 有数据 (HK.00700)")
def check_excess_return_data():
    from db import get_conn
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM v_daily_excess_return WHERE stock_code = 'HK.00700'")
        cnt = cur.fetchone()[0]
        conn.commit()
    assert cnt > 0, "超额收益视图无数据"


def main():
    print(f"健康检查 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("-" * 40)

    # 按定义的顺序执行
    import __main__
    checks = [(name, obj) for name, obj in vars(__main__).items()
              if callable(obj) and name.startswith('check_')]

    for _, fn in checks:
        fn()

    print("-" * 40)
    passed = sum(1 for s, _ in results if s == PASS)
    failed = sum(1 for s, _ in results if s == FAIL)
    for status, name in results:
        print(f"  {status} {name}")
    print(f"\n通过: {passed}/{passed+failed}")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
