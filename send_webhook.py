#!/usr/bin/env python3
"""企业微信群机器人 Webhook 分段发送（按字符数分段，2000字符上限）"""
import sys, json, re
import urllib.request
from config import val

WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + val("wecom_webhook", "key")
MAX_CHARS = 3400   # 每条消息最多 3442 字符，留余量

def clean_content(text):
    """过滤掉 futu 日志等无关行"""
    lines = []
    for line in text.splitlines():
        # 跳过包含 open_context_base.py 的行
        if 'open_context_base.py' in line:
            continue
        # 跳过 futu 的其他调试日志
        if '[open_context_base.py' in line:
            continue
        lines.append(line)
    return "\n".join(lines)

def send_chunk(text):
    """发送单条消息"""
    payload = {"msgtype": "text", "text": {"content": text}}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("errcode") != 0:
                print(f"发送失败: {result.get('errmsg')}")
    except Exception as e:
        print(f"发送异常: {e}")

def send_long_message(text):
    """按字符数分段发送"""
    # 先清洗
    text = clean_content(text)
    # 按固定字符数切分
    for i in range(0, len(text), MAX_CHARS):
        chunk = text[i:i + MAX_CHARS]
        send_chunk(chunk)
        print(f"已发送第 {i // MAX_CHARS + 1} 段，长度 {len(chunk)} 字符")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: send_webhook.py <文本文件路径>")
        sys.exit(1)
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        content = f.read()
    send_long_message(content)