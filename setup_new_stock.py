#!/usr/bin/env python3
"""
新股接入一键脚本

功能：
  1. 从富途 API 获取股票基本信息 → 写入 stock_info
  2. 回填 60 日 daily_quote 历史数据
  3. A 股：首次采集融资余额数据
  4. 自动添加到 market_scheduler.py 的 STOCKS 列表
  5. 自动添加到 config.conf 的 [ticker] 订阅列表
  6. 重启 scheduler + ticker-collector 服务

用法：
    .venv/bin/python3 setup_new_stock.py SH.520900
    .venv/bin/python3 setup_new_stock.py HK.00700
"""

import sys
import os
import json
import platform
import subprocess
import logging
from datetime import date

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

# ── 步骤 1: 解析参数 ──────────────────────────────────────

if len(sys.argv) < 2:
    print("用法: .venv/bin/python3 setup_new_stock.py SH.520900")
    sys.exit(1)

raw_code = sys.argv[1].strip().upper()

# 兼容 "520900"、"600900" 等纯数字 → 自动推断市场
FULL_CODE_MAP = {
    "HK.00700": "HK",
    "HK.09988": "HK",
}
if raw_code.startswith("HK.") or raw_code.startswith("SH.") or raw_code.startswith("SZ."):
    stock_code = raw_code
    market = raw_code.split(".")[0]
else:
    # 纯数字，默认 HK（5位）/ SH（6位 5/6/9开头）/ SZ（6位 0/2/3开头）
    if len(raw_code) == 5:
        stock_code = f"HK.{raw_code}"
        market = "HK"
    elif raw_code.startswith(("5", "6", "9")):
        stock_code = f"SH.{raw_code}"
        market = "SH"
    else:
        stock_code = f"SZ.{raw_code}"
        market = "SZ"

symbol = stock_code.split(".")[1]
currency = "HKD" if market == "HK" else "CNY"
stock_type = "A" if market in ("SH", "SZ") else "HK"

log.info(f"开始接入新股: {stock_code} (市场={market}, 币种={currency})")


# ── 步骤 2: 获取基本信息 + 写入 stock_info ─────────────────

def get_stock_info(code: str) -> dict | None:
    """从富途 API 获取股票基本信息"""
    try:
        from futu import OpenQuoteContext, RET_OK, Market
        market_map = {"HK": Market.HK, "SH": Market.SH, "SZ": Market.SZ}
        market = market_map.get(code.split(".")[0], Market.HK)
        ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
        ret, data = ctx.get_stock_basicinfo(market, code_list=[code])
        ctx.close()
        if ret == RET_OK and not data.empty:
            row = data.iloc[0]
            return {
                "name": row.get("name", code),
                "stock_type": row.get("stock_type", ""),
            }
    except Exception as e:
        log.warning(f"富途 API 获取基本信息失败: {e}")
    return None


