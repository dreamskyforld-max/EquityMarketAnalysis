#!/usr/bin/env python3
"""
统一配置加载器

读取项目根目录的 config.conf（INI 格式）。
取值优先级：环境变量 > config.conf > 兜底默认值。
config.conf 不传入 GitHub（见 .gitignore），仅本地使用。
"""
import os
import configparser

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.conf")
CONFIG_PATH = _CONFIG_PATH  # 公开常量，供需要写回配置（如 setup_new_stock）的脚本使用
_cp = configparser.ConfigParser()
if os.path.exists(_CONFIG_PATH):
    _cp.read(_CONFIG_PATH, encoding="utf-8")

# 转成嵌套 dict，方便 config["section"]["key"] 风格访问
_CONFIG = {s: dict(_cp.items(s)) for s in _cp.sections()}


def get_config():
    """返回嵌套 dict: {"database": {"host": ..., ...}, ...}"""
    return _CONFIG


def val(section, key, env_name=None, fallback=""):
    """
    取配置值：环境变量 env_name 优先，否则 config.conf 的 [section] key。
    config.conf 缺失或该 key 为空时回退到 fallback。
    """
    if env_name:
        v = os.environ.get(env_name)
        if v:
            return v
    return _CONFIG.get(section, {}).get(key, fallback)
