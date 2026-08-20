#!/usr/bin/env python3
"""
从 hk_daily_quote 同步缺失股票到 stock_info（is_active=FALSE）

背景：
  - stock_info 不仅是对外展示的股票基本信息，更重要的是被 market_scheduler /
    ticker_collector 在启动时读取（is_active=TRUE 才纳入采集/调度）。
  - 富途接口权限有限，当前只能对重点筛选的股票做实时逐笔采集，所以新补入的
    股票一律 is_active=FALSE，仅作为参考数据落地（名称映射、历史行情关联），
    不会触发任何采集任务。
  - 若将来某只股票要转为正式采集对象，再单独跑 setup_new_stock.py（写入
    is_active=TRUE）即可，不要在此脚本里把 is_active 改 TRUE。

流程：
  1. 取 hk_daily_quote 中所有去重 stock_code
  2. 与 stock_info 对比，找出映射不上的代码
  3. 按 market 分组，批量调用富途 get_stock_basicinfo 获取名称
     （code_list 支持多代码，每批 BATCH 只，单次请求同 market）
  4. 批量写入 stock_info（upsert，is_active=FALSE），已存在则只更新名称
  5. 打印整理结果

用法：
    .venv/bin/python3 sync_hk_quote_to_stock_info.py            # 全量补全
    .venv/bin/python3 sync_hk_quote_to_stock_info.py --dry-run  # 只列出缺失、不写库
    .venv/bin/python3 sync_hk_quote_to_stock_info.py --batch 500  # 自定义批大小
"""

import argparse
import logging

BATCH = 200  # 每批请求富途的股票数（同 market 内）

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)


# ── 步骤 1: 取 hk_daily_quote 全部代码 + 已在 stock_info 的代码 ──

def fetch_codes_from_hk_quote() -> list[str]:
    from db import get_conn
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT stock_code FROM hk_daily_quote ORDER BY stock_code")
        return [r[0] for r in cur.fetchall()]


def fetch_existing_codes() -> set[str]:
    from db import get_conn
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT stock_code FROM stock_info")
        return {r[0] for r in cur.fetchall()}


# ── 步骤 2: 富途批量获取基本信息 ──

def get_stock_basicinfo_batch(market: str, code_list: list[str]) -> dict[str, str]:
    """批量从富途 API 获取股票名称，返回 {code: name}。

    code_list 必须属于同一 market（富途 get_stock_basicinfo 的 market 为单值）。
    注意：名称语言取决于运行环境的 OpenD 设置——本机通常返回英文名，
    服务器（OpenD 设为中文）返回中文名。本脚本直接落库，不做语言转换。
    """
    result: dict[str, str] = {}
    if not code_list:
        return result
    try:
        from futu import OpenQuoteContext, RET_OK, Market
        mkt = {"HK": Market.HK, "SH": Market.SH, "SZ": Market.SZ}.get(market, Market.HK)
        ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
        try:
            ret, data = ctx.get_stock_basicinfo(mkt, code_list=code_list)
        finally:
            ctx.close()
        if ret == RET_OK and not data.empty:
            for _, row in data.iterrows():
                code = str(row.get("code", ""))
                if code:
                    result[code] = str(row.get("name", code))
        else:
            log.warning(f"  [{market}] 富途批量返回空 (ret={ret})")
    except Exception as e:
        log.warning(f"  [{market}] 富途批量获取失败: {e}")
    return result


# ── 步骤 3: 批量写入 stock_info（is_active=FALSE）──

def upsert_inactive_batch(rows: list[tuple], dry_run: bool) -> None:
    """批量写入/更新 stock_info，is_active 强制为 FALSE（仅补充参考数据）。

    rows: [(stock_code, stock_name, market, symbol, currency), ...]
    关键点：VALUES 用 FALSE；ON CONFLICT 仅更新名称，绝不触碰 is_active
    （避免把已 active 的股票误置为 FALSE，也不把 FALSE 误置为 TRUE）。
    """
    if dry_run:
        for code, name, *_ in rows:
            log.info(f"  [DRY-RUN] 将写入 {code} → {name} (is_active=FALSE)")
        return
    if not rows:
        return
    from db import get_conn
    with get_conn() as conn:
        cur = conn.cursor()
        cur.executemany("""
            INSERT INTO stock_info
                (stock_code, stock_name, market, symbol, currency, is_active)
            VALUES (%s, %s, %s, %s, %s, FALSE)
            ON CONFLICT (stock_code) DO UPDATE SET
                stock_name = EXCLUDED.stock_name,
                updated_at = NOW()
        """, rows)
        conn.commit()
    log.info(f"  批量写入 {len(rows)} 条 (is_active=FALSE)")


# ── 主流程 ──

def main():
    parser = argparse.ArgumentParser(description="补全 hk_daily_quote 中缺失于 stock_info 的港股（is_active=FALSE）")
    parser.add_argument("--dry-run", action="store_true", help="只列出缺失代码，不写库")
    parser.add_argument("--limit", type=int, default=0, help="仅处理前 N 只（调试用）")
    parser.add_argument("--batch", type=int, default=BATCH, help=f"每批请求富途的股票数（默认 {BATCH}）")
    args = parser.parse_args()

    log.info("读取 hk_daily_quote 全部股票代码...")
    hk_codes = fetch_codes_from_hk_quote()
    log.info(f"  hk_daily_quote 去重代码数: {len(hk_codes)}")

    existing = fetch_existing_codes()
    missing = [c for c in hk_codes if c not in existing]
    log.info(f"  stock_info 已有代码数: {len(existing)}")
    log.info(f"  ⚠ 映射不上的代码数: {len(missing)}")

    if not missing:
        log.info("没有需要补全的股票，结束。")
        return

    if args.limit > 0:
        missing = missing[: args.limit]

    print(f"\n{'='*70}")
    print(f"映射不上的股票代码（共 {len(missing)} 只）：")
    for c in missing:
        print(f"  {c}")
    print(f"{'='*70}\n")

    # 按 market 分组（富途 code_list 必须同 market）
    by_market: dict[str, list[str]] = {}
    for code in missing:
        by_market.setdefault(code.split(".")[0], []).append(code)

    added: list[tuple[str, str]] = []
    total = 0
    for market, codes in by_market.items():
        log.info(f"处理 market={market}，代码数={len(codes)}")
        # 分批请求富途
        for i in range(0, len(codes), args.batch):
            batch = codes[i: i + args.batch]
            names = get_stock_basicinfo_batch(market, batch)
            rows = []
            for code in batch:
                symbol = code.split(".")[1] if "." in code else code
                currency = "HKD" if market == "HK" else "CNY"
                name = names.get(code)
                if name is None:
                    # 富途拿不到就退化为用代码本身当名称，仍按 inactive 落库
                    name = code
                    log.warning(f"  [{code}] 富途无数据，退化为代码名落库")
                rows.append((code, name, market, symbol, currency))
                added.append((code, name))
            upsert_inactive_batch(rows, args.dry_run)
            total += len(rows)
            log.info(f"  {market} 进度 {min(i + args.batch, len(codes))}/{len(codes)}")

    print(f"\n{'='*70}")
    print(f"处理完成（{'DRY-RUN，未写库' if args.dry_run else '已写库'}）：")
    print(f"  补全股票数: {len(added)}")
    for code, name in added:
        print(f"    ├─ {code}  {name}  [is_active=FALSE]")
    print(f"{'='*70}")
    log.info(
        "提示：这些股票 is_active=FALSE，不会被采集/调度。若需转为正式采集对象，"
        "请单独跑 setup_new_stock.py。"
    )


if __name__ == "__main__":
    main()
