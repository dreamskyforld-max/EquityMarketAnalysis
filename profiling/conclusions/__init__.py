"""个股画像 L2/L3 结论层（doc/stock_profile_l2_l3.md v1.0）。

- rules.py     : RULES（唯一可执行规则真源）+ 结构化条件求值 + conclusion_dict 同步
- producer.py  : 结论生产者（输入归一化 / 派生 / 门禁 / 事件行写入 / tag_run_log）
"""
from . import rules  # noqa: F401
