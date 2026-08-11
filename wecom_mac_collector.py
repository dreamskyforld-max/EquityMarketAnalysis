#!/usr/bin/env python3
"""
机器人B - 全功能数据采集（Mac 端，LV2 权限）
指令：
  全量 HK.00700   → 执行盘后模块
  实时 HK.00700   → 执行盘中实时模块
  趋势 HK.00700   → 读取盘中趋势缓存
"""
import os, subprocess, re, asyncio, json
from datetime import datetime
from aibot import WSClient, WSClientOptions, generate_req_id
from config import val

BOT_ID = val("wecom", "bot_b_id", "BOT_B_ID")
SECRET = val("wecom", "bot_b_secret", "BOT_B_SECRET")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(SCRIPT_DIR, ".venv", "bin", "python3")
CACHE_DIR = os.path.join(SCRIPT_DIR, "trend_cache")

# (名称, 脚本, 需要传股票代码, 是否实时)
ALL_MODULES = [
    # ========== 盘后模块 ==========
    ("行情快照", "get_quote.py", True, False),
    ("资金流向", "get_realtime_order_size.py", True, False),
    ("基准指数", "get_benchmark.py", True, False),
    ("超额收益", "get_excess_return.py", True, False),
    ("历史K线", "get_kline.py", True, False),
    ("融资余额", "get_margin_balance.py", True, False),
    ("沽空数据", "get_short_selling.py", True, False),
    ("趋势数据", "get_trend.py", True, False),
    ("全日沽空数据", "get_realtime_short_selling_fullday.py", True, False),
    ("公司回购", "get_buyback.py", True, False),

    # ========== 实时模块 ==========
    ("实时主动性买卖盘", "get_realtime_trade_direction.py", True, True),
    ("实时分时成交", "get_realtime_volume_price.py", True, True),
    ("实时大小单资金", "get_realtime_order_size.py", True, True),
    ("实时盘口挂单", "get_realtime_order_book.py", True, True),
    ("实时超额收益", "get_realtime_excess_return.py", True, True),
    ("半日沽空数据", "get_realtime_short_selling_halfday.py", True, True),
    ("全日沽空数据", "get_realtime_short_selling_fullday.py", True, True),
]


def parse_stock_code(content):
    m = re.search(r'(HK\.\d{5})|(SH\.\d{6})|(SZ\.\d{6})', content)
    if m:
        return m.group(0)
    digits = re.search(r'\b(\d{5,6})\b', content)
    if digits:
        code = digits.group(1)
        if len(code) == 5:
            return f"HK.{code}"
        elif len(code) == 6 and code.startswith('6'):
            return f"SH.{code}"
        else:
            return f"SZ.{code}"
    return "HK.00700"


def run_module_and_get_output(script_name, stock_code=None):
    script_path = os.path.join(SCRIPT_DIR, script_name)
    cmd = [VENV_PYTHON, script_path]
    if stock_code:
        cmd.append(stock_code)
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, timeout=120,
            cwd=SCRIPT_DIR,
        )
        if result.returncode == 0:
            lines = result.stdout.splitlines()
            cleaned = [line for line in lines if 'open_context_base.py' not in line]
            return "\n".join(cleaned).strip()
        return f"失败 (退出码: {result.returncode})"
    except subprocess.TimeoutExpired:
        return "超时"
    except Exception as e:
        return f"异常: {e}"