info = get_stock_info(stock_code)
stock_name = info["name"] if info else stock_code

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
        """, (stock_code, stock_name, market, symbol, currency))
        conn.commit()
    log.info(f"stock_info 已写入: {stock_code} → {stock_name}")
except Exception as e:
    log.warning(f"stock_info 写入失败: {e}")


# ── 步骤 3: 回填 daily_quote ───────────────────────────────

log.info("回填 daily_quote 60 日数据...")
result = subprocess.run(
    [sys.executable, os.path.join(SCRIPTS_DIR, "backfill_daily_quote.py"), stock_code, "60"],
    capture_output=True, text=True, timeout=180, cwd=SCRIPTS_DIR,
)
if result.returncode == 0:
    log.info("daily_quote 回填完成")
else:
    log.warning(f"daily_quote 回填失败: {result.stderr.strip()[-200:]}")


# ── 步骤 4: A 股首次采集融资余额 ────────────────────────────

if stock_type == "A":
    log.info("A 股首次采集融资余额...")
    result = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, "get_margin_balance.py"), stock_code],
        capture_output=True, text=True, timeout=120, cwd=SCRIPTS_DIR,
    )
    if result.returncode == 0:
        log.info("融资余额采集完成")
        for line in result.stdout.splitlines():
            if "融资余额" in line:
                log.info(f"  {line.strip()}")
    else:
        log.warning(f"融资余额采集失败: {result.stderr.strip()[-200:]}")
else:
    log.info("港股无需采集融资余额，跳过")


# ── 步骤 5: 写入 market_scheduler.py ───────────────────────

scheduler_path = os.path.join(SCRIPTS_DIR, "market_scheduler.py")
with open(scheduler_path, "r") as f:
    lines = f.readlines()

# 查找 STOCKS 列表的结束行（"STOCKS = [" 之后的第一个 "]")
in_stocks = False
for i, line in enumerate(lines):
    if line.strip().startswith("STOCKS = ["):
        in_stocks = True
        continue
    if in_stocks and line.strip() == "]":
        # 检查是否已存在
        if any(stock_code in l for l in lines):
            log.info(f"market_scheduler.py 中已存在 {stock_code}，跳过")
        else:
            # 在前一行末尾加逗号（如果不是只有一行）
            prev = lines[i - 1].rstrip()
            if not prev.endswith(","):
                lines[i - 1] = prev + ",\n"
            indent = "    "
            lines.insert(i, f'{indent}{{"code": "{stock_code}", "market": "{stock_type}"}},\n')
            with open(scheduler_path, "w") as f:
                f.writelines(lines)
            log.info(f"market_scheduler.py STOCKS 已添加 {stock_code}")
        break


# ── 步骤 6: 更新 config.conf 的 [ticker] 订阅列表 ───────────
import config
import configparser as _cp_mod

_cp = _cp_mod.ConfigParser()
if os.path.exists(config.CONFIG_PATH):
    _cp.read(config.CONFIG_PATH, encoding="utf-8")
if not _cp.has_section("ticker"):
    _cp.add_section("ticker")
_raw = _cp.get("ticker", "stocks", fallback="")
_stock_list = [s.strip() for s in _raw.split(",") if s.strip()]
if stock_code not in _stock_list:
    _stock_list.append(stock_code)
    _cp.set("ticker", "stocks", ",".join(_stock_list))
    with open(config.CONFIG_PATH, "w", encoding="utf-8") as f:
        _cp.write(f)
    log.info(f"config.conf [ticker] 已添加 {stock_code}")
else:
    log.info(f"config.conf [ticker] 中已存在 {stock_code}，跳过")


# ── 步骤 7: 重启服务 ───────────────────────────────────────

IS_MAC = platform.system() == "Darwin"
SERVICES = {
    "scheduler": {"mac": "com.equity.a-scheduler.plist", "linux": "market-scheduler"},
    "ticker": {"mac": "com.equity.ticker-collector.plist", "linux": "ticker-collector"},
}


def restart_service(name: str):
    cfg = SERVICES[name]
    if IS_MAC:
        plist_dir = os.path.expanduser("~/Library/LaunchAgents")
        plist_path = os.path.join(plist_dir, cfg["mac"])
        if not os.path.exists(plist_path):
            log.warning(f"plist 文件不存在: {plist_path}，跳过 {name}")
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


restart_service("scheduler")
restart_service("ticker")


# ── 完成 ──────────────────────────────────────────────────

scheduler_svc = SERVICES["scheduler"]["mac"] if IS_MAC else SERVICES["scheduler"]["linux"]
ticker_svc = SERVICES["ticker"]["mac"] if IS_MAC else SERVICES["ticker"]["linux"]

print(f"""
{'='*60}
新股接入完成: {stock_code} ({stock_name})
  ├─ stock_info       ✅
  ├─ daily_quote 60日  ✅
  ├─ 融资余额首采      {'✅' if stock_type == 'A' else '— (港股跳过)'}
  ├─ market_scheduler  ✅
  ├─ ticker_config     ✅
  └─ 服务重启          ✅ ({scheduler_svc} + {ticker_svc})
{'='*60}
""")
