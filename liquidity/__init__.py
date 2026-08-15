"""
港股流动性分析 · 三层架构
============================

「从市场总体 → 板块/指数 → 个股」的流动性全景分析。

三层：
  liquidity.market  —— 第 1 层：市场总体（量价水位 + 资金方向）
  liquidity.sector  —— 第 2 层：板块/指数（stock_sector 自适应分组）
  liquidity.stock   —— 第 3 层：个股（量价流动性 + 资金流 + 微观结构）
  liquidity.report  —— 编排层，汇总三层输出

数据源（均为既有表，不落中间表，现算）：
  daily_market_turnover —— 全港股总成交额（分钟/日快照）
  daily_ggt_hold        —— 南向资金（港股通持股明细）
  daily_quote           —— 个股日频行情（量价流动性）
  daily_benchmark       —— 指数基准（HSI 等）
  tick_data             —— 逐笔成交（微观结构）
  stock_sector          —— 股票-指数成分归属（板块划分）
"""
from . import market, sector, stock, report

__all__ = ["market", "sector", "stock", "report"]
