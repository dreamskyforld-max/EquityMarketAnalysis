#!/usr/bin/env python3
"""
从服务器同步 stock_info 中 is_active=FALSE 的行到本机数据库。

背景：
  - 服务器上已通过 sync_hk_quote_to_stock_info.py 批量补全了港股 stock_info
    （is_active=FALSE，作为参考数据，不触发采集）。
  - 本机需要这些中文名/映射数据用于分析，但不要覆盖本机现有 active 状态。
  - 因此只同步 is_active=FALSE 的行，且 ON CONFLICT 仅更新 stock_name，
    绝不触碰 is_active（保护本机原有 active=TRUE 的重点股票）。

方法（复用 sync_from_server.sh 的 SSH + psql COPY 模式）：
  1. ssh 到服务器，COPY 选出 is_active=FALSE 的目标列（排除 id/时间戳）到 stdout CSV
  2. 本机用临时表 COPY FROM，再 INSERT ... ON CONFLICT(stock_code)
     DO UPDATE SET stock_name=EXCLUDED.stock_name
  3. 打印前后计数

用法：
    .venv/bin/python3 sync_stock_info_inactive.py            # 执行同步
    .venv/bin/python3 sync_stock_info_inactive.py --dry-run  # 只统计、不写库
"""

import argparse
import logging
import subprocess

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# 与 sync_from_server.sh / config.conf [sync_server] 保持一致
SERVER_HOST = "47.242.62.243"
SERVER_USER = "root"
SERVER_PASS = "mkt_usr_Psw@90965"
REMOTE_DB = "market_db"
REMOTE_USER = "market_user"
REMOTE_HOST = "localhost"
REMOTE_PORT = "5432"

# stock_info 同步列（排除 id / created_at / updated_at，避免自增与时间戳冲突）
SYNC_COLS = "stock_code, stock_name, market, symbol, currency, is_active"


def fetch_remote_csv() -> str:
    """通过 SSH + 远端 psql COPY 把 is_active=FALSE 行导出为 CSV 文本。"""
    sql = (
        f"\\COPY (SELECT {SYNC_COLS} FROM stock_info WHERE is_active=FALSE) "
        f"TO STDOUT CSV HEADER"
    )
    # 用 stdin 传 SQL，避免 -c 的单引号/特殊字符嵌套问题
    proc = subprocess.run(
        ["ssh", f"{SERVER_USER}@{SERVER_HOST}",
         f"PGPASSWORD='{SERVER_PASS}' psql -q -X -h {REMOTE_HOST} -p {REMOTE_PORT} "
         f"-d {REMOTE_DB} -U {REMOTE_USER}"],
        input=sql,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"远端 COPY 失败: {proc.stderr}")
    return proc.stdout


def upsert_local(csv_text: str, dry_run: bool) -> int:
    """把 CSV 文本通过临时表 upsert 进本机 stock_info。返回影响行数。"""
    if dry_run:
        n = sum(1 for line in csv_text.strip().splitlines() if line and not line.startswith("stock_code"))
        log.info(f"[DRY-RUN] 远端 FALSE 行数: {n}，未写库")
        return n

    from db import get_conn
    import io
    with get_conn() as conn:
        cur = conn.cursor()
        # 建临时表（LIKE 含默认值）；stock_info 无 id 列，无需 DROP
        cur.execute("CREATE TEMP TABLE _si_tmp (LIKE stock_info INCLUDING DEFAULTS)")
        # COPY FROM STDIN（CSV 已含 HEADER）—— 注意是纯 SQL，不是 psql 的 \COPY
        cur.copy_expert(
            f"COPY _si_tmp({SYNC_COLS}) FROM STDIN WITH (FORMAT CSV, HEADER TRUE)",
            io.StringIO(csv_text),
        )
        # upsert：仅更新 stock_name，绝不碰 is_active
        cur.execute("""
            INSERT INTO stock_info (stock_code, stock_name, market, symbol, currency, is_active)
            SELECT stock_code, stock_name, market, symbol, currency, is_active
            FROM _si_tmp
            ON CONFLICT (stock_code) DO UPDATE SET
                stock_name = EXCLUDED.stock_name,
                updated_at = NOW()
        """)
        cur.execute("DROP TABLE _si_tmp")
        conn.commit()
        return cur.rowcount


def count_local_inactive() -> int:
    from db import get_conn
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM stock_info WHERE is_active=FALSE")
        return cur.fetchone()[0]


def main():
    parser = argparse.ArgumentParser(description="同步服务器 stock_info 中 is_active=FALSE 的行到本机")
    parser.add_argument("--dry-run", action="store_true", help="只统计远端 FALSE 行数，不写库")
    args = parser.parse_args()

    log.info("从服务器拉取 is_active=FALSE 的 stock_info 行...")
    try:
        csv_text = fetch_remote_csv()
    except Exception as e:
        log.error(f"拉取失败: {e}")
        raise SystemExit(1)

    before = count_local_inactive()
    log.info(f"本机同步前 is_active=FALSE 行数: {before}")

    affected = upsert_local(csv_text, args.dry_run)

    if args.dry_run:
        log.info(f"[DRY-RUN] 完成，未写库。远端待同步 FALSE 行数: {affected}")
        return

    after = count_local_inactive()
    log.info(f"本机同步后 is_active=FALSE 行数: {after}（变化 {after - before:+d}）")
    log.info("完成。注意：is_active 保持不变（FALSE 落库 / 原有 active 行不受影响）。")


if __name__ == "__main__":
    main()
