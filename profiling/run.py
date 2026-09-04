#!/usr/bin/env python3
"""股票画像系统 CLI

    python3 -m profiling.run init               建 schema + 同步标签字典
    python3 -m profiling.run sync               只同步标签字典
    python3 -m profiling.run list               列出已注册标签（含状态）
    python3 -m profiling.run compute            计算全部标签
    python3 -m profiling.run compute --domain ① 计算 ① 证券属性域
    python3 -m profiling.run compute --tag idt_market --dry-run
    python3 -m profiling.run backfill-basic     补 stock_info（上市日期/交易所/缺失股票）
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

log = logging.getLogger(__name__)


def _print_tags():
    from . import registry

    print(f"{'tag_code':22s}{'域':12s}{'类型':8s}{'频率':10s}{'多重':6s}{'状态':16s}名称")
    print("-" * 96)
    for m in sorted(registry.all_tags(), key=lambda x: (x.domain, x.code)):
        print(
            f"{m.code:22s}{m.domain:12s}{m.value_type:8s}{m.update_freq:10s}"
            f"{'是' if m.multi_value else '-':6s}{m.status:16s}{m.name}"
        )
    print(f"\n合计 {len(registry.all_tags())} 个标签，域：{', '.join(registry.domains())}")


def _sampling_dates(conn, start: date, end: date, freq: str) -> list:
    """从交易日历（a_daily_quote）取区间内交易日，按频率取样。

    D = 每个交易日；W = 每 5 个交易日取 1 个；M = 每月最后一个交易日（默认）。
    月末取样覆盖绝大多数中低频回测需求，数据量只有全量回算的约 1/20。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT trade_date FROM a_daily_quote "
            "WHERE trade_date BETWEEN %s AND %s ORDER BY trade_date",
            (start, end),
        )
        days = [r[0] for r in cur.fetchall()]
    if not days:
        return []
    if freq == "D":
        return days
    if freq == "W":
        return days[2::5]
    last = {}
    for d in days:
        last[(d.year, d.month)] = d
    return sorted(last.values())


def _print_results(results: list, aggregate: bool = False) -> None:
    """单日：逐条列出；区间批量：按标签聚合（否则输出会淹没在行数里）。"""
    if not aggregate:
        print(f"\n{'tag_code':24s}{'模式':10s}{'行数':>8s}{'新增':>8s}{'关闭':>8s}  说明")
        print("-" * 96)
        for r in results:
            print(
                f"{r['tag_code']:24s}{r.get('mode', ''):10s}{r['rows_total']:>8d}"
                f"{r['rows_new']:>8d}{r['rows_closed']:>8d}  {r.get('message', '')}"
            )
        return

    agg: dict = {}
    for r in results:
        a = agg.setdefault(r["tag_code"], {"total": 0, "new": 0, "closed": 0, "err": 0, "days": 0})
        a["total"] += r["rows_total"]
        a["new"] += r["rows_new"]
        a["closed"] += r["rows_closed"]
        a["days"] += 1
        if r["status"] == "error":
            a["err"] += 1
    print(f"\n{'tag_code':24s}{'日期数':>7s}{'行数':>10s}{'新增':>10s}{'关闭':>10s}  失败")
    print("-" * 88)
    for code, a in sorted(agg.items()):
        print(
            f"{code:24s}{a['days']:>7d}{a['total']:>10,}{a['new']:>10,}"
            f"{a['closed']:>10,}  {a['err'] or ''}"
        )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="profiling", description="股票画像系统")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="建 profile schema + 同步标签字典")
    sub.add_parser("sync", help="同步标签字典")
    sub.add_parser("list", help="列出已注册标签")

    c = sub.add_parser("compute", help="计算标签")
    c.add_argument("--tag", action="append", help="指定标签 code（可重复）")
    c.add_argument("--domain", help="按域名称匹配计算，如 规模属性")
    c.add_argument("--as-of", help="计算日期 YYYY-MM-DD（默认今天；指定历史日期会自动跳过更新门禁并按版本模式写入）")
    c.add_argument("--date-range", help="批量补算历史区间 START:END（YYYY-MM-DD），需配合 --freq，按版本模式写入")
    c.add_argument("--freq", choices=["M", "W", "D"], default="M",
                   help="区间取样频率：M=月末（默认）/ W=周中 / D=每个交易日")
    c.add_argument("--dry-run", action="store_true", help="只算出 diff 不写库")
    c.add_argument("--force", action="store_true",
                   help="忽略 update_freq 更新门禁，强制重算（补完依赖数据后刷新标签用）")
    c.add_argument("--mode", choices=["auto", "snapshot", "version"], default="auto",
                   help="写入模式：auto=当天快照/历史版本（默认）；snapshot=原地更新不产生版本；version=完整版本化")

    sub.add_parser("backfill-basic", help="补 stock_info：缺失股票 + 上市日期/交易所类型")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    # 导入 tags 包触发标签注册
    from . import tags  # noqa: F401
    from . import registry, schema, engine
    from db import get_conn

    if args.cmd == "list":
        _print_tags()
        return 0

    if args.cmd == "backfill-basic":
        from .backfill import stock_basic

        print(stock_basic.run())
        return 0

    with get_conn() as conn:
        if args.cmd in ("init", "sync"):
            if args.cmd == "init":
                schema.ensure_schema(conn)
                log.info("profile schema 就绪")
            ins, upd = registry.sync_registry(conn)
            log.info("同步结果：新增 %d，更新 %d", ins, upd)
            return 0

        if args.cmd == "compute":
            today = date.today()
            as_of = date.fromisoformat(args.as_of) if args.as_of else today
            # 显式指定历史日期 = 针对性补算，默认跳过更新门禁
            # （否则 static/quarterly 标签会被「距上次版本不足 N 天」拦住，历史区间算不动）
            force = args.force or as_of < today

            def _run_one(d: date):
                if args.tag:
                    return [
                        engine.compute_tag(conn, t, as_of=d, dry_run=args.dry_run,
                                           force=force, mode=args.mode)
                        for t in args.tag
                    ]
                if args.domain:
                    return engine.compute_domain(conn, args.domain, as_of=d, dry_run=args.dry_run,
                                                 force=force, mode=args.mode)
                return engine.compute_all(conn, as_of=d, dry_run=args.dry_run,
                                          force=force, mode=args.mode)

            if args.date_range:
                start_s, end_s = args.date_range.split(":")
                dates = _sampling_dates(conn, date.fromisoformat(start_s),
                                        date.fromisoformat(end_s), args.freq)
                log.info("区间 %s ~ %s，按 %s 取样 %d 个日期", start_s, end_s, args.freq, len(dates))
                results = []
                for i, d in enumerate(dates):
                    log.info("[%d/%d] as_of=%s", i + 1, len(dates), d)
                    results.extend(_run_one(d))
                _print_results(results, aggregate=True)
            else:
                results = _run_one(as_of)
                _print_results(results)

            failed = [r for r in results if r["status"] == "error"]
            if failed:
                log.error("%d 次计算失败", len(failed))
                return 1
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
