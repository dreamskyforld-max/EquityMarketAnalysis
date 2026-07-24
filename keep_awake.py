#!/usr/bin/env python3
"""
交易时段防休眠代理 — 仅在插电 + 交易时段阻止 Mac 空闲休眠。

逻辑：
  - 每 30 秒检查一次
  - 插电 + 交易时段 → 启动 caffeinate -i（阻止空闲休眠，屏幕正常熄）
  - 拔电 or 非交易时段 → 终止 caffeinate，允许正常休眠

caffeinate -i 参数说明：
  -i  : PreventUserIdleSystemSleep — 仅阻止空闲休眠
        屏幕可正常熄屏，不影响用户体验
  （-d 阻止屏幕休眠 / -s 阻止系统休眠，均不使用）

用法：
    python3 keep_awake.py                  # 前台运行
    launchctl load ~/Library/LaunchAgents/com.equity.keep-awake.plist  # 后台运行
"""

import os
import signal
import subprocess
import time
from datetime import datetime


def is_on_ac_power() -> bool:
    """检查是否插着电源（适配器供电）。"""
    try:
        result = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True, text=True, timeout=5,
        )
        # 输出示例："Now drawing from 'AC Power'" 或 "Now drawing from 'Battery Power'"
        return "AC Power" in result.stdout
    except Exception:
        # 无法判断时保守处理：当作电池供电，允许休眠
        return False


def is_trading_time() -> bool:
    """是否处于交易时段（兼顾港/A 股）。"""
    now = datetime.now()
    if now.weekday() >= 5:  # 周六日
        return False

    t = now.hour * 60 + now.minute
    # 港股 9:30-12:00, 13:00-16:00
    # A 股 9:30-11:30, 13:00-15:00
    # 取并集：9:30-12:00, 13:00-16:00
    return (570 <= t < 720) or (780 <= t < 960)


def main():
    print(f"[keep_awake] 启动 (pid={os.getpid()})")
    print(f"[keep_awake] 规则: 插电 + 交易日 9:30-16:00 → caffeinate -i")
    print(f"[keep_awake] 规则: 拔电 or 非交易时段 → 释放断言，允许休眠")

    proc = None

    def cleanup():
        nonlocal proc
        if proc and proc.poll() is None:
            print("[keep_awake] 收到退出信号，释放 caffeinate")
            proc.terminate()
            proc.wait(timeout=3)

    signal.signal(signal.SIGTERM, lambda *_: cleanup() or os._exit(0))
    signal.signal(signal.SIGINT, lambda *_: cleanup() or os._exit(0))

    while True:
        ac = is_on_ac_power()
        trading = is_trading_time()
        should_keep_awake = ac and trading

        if should_keep_awake:
            if proc is None or proc.poll() is not None:
                print(f"[keep_awake] 插电+交易时段，启动 caffeinate -i"
                      f" ({datetime.now().strftime('%H:%M')})")
                proc = subprocess.Popen(["caffeinate", "-i"])
        else:
            if proc and proc.poll() is None:
                reason = "拔电" if not ac else "非交易时段"
                print(f"[keep_awake] {reason}，释放 caffeinate"
                      f" ({datetime.now().strftime('%H:%M')})")
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                proc = None

        time.sleep(30)


if __name__ == "__main__":
    main()
