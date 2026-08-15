#!/usr/bin/env python3
"""
批量新股接入脚本

功能：把一批股票统一接入系统（等价于对每只依次跑 setup_new_stock.py 的核心逻辑），
但只在全部处理完后重启一次服务，避免逐只重启 10 次。

每只股票会：
  1. 从富途 API 获取基本信息 → 写入 stock_info（upsert，幂等，is_active=TRUE）
  2. 回填 daily_quote 历史日线（默认 3 年，可用 BACKFILL_DAYS 覆盖）
  3. A 股：首次采集融资余额
全部完成后统一重启 scheduler + ticker-collector 一次。

注意：股票列表由 market_scheduler / ticker_collector 启动时从 stock_info
(is_active=TRUE) 动态加载，本脚本只需写入 stock_info 即可，无需再改 STOCKS
硬编码列表或 config.conf 的 [ticker] 订阅列表（已弃用该逻辑）。

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

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

# 默认列表：港股 TOP70 中 stock_info 尚未接入的股票（按 TOP70 排名顺序）
# 已存在 stock_info 的 HK 股（00700/00857/00941/06869/09660）不在此列；
# stock_info 中自选但不在 TOP70 的 6 只（00386/00728/01088/01919/03328/03968）亦不动。
DEFAULT_CODES = [
    "02513",  # 智谱
    "00100",  # MINIMAX-W
    "00981",  # 中芯国际
    "01347",  # 华虹宏力
    "09988",  # 阿里巴巴-W
    "01888",  # 建滔积层板
    "00992",  # 联想集团
    "09992",  # 泡泡玛特
    "01810",  # 小米集团-W
    "03308",  # 中际旭创
    "03986",  # 兆易创新
    "06166",  # 剑桥科技
    "01299",  # 友邦保险
    "02476",  # 胜宏科技
    "00148",  # 建滔集团
    "06809",  # 澜起科技
    "09903",  # 天数智芯
    "02899",  # 紫金矿业
    "02318",  # 中国平安
    "02269",  # 药明生物
    "01548",  # 金斯瑞生物科技
    "03690",  # 美团-W
    "02628",  # 中国人寿
    "06160",  # 百济神州
    "01093",  # 石药集团
    "09999",  # 网易
    "02359",  # 药明康德
    "00388",  # 香港交易所
    "01024",  # 快手-W
    "01378",  # 中国宏桥
    "09618",  # 京东集团-SW
    "03750",  # 宁德时代
    "03330",  # 灵宝黄金
    "01801",  # 信达生物
    "00005",  # 汇丰控股
    "09926",  # 康方生物
    "03896",  # 金山云
    "00939",  # 建设银行
    "02259",  # 紫金黄金国际
    "00322",  # 康师傅控股
    "02600",  # 中国铝业
    "02099",  # 中国黄金国际
    "03988",  # 中国银行
    "06951",  # 三环集团
    "09888",  # 百度集团-SW
    "00268",  # 金蝶国际
    "01211",  # 比亚迪股份
    "00189",  # 东岳集团
    "00669",  # 创科实业
    "03696",  # 英矽智能
    "01398",  # 工商银行
    "00175",  # 吉利汽车
    "03993",  # 洛阳钼业
    "00522",  # ASMPT
    "06181",  # 老铺黄金
    "01772",  # 赣锋锂业
    "09688",  # 再鼎医药
    "01208",  # 五矿资源
    "00027",  # 银河娱乐
    "09880",  # 优必选
    "06082",  # 壁仞科技
    "03939",  # 万国黄金集团
    "03759",  # 康龙化成
    "02228",  # 晶泰控股
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


# ── 服务重启（仅一次）─────────────────────────────────────
# 注：股票列表已由 market_scheduler / ticker_collector 启动时从 stock_info
# (is_active=TRUE) 动态加载，无需再改写 STOCKS 硬编码或 config.conf 的
# [ticker] 订阅列表。写入 stock_info 后重启进程即自动纳入。

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
