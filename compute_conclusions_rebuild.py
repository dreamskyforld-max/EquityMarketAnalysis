#!/usr/bin/env python3
"""个股画像结论层「规则升级后全量重建」**手动脚本**（不注册 systemd 服务）。

用法：
    # 预演：只打印将删除的行数与规则版本，不改库
    .venv/bin/python3 compute_conclusions_rebuild.py
    # 确认后执行
    .venv/bin/python3 compute_conclusions_rebuild.py --yes
    # 指定基准日
    .venv/bin/python3 compute_conclusions_rebuild.py --yes --as-of 2026-11-12

执行过程全程 INFO 日志：规则版本、各 tag_code 待删除行数、删除进度、重新生产
统计、版本残留检查、耗时与实测峰值内存。需要降优先级时可自行包裹：
    nice -n 10 ionice -c2 -n7 .venv/bin/python3 compute_conclusions_rebuild.py --yes

⚠ 破坏性：会 DELETE 6 个 derived tag_code 的全部历史行，再按当前 L1 有效值重建，
   故默认预演，必须显式 --yes 才执行。重建前确认可接受「历史结论被当前规则回算值
   替换」（doc §4.4.4 已定不保留当时快照）。
⚠ PIT 局限（doc §8.5）：L1 当日走 snapshot 原地 UPDATE，历史日才是版本链，
   严格「按 as_of 取当时版本」不可得；重建的是「当前 L1 有效值 + 当前规则」下的结论。

资源安全不依赖 cgroup：R1/R6/R7 由 producer 的分批与实体过滤保证，R3 由本脚本
末尾打印实测峰值 RSS（本机基线约 149MB；对照规范 §2 R3 的 256MB 参考线）。
"""
from __future__ import annotations

import argparse
import platform
import resource
import time
from datetime import date

from log_utils import setup_logger
from profiling.conclusions import producer, rules as R

log = setup_logger("compute_conclusions_rebuild")


def _current_rows(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tag_code, count(*) FROM profile.tag_value "
            "WHERE tag_code = ANY(%s) GROUP BY tag_code",
            (list(R.DERIVED_TAGS),),
        )
        return dict(cur.fetchall())


def _residue_rows(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM profile.tag_value tv "
            "JOIN profile.tag_registry r ON r.tag_code = tv.tag_code "
            "WHERE r.source_type = 'derived' AND tv.tag_code = ANY(%s) "
            "AND tv.version IS DISTINCT FROM r.version",
            (list(R.DERIVED_TAGS),),
        )
        return cur.fetchone()[0]


def run(as_of: date, execute: bool) -> dict:
    from db import get_conn

    t0 = time.monotonic()
    versions = {c: R.RULES[c]["version"] for c in R.DERIVED_TAGS}
    log.info("=== 结论层全量重建开始 as_of=%s 模式=%s ===", as_of, "执行" if execute else "预演")
    log.info("规则版本: %s", versions)

    with get_conn() as conn:
        before = _current_rows(conn)
    for code in R.DERIVED_TAGS:
        log.info("  %-28s 历史行 %6d 行", code, before.get(code, 0))
    total = sum(before.values())
    log.info("合计待删除 %d 行（6 个 derived 标签的全部历史行）", total)

    if not execute:
        log.info("预演结束：未修改数据库。确认无误后加 --yes 执行。")
        return {"status": "dry_run", "rows_to_delete": total}

    log.info("开始删除历史结论行（按 tag_code 实体过滤，逐标签一事务）")
    result = producer.rebuild(as_of=as_of)
    log.info("重新生产完成 status=%s", result.get("status"))
    for code, stat in (result.get("stats") or {}).items():
        log.info("  %-28s inserted=%-6d closed=%-6d refreshed=%-6d",
                 code, stat["inserted"], stat["closed"], stat["refreshed"])

    with get_conn() as conn:
        residue = _residue_rows(conn)
        after = _current_rows(conn)
    log.info("残留旧版本行 = %d（应为 0）", residue)
    log.info("重建后行数合计 = %d", sum(after.values()))

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    mb = peak / 1024 / 1024 if platform.system() == "Darwin" else peak / 1024
    log.info("=== 完成 耗时=%.1fs 峰值内存=%.1fMB ===", time.monotonic() - t0, mb)
    return {"status": result.get("status"), "residue": residue,
            "peak_mb": round(mb, 1)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="compute_conclusions_rebuild",
        description="结论层规则升级后全量重建（手动执行，默认预演）",
    )
    ap.add_argument("--yes", action="store_true",
                    help="确认执行破坏性重建；不加则只预演、不改库")
    ap.add_argument("--as-of", help="基准日 YYYY-MM-DD（默认今天）")
    args = ap.parse_args(argv)
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    run(as_of, args.yes)
    return 0


if __name__ == "__main__":
    main()
