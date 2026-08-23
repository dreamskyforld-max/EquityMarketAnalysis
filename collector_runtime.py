#!/usr/bin/env python3
"""
常驻采集运行时（方案 B 核心）

让 market_scheduler 在进程内直接调用各采集脚本的 run()，彻底消灭
原 subprocess 每分钟冷启动读盘的 IOPS 风暴。

机制：
- run_module(name, codes)：惰性 importlib 导入采集模块（每进程仅一次），
  调用其 run(codes, ctx=共享上下文)。
- get_shared_ctx()：进程级单例 OpenQuoteContext，由 _LockedCtx 包装，
  自动串行化所有 futu 调用（SDK 非线程安全）并在断线时重建后重试一次。
"""
import os
import sys
import importlib
import threading
import time
import logging

log = logging.getLogger("collector")

_FUTU_HOST = "127.0.0.1"
_FUTU_PORT = 11111

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

_ctx = None
_ctx_lock = threading.Lock()       # 保护 ctx 创建/重连
_call_lock = threading.Lock()      # 串行化所有 futu 调用

_MOD_CACHE = {}
_IMPORT_LOCK = threading.Lock()    # 串行化模块首次 import


class _LockedCtx:
    """包装 OpenQuoteContext：每次方法调用自动加锁；断线时重建并重试一次。

    重连失败时抛 RuntimeError（普通 Exception），而非让 futu SDK 的
    ECONNREFUSED 等异常向上传播可能导致进程级崩溃。
    """
    def __getattr__(self, name):
        raw = _raw_ctx()
        attr = getattr(raw, name)
        if not callable(attr):
            return attr

        def _wrapper(*args, **kwargs):
            with _call_lock:
                _t0 = _mono()
                _ok = True
                _err = None
                try:
                    try:
                        return attr(*args, **kwargs)
                    except Exception:
                        pass  # 第一次调用失败，尝试重连
                    # 重连并重试一次
                    _reconnect()
                    return getattr(_raw_ctx(), name)(*args, **kwargs)
                except Exception as e:
                    _ok = False
                    _err = f"{type(e).__name__}: {e}"
                    raise RuntimeError(
                        f"futu 调用 {name} 失败（重连后仍不可用）: {_err}"
                    ) from e
                finally:
                    try:
                        from monitor_collector import record_api_call
                        _lat = _mono() - _t0
                        record_api_call(name, _ok, round(_lat, 3), _err)
                    except Exception:
                        pass  # 监控埋点失败不影响采集主流程
        return _wrapper


def _mono():
    return time.monotonic()


def _raw_ctx():
    global _ctx
    if _ctx is None:
        with _ctx_lock:
            if _ctx is None:
                from futu import OpenQuoteContext
                _ctx = OpenQuoteContext(host=_FUTU_HOST, port=_FUTU_PORT)
    return _ctx


def _reconnect():
    global _ctx
    with _ctx_lock:
        try:
            _ctx and _ctx.close()
        except Exception:
            pass
        from futu import OpenQuoteContext
        _ctx = OpenQuoteContext(host=_FUTU_HOST, port=_FUTU_PORT)


def get_shared_ctx():
    """返回带锁包装的共享行情上下文（无状态包装，每次取最新底层 ctx）。"""
    return _LockedCtx()


def run_module(script_name, codes=None, **kwargs):
    """惰性导入采集模块并调用其 run(codes)。import 仅发生一次/进程。"""
    mod = _MOD_CACHE.get(script_name)
    if mod is None:
        with _IMPORT_LOCK:
            mod = _MOD_CACHE.get(script_name)
            if mod is None:
                modname = script_name[:-3] if script_name.endswith(".py") else script_name
                mod = importlib.import_module(modname)
                _MOD_CACHE[script_name] = mod
    fn = getattr(mod, "run", None)
    if fn is None:
        raise RuntimeError(f"{script_name} 未实现 run()，无法常驻调用")
    return fn(codes, ctx=get_shared_ctx(), **kwargs)
