#!/usr/bin/env python3
"""
批量新股接入脚本

功能：把一批股票统一接入系统（等价于对每只依次跑 setup_new_stock.py 的核心逻辑），
但只在全部处理完后重启一次服务，避免逐只重启 10 次。

每只股票会：
  1. 从富途 API 获取基本信息 → 写入 stock_info（upsert，幂等）
  2. 回填 daily_quote 历史日线（默认 3 年，可用 BACKFILL_DAYS 覆盖）
  3. A 股：首次采集融资余额
  4. 追加到 market_scheduler.py 的 STOCKS 列表（已存在则跳过）
  5. 追加到 config.conf 的 [ticker] 订阅列表（已存在则跳过）
全部完成后统一重启 scheduler + ticker-collector 一次。

用法：
    # 处理内置的 SH.520900 ETF 十大重仓股
    .venv/bin/python3 setup_new_stocks.py

    # 指定代码（空格分隔，纯数字自动推断市场）
    .venv/bin/python3 setup_new_stocks.py HK.00700 00857 01088

    # 自定义回填天数
    BACKFILL_DAYS=365 .venv/bin/python3 setup_new_stocks.py 01088 00883
"""

import sys
import os
import platform
import subprocess
import logging

import config
import configparser as _cp_mod

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

# 默认列表：SH.520900（港股通红利ETF广发）十大重仓股（2026Q2，截止 2026-06-30）
DEFAULT_CODES = [
    "01088",  # 中国神华
    "00857",  # 中国石油股份
    "00883",  # 中国海洋石油
    "00941",  # 中国移动
    "00386",  # 中国石油化工股份
    "01919",  # 中远海控
    "06869",  # 长飞光纤光缆
    "00728",  # 中国电信
    "03328",  # 交通银行
    "03968",  # 招商银行
]

BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "1095"))


# ── 代码解析：兼容纯数字自动推断市场 ────────────────────────

def resolve_code(raw: str):
    raw = raw.strip().upper()
    if raw.startswith(("HK.", "SH.", "SZ.")):
        code = raw
        market = raw.split(".")[0]
    elif len(raw) == 5:
        code, market = f"HK.{raw}", "HK"
    elif raw.startswith(("5", "6", "9")):
        code, market = f"SH.{raw}", "SH"
    else:
        code, market = f"SZ.{raw}", "SZ"
    symbol = code.split(".")[1]
    currency = "HKD" if market == "HK" else "CNY"
    stock_type = "A" if market in ("SH", "SZ") else "HK"
    return code, market, symbol, currency, stock_type


# ── 步骤 1: 基本信息 + stock_info ───────────────────────────

def get_stock_info(code: str) -> str | None:
    try:
        from futu import OpenQuoteContext, RET_OK, Market
        mkt = {"HK": Market.HK, "SH": Market.SH, "SZ": Market.SZ}.get(code.split(".")[0], Market.HK)
        ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
        ret, data = ctx.get_stock_basicinfo(mkt, code_list=[code])
        ctx.close()
        if ret == RET_OK and not data.empty:
            return data.iloc[0].get("name", code)
    except Exception as e:
        log.warning(f"  [{code}] 富途基本信息获取失败: {e}")
    return None