async def handle_collect(ws_client, frame, stock_code):
    confirm = f"收到指令，开始全功能数据采集（{stock_code}）..."
    await ws_client.reply_stream(frame, generate_req_id('stream'), confirm, True)

    batch_modules = [
        (name, script, need_param)
        for name, script, need_param, is_rt in ALL_MODULES if not is_rt
    ]

    results = {}
    for name, script, need_param in batch_modules:
        code_arg = stock_code if need_param else None
        output = await asyncio.to_thread(run_module_and_get_output, script, code_arg)
        results[name] = output

    reply_lines = [f"数据采集完成（{stock_code}）："]
    for name, output in results.items():
        if output.startswith("失败") or output.startswith("超时") or output.startswith("异常"):
            reply_lines.append(f"\n[{name}] 采集失败：{output}")
        else:
            reply_lines.append(f"\n[{name}]")
            reply_lines.append(output)
    reply_lines.append(f"\n完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    full_reply = "\n".join(reply_lines)
    await ws_client.reply_stream(frame, generate_req_id('stream'), full_reply, True)


async def handle_realtime(ws_client, frame, stock_code):
    confirm = f"收到指令，实时资金数据采集中（{stock_code}）..."
    await ws_client.reply_stream(frame, generate_req_id('stream'), confirm, True)

    realtime_modules = [
        (name, script, need_param)
        for name, script, need_param, is_rt in ALL_MODULES if is_rt
    ]

    async def run_one(name, script, need_param):
        code_arg = stock_code if need_param else None
        output = await asyncio.to_thread(run_module_and_get_output, script, code_arg)
        return name, output

    tasks = [run_one(name, script, need_param) for name, script, need_param in realtime_modules]
    results_list = await asyncio.gather(*tasks)
    results = dict(results_list)

    reply_lines = [f"实时资金数据采集完成（{stock_code}）："]
    for name, output in results.items():
        if output.startswith("失败") or output.startswith("超时") or output.startswith("异常"):
            reply_lines.append(f"\n[{name}] 采集失败：{output}")
        else:
            reply_lines.append(f"\n[{name}]")
            reply_lines.append(output)
    reply_lines.append(f"\n完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    full_reply = "\n".join(reply_lines)
    await ws_client.reply_stream(frame, generate_req_id('stream'), full_reply, True)


def fetch_trend_from_db(stock_code, date_str):
    """从 trend_snapshot 表读取当日趋势序列，映射为 handle_trend 所需的 record 结构。

    返回 dict: {"am": [...], "pm": [...], "found": bool}
    """
    from db import get_conn
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT snapshot_time, price,
                           super_in_net, big_in_net, mid_in_net, small_in_net,
                           buy_sell_ratio, excess_return_pct, volume, turnover,
                           buy_levels_str, sell_levels_str
                    FROM trend_snapshot
                    WHERE stock_code = %s
                      AND snapshot_time::date = %s
                    ORDER BY snapshot_time ASC
                    """,
                    (stock_code, date_str),
                )
                rows = cur.fetchall()
    except Exception as e:
        print(f"[趋势] DB查询失败: {e}")
        return {"am": [], "pm": [], "found": False, "error": True}

    if not rows:
        return {"am": [], "pm": [], "found": False}

    am, pm = [], []
    for (snap_time, price, super_in, big_in, mid_in, small_in,
         ratio, excess, volume, turnover, buy_str, sell_str) in rows:
        rec = {
            "time": snap_time.strftime('%H:%M'),
            "price": float(price) if price is not None else None,
            "super_in": float(super_in) if super_in is not None else None,
            "big_in": float(big_in) if big_in is not None else None,
            "mid_in": float(mid_in) if mid_in is not None else None,
            "small_in": float(small_in) if small_in is not None else None,
            "ratio": float(ratio) if ratio is not None else None,
            "excess": float(excess) if excess is not None else None,
            "volume": float(volume) if volume is not None else None,
            "turnover": float(turnover) if turnover is not None else None,
            "buy_str": buy_str or "N/A",
            "sell_str": sell_str or "N/A",
        }
        if snap_time.hour < 13:
            am.append(rec)
        else:
            pm.append(rec)
    return {"am": am, "pm": pm, "found": True}


async def handle_trend(ws_client, frame, stock_code):
    date_str = datetime.now().strftime('%Y%m%d')

    result = fetch_trend_from_db(stock_code, date_str)
    if result.get("error"):
        await ws_client.reply_stream(frame, generate_req_id('stream'),
                                     f"趋势数据 ({stock_code}): 读取失败", True)
        return
    if not result["found"]:
        await ws_client.reply_stream(frame, generate_req_id('stream'),
                                     f"趋势数据 ({stock_code}): 暂无今日记录", True)
        return

    am_records = result["am"]
    pm_records = result["pm"]

    def format_records(records, title):
        lines = [f"趋势数据 ({stock_code}) {title}:"]
        for rec in records:
            time_str = f"[{rec['time']}]"
            price_str = f"股价 {rec['price']:.1f}" if rec.get('price') is not None else "股价 N/A"
            super_str = f"特大单 {rec['super_in']:.2f}亿" if rec.get('super_in') is not None else "特大单 N/A"
            big_str = f"大单 {rec['big_in']:.2f}亿" if rec.get('big_in') is not None else "大单 N/A"
            mid_str = f"中单 {rec['mid_in']:.2f}亿" if rec.get('mid_in') is not None else "中单 N/A"
            small_str = f"小单 {rec['small_in']:.2f}亿" if rec.get('small_in') is not None else "小单 N/A"
            ratio_str = f"买比 {rec['ratio']:.2f}" if rec.get('ratio') is not None else "买比 N/A"
            excess_str = f"超额 {rec['excess']:.2f}%" if rec.get('excess') is not None else "超额 N/A"
            vol_str = f"量 {rec['volume']/10000:.0f}万" if rec.get('volume') is not None else "量 N/A"
            to_str = f"成交 {rec['turnover']:.2f}亿" if rec.get('turnover') is not None else "成交 N/A"
            book_str = f"盘口 买5-1: {rec.get('buy_str', 'N/A')} | 卖1-5: {rec.get('sell_str', 'N/A')}"
            line = f"{time_str} {price_str} | {super_str} | {big_str} | {mid_str} | {small_str} | {ratio_str} | {excess_str} | {vol_str} | {to_str}\n{book_str}"
            lines.append(line)
        return "\n".join(lines)

    # 发送上午消息
    if am_records:
        reply_am = format_records(am_records, "上午时段")
        await ws_client.reply_stream(frame, generate_req_id('stream_am'), reply_am, True)

    # 发送下午消息（如果有）
    if pm_records:
        reply_pm = format_records(pm_records, "下午时段")
        await ws_client.reply_stream(frame, generate_req_id('stream_pm'), reply_pm, True)


async def main():
    ws_client = WSClient(WSClientOptions(bot_id=BOT_ID, secret=SECRET))

    @ws_client.on('authenticated')
    def on_authenticated():
        print(f"[{datetime.now()}] 机器人B 已认证")

    @ws_client.on('message.text')
    async def on_text(frame):
        body = frame.get('body', {}).get('text', {})
        content = body.get('content', '').strip()
        if not content:
            return
        print(f"[{datetime.now()}] 收到消息: {content[:100]}...")

        stock_code = parse_stock_code(content)

        if re.search(r"趋势", content):
            await handle_trend(ws_client, frame, stock_code)
        elif re.search(r"实时", content):
            await handle_realtime(ws_client, frame, stock_code)
        elif re.search(r"全量", content, re.IGNORECASE):
            await handle_collect(ws_client, frame, stock_code)
        # 可在此继续添加其他指令

    await ws_client.connect()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())