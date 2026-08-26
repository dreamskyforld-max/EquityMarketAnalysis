#!/usr/bin/env python3
"""
stock_sector 表 stock_code 前缀大小写订正 — 历史脏数据修复脚本

问题背景：
    旧版 get_stock_sector._fetch_a_industry_baostock 用 code.lower() 入库，
    导致 A 股证监会行业分类行的 stock_code 以 sh./sz./bj. 小写前缀写入，
    与港股 HK.00700 及其它 A 股路径(SH./SZ./BJ.) 的大写前缀不一致。
    stock_code 是大小写敏感的复合主键(stock_code, sector_code, sector_type)，
    小写前缀会造成同一只 A 股出现 sh.600000 / SH.600000 两套主键，下游
    按 stock_code 精确 join/聚合时漏匹配。

本脚本作用：
    把 stock_sector 中 stock_code 前缀为 sh./sz./bj. 的行 UPDATE 成
    SH./SZ./BJ.（仅改前缀，保留 . 之后的代码主体）。幂等，可重复执行。

安全性：
    - 复合主键在大小写归一后无冲突（同一股票的行业/指数/概念行互不重叠），
      UPDATE 不会触发主键冲突（已实测 TRUE_PK_CONFLICT=0）。
    - 默认 --dry-run（只统计、不落库）；加 --execute 才真正写入。

用法：
    python3 fix_stock_sector_case.py            # 默认 dry-run，只统计
    python3 fix_stock_sector_case.py --execute  # 真正订正并落库
    python3 fix_stock_sector_case.py --check    # 仅检查残留小写（退出码非0表示有残留）
"""
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("fix_stock_sector_case")

# 小写前缀 -> 大写前缀映射
_PREFIX_MAP = {"sh": "SH", "sz": "SZ", "bj": "BJ"}


def count_lowercase(conn):
    """统计各小写前缀待订正的行数，返回 (明细dict, 总数)。"""
    totals = {}
    with conn.cursor() as cur:
        for low, up in _PREFIX_MAP.items():
            cur.execute(
                "SELECT COUNT(*) FROM stock_sector WHERE stock_code LIKE %s || '.%%'",
                (low,),
            )
            n = cur.fetchone()[0]
            if n:
                totals[up] = n
    return totals, sum(totals.values())


def execute(conn, dry_run=True):
    """执行订正。dry_run=True 时只统计不落库（不 commit）。返回修动行数。"""
    totals, total = count_lowercase(conn)
    if not totals:
        log.info("无小写前缀脏数据，无需订正")
        return 0

    log.info("检测到小写前缀脏数据：%s，合计 %d 行", totals, total)
    if dry_run:
        log.info("[dry-run] 未落库，加 --execute 真正写入")
        return 0

    fixed = 0
    with conn.cursor() as cur:
        for low, up in _PREFIX_MAP.items():
            # SUBSTRING(stock_code FROM 3) 取第3个字符起(含 '.')，保留 'sh.' 中的点，
            # 拼成 'SH' || '.600000' = 'SH.600000'。注意不能用 FROM 4(会跳过点导致 SH600000)。
            cur.execute(
                "UPDATE stock_sector "
                "SET stock_code = %s || SUBSTRING(stock_code FROM 3) "
                "WHERE stock_code LIKE %s || '.%%'",
                (up, low),
            )
            fixed += cur.rowcount
    conn.commit()
    log.info("订正完成：共更新 %d 行（sh/sz/bj → SH/SZ/BJ）", fixed)
    return fixed


def check_residual(conn):
    """检查是否仍有残留小写前缀（大小写不敏感匹配）。返回残留行数。"""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM stock_sector WHERE stock_code ~* '^[shszbj]\\.'")
        return cur.fetchone()[0]


def run(argv=None):
    argv = list(sys.argv if argv is None else argv)
    dry_run = "--execute" not in argv
    only_check = "--check" in argv

    from db import get_conn
    with get_conn() as conn:
        if only_check:
            residual = check_residual(conn)
            log.info("残留小写前缀行数：%d", residual)
            return 1 if residual else 0

        if dry_run:
            execute(conn, dry_run=True)
            residual = check_residual(conn)
            log.info("当前残留小写前缀行数：%d", residual)
            return 0

        execute(conn, dry_run=False)
        residual = check_residual(conn)
        if residual:
            log.warning("订正后仍有 %d 行残留小写（可能为非 sh/sz/bj 其它前缀），需人工排查", residual)
        else:
            log.info("订正后无残留小写前缀")
        return 0


if __name__ == "__main__":
    sys.exit(run())