def write_stock_info(code, name, market, symbol, currency):
    try:
        from db import get_conn
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO stock_info (stock_code, stock_name, market, symbol, currency, is_active)
                VALUES (%s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (stock_code) DO UPDATE SET
                    stock_name = EXCLUDED.stock_name,
                    updated_at = NOW()
            """, (code, name, market, symbol, currency))
            conn.commit()
        log.info(f"  [{code}] stock_info 已写入: {name}")
    except Exception as e:
        log.warning(f"  [{code}] stock_info 写入失败: {e}")


# ── 步骤 2: 回填 daily_quote ───────────────────────────────

def backfill_daily(code):
    result = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, "backfill_daily_quote.py"), code, str(BACKFILL_DAYS)],
        capture_output=True, text=True, timeout=300, cwd=SCRIPTS_DIR,
    )
    if result.returncode == 0:
        log.info(f"  [{code}] daily_quote 回填完成")
    else:
        log.warning(f"  [{code}] daily_quote 回填失败: {result.stderr.strip()[-200:]}")


# ── 步骤 3: A 股融资余额 ───────────────────────────────────

def collect_margin(code):
    result = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, "get_margin_balance.py"), code],
        capture_output=True, text=True, timeout=120, cwd=SCRIPTS_DIR,
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if "融资余额" in line:
                log.info(f"  [{code}] {line.strip()}")
    else:
        log.warning(f"  [{code}] 融资余额采集失败: {result.stderr.strip()[-200:]}")


# ── 步骤 4: market_scheduler.py STOCKS ──────────────────────

def add_to_scheduler(code, stock_type):
    scheduler_path = os.path.join(SCRIPTS_DIR, "market_scheduler.py")
    with open(scheduler_path, "r") as f:
        lines = f.readlines()
    in_stocks = False
    for i, line in enumerate(lines):
        if line.strip().startswith("STOCKS = ["):
            in_stocks = True
            continue
        if in_stocks and line.strip() == "]":
            if any(code in l for l in lines):
                log.info(f"  [{code}] market_scheduler.py 已存在，跳过")
            else:
                prev = lines[i - 1].rstrip()
                if not prev.endswith(","):
                    lines[i - 1] = prev + ",\n"
                lines.insert(i, f'    {{"code": "{code}", "market": "{stock_type}"}},\n')
                with open(scheduler_path, "w") as f:
                    f.writelines(lines)
                log.info(f"  [{code}] market_scheduler.py STOCKS 已添加")
            return


# ── 步骤 5: config.conf [ticker] ───────────────────────────

def add_to_ticker_config(code):
    _cp = _cp_mod.ConfigParser()
    if os.path.exists(config.CONFIG_PATH):
        _cp.read(config.CONFIG_PATH, encoding="utf-8")
    if not _cp.has_section("ticker"):
        _cp.add_section("ticker")
    raw = _cp.get("ticker", "stocks", fallback="")
    lst = [s.strip() for s in raw.split(",") if s.strip()]
    if code not in lst:
        lst.append(code)
        _cp.set("ticker", "stocks", ",".join(lst))
        with open(config.CONFIG_PATH, "w", encoding="utf-8") as f:
            _cp.write(f)
        log.info(f"  [{code}] config.conf [ticker] 已添加")
    else:
        log.info(f"  [{code}] config.conf [ticker] 已存在，跳过")


# ── 服务重启（仅一次）─────────────────────────────────────

IS_MAC = platform.system() == "Darwin"
SERVICES = {
    "scheduler": {"mac": "com.equity.a-scheduler.plist", "linux": "market-scheduler"},
    "ticker": {"mac": "com.equity.ticker-collector.plist", "linux": "ticker-collector"},
}


def restart_service(name: str):
    cfg = SERVICES[name]
    if IS_MAC:
        plist_path = os.path.join(os.path.expanduser("~/Library/LaunchAgents"), cfg["mac"])
        if not os.path.exists(plist_path):
            log.warning(f"plist 不存在: {plist_path}，跳过 {name}")
            return
        subprocess.run(["launchctl", "unload", plist_path], capture_output=True)
        subprocess.run(["launchctl", "load", plist_path], capture_output=True)
        log.info(f"已重启 (launchctl): {cfg['mac']}")
    else:
        svc = cfg["linux"]
        result = subprocess.run(["systemctl", "restart", svc], capture_output=True, text=True)
        if result.returncode == 0:
            log.info(f"已重启 (systemctl): {svc}")
        else:
            log.warning(f"systemctl restart {svc} 失败: {result.stderr.strip()}")


# ── 主流程 ─────────────────────────────────────────────────

def main():
    raw_codes = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_CODES
    if not raw_codes:
        print("用法: setup_new_stocks.py [HK.00700 00857 01088 ...]")
        sys.exit(1)

    results = []
    for raw in raw_codes:
        code, market, symbol, currency, stock_type = resolve_code(raw)
        log.info(f"=== 接入: {code} (市场={market}, 币种={currency}) ===")
        name = get_stock_info(code) or code
        write_stock_info(code, name, market, symbol, currency)
        backfill_daily(code)
        if stock_type == "A":
            collect_margin(code)
        add_to_scheduler(code, stock_type)
        add_to_ticker_config(code)
        results.append((code, name, market))

    # 统一重启一次
    log.info("全部股票处理完毕，统一重启服务...")
    restart_service("scheduler")
    restart_service("ticker")

    scheduler_svc = SERVICES["scheduler"]["mac"] if IS_MAC else SERVICES["scheduler"]["linux"]
    ticker_svc = SERVICES["ticker"]["mac"] if IS_MAC else SERVICES["ticker"]["linux"]

    print(f"\n{'='*60}")
    print(f"批量接入完成，共 {len(results)} 只:")
    for code, name, market in results:
        print(f"  ├─ {code} ({name}) [{market}]")
    print(f"  └─ 服务重启 ✅ ({scheduler_svc} + {ticker_svc})")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
