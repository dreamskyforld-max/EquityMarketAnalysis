# -*- coding: utf-8 -*-
"""
qrobot_plugin.py
功能：
1. 使用企业微信 AI Bot SDK
2. 向企微机器人发送查询指令（如 /quote HK.00700）
3. 阻塞等待并返回机器人返回的文本数据
4. 供 WorkBuddy 通过 HTTP / Function Call 调用
"""

import os
import sys
import time
import threading
import configparser
from wecom_aibot_sdk_python import WeComAiBot

# =========================
# 1. 配置区（从根目录 config.conf 读取）
# =========================
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_cp = configparser.ConfigParser()
if os.path.exists(os.path.join(_ROOT, "config.conf")):
    _cp.read(os.path.join(_ROOT, "config.conf"), encoding="utf-8")
BOT_ID = _cp.get("wecom", "bot_a_id", fallback="")
BOT_SECRET = _cp.get("wecom", "bot_a_secret", fallback="")

# 查询指令（与你企微机器人配置保持一致）
QUERY_CMD = "/全量 HK.00700"

# 超时控制（秒）
TIMEOUT = 15


# =========================
# 2. 核心函数
# =========================
def fetch_qrobot_data(
    symbol: str = "HK.00700",
    timeout: int = TIMEOUT
) -> str:
    """
    供 WorkBuddy 调用的统一入口
    :param symbol: 股票代码
    :param timeout: 最长等待时间
    :return: 企微机器人返回的文本数据
    """

    result = {"text": None}
    stop_event = threading.Event()

    def on_message(message, reply):
        """企微消息回调"""
        msg_type = message.get("msgtype")
        if msg_type != "text":
            return

        content = message.get("content", "").strip()
        if not content:
            return

        print(f"[QRobot] 收到文本，长度={len(content)}")
        result["text"] = content

        # 告诉机器人：已收到
        reply.text("✅ 数据已接收，正在分析")

        # 通知主线程可以结束
        stop_event.set()

    bot = WeComAiBot({
        "bot_id": BOT_ID,
        "secret": BOT_SECRET,
        "on_message": on_message
    })

    try:
        print("[QRobot] 启动连接...")
        bot.start()

        cmd = QUERY_CMD.replace("HK.00700", symbol)
        print(f"[QRobot] 发送指令：{cmd}")
        bot.send_text(cmd)

        # 等待结果
        stop_event.wait(timeout)

    except Exception as e:
        print(f"[QRobot] 异常：{e}")
        result["text"] = f"ERROR: {e}"

    finally:
        try:
            bot.stop()
        except Exception:
            pass

    return result["text"] or "ERROR: 未获取到数据"


# =========================
# 3. HTTP 服务封装（推荐）
# =========================
try:
    from flask import Flask, request, jsonify

    app = Flask(__name__)

    @app.route("/qrobot", methods=["POST"])
    def http_fetch():
        """
        WorkBuddy 调用方式：
        POST /qrobot
        {
          "symbol": "HK.00700"
        }
        """
        data = request.get_json(silent=True) or {}
        symbol = data.get("symbol", "HK.00700")

        text = fetch_qrobot_data(symbol)
        return jsonify({
            "success": not text.startswith("ERROR"),
            "data": text
        })

except ImportError:
    pass


# =========================
# 4. 本地测试
# =========================
if __name__ == "__main__":
    print("🚀 本地测试模式")
    data = fetch_qrobot_data("HK.00700")

    print("\n====== 企微返回数据 ======\n")
    print(data)
    print("\n===========================\n")