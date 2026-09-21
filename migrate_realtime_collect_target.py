#!/usr/bin/env python3
"""
一次性迁移：实时采集池 is_active → realtime_collect_target

背景
----
实时采集池的真相源由 stock_info.is_active 切换为 realtime_collect_target：
  · 行存在 = 在池（等价旧 is_active=TRUE）→ 日频任务（收盘 get_quote/get_trend、
    相关性、宏观）按「在表」全采；
  · collect_tick / collect_trend / collect_quote 三开关分别控制
    实时逐笔 / 盘中分钟级 / QUOTE(LV1) 推送。

迁移内容（幂等，可重复执行）
--------------------------
  1) stock_info.is_active=TRUE 的行 → 插入新表（collect_tick=TRUE, collect_trend=TRUE）；
  2) config.conf [ticker] quote_stocks 命中的代码 → 该行 collect_quote=TRUE；
  3) 已存在的行一律不覆盖（保护前端已手工调整过的开关）。

注意
----
  · 只插不改：重复执行只补齐缺失行，绝不把已改 FALSE 的开关翻回 TRUE；
  · 前置：目标库需已应用 sql/schema.sql 中的 realtime_collect_target 定义；
  · 跑完后需重启 ticker-collector / market-scheduler 使新表生效；
  · 服务器与本机库都需要执行（本机用于分析与测试）。

用法
----
    .venv/bin/python3 migrate_realtime_collect_target.py            # 执行
    .venv/bin/python3 migrate_realtime_collect_target.py --dry-run  # 仅预览
"""
import argparse
import logging

from db import get_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("migrate_collect_target")

MIGRATOR = "migrate_from_is_active"


def _load_quote_stocks() -> set:
    """config.conf [ticker] quote_stocks（旧的 QUOTE 推送订阅名单）→ set。"""
    from config import val
    raw = val("ticker", "quote_stocks", fallback="")
    return {s.strip() for s in raw.split(",") if s.strip()}


def main():
    ap = argparse.ArgumentParser(description="迁移 is_active → realtime_collect_target")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写库")
    args = ap.parse_args()

    quote_stocks = _load_quote_stocks()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.realtime_collect_target')")
            if cur.fetchone()[0] is None:
                raise SystemExit(
                    "目标库尚未创建 realtime_collect_target 表。\n"
                    "请先应用 sql/schema.sql 中该表定义（服务器侧走 ./deploy.sh up 的 schema 同步）。"
                )

            # 源列已被删除（迁移完成后的清理）→ 无可迁移内容，直接退出
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='stock_info' "
                "AND column_name='is_active'"
            )
            if cur.fetchone()[0] == 0:
                log.info("stock_info.is_active 列已删除（迁移早已完成），无需执行。")
                return

            cur.execute(
                "SELECT stock_code FROM stock_info WHERE is_active = TRUE ORDER BY stock_code"
            )
            active = [r[0] for r in cur.fetchall()]

            cur.execute(
                "SELECT stock_code FROM realtime_collect_target ORDER BY stock_code"
            )
            existing = {r[0] for r in cur.fetchall()}

            to_insert = [c for c in active if c not in existing]

            log.info("配置现状：")
            log.info("  stock_info.is_active=TRUE : %d 只", len(active))
            log.info("  realtime_collect_target   : %d 只（已存在，不覆盖）", len(existing))
            log.info("  config.conf quote_stocks  : %s", sorted(quote_stocks) or "（空）")
            log.info("待插入 %d 只：%s%s", len(to_insert), to_insert[:20],
                     " …" if len(to_insert) > 20 else "")

            if args.dry_run:
                log.info("[dry-run] 未写库")
                return

            inserted = 0
            for code in to_insert:
                cur.execute(
                    """
                    INSERT INTO realtime_collect_target
                        (stock_code, collect_tick, collect_trend, collect_quote, applied_by)
                    VALUES (%s, TRUE, TRUE, %s, %s)
                    ON CONFLICT (stock_code) DO NOTHING
                    """,
                    (code, code in quote_stocks, MIGRATOR),
                )
                inserted += cur.rowcount

            log.info("完成：新增 %d 只（已存在行未改动）", inserted)
            log.info("下一步：重启采集服务使配置生效 —— ticker-collector + market-scheduler")


if __name__ == "__main__":
    main()
