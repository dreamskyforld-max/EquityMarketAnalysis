#!/usr/bin/env python3
"""股票画像系统（Stock Profiling / Multi-label Tagging）

在现有 market_db 之上构建一层「标签体系」，为每只股票打上多维度、
可组合、可回溯的标签，支撑多维筛选与分组回测。

设计要点：
- **不新建库、不新建服务**：标签落在现有 market_db 的独立 schema `profile` 下，
  与采集表和其他项目（stock-realtime 等共用此库）命名空间隔离。
- **代码即元数据**：标签的口径、版本、依赖数据源用 `@tag` 装饰器写在计算函数旁，
  导入时自动同步到 `profile.tag_registry`，避免「字典表与代码两张皮」。
- **版本化 EAV 存储**：`profile.tag_value` 用 (stock_code, tag_code, value_key, eff_from)
  存储，标签变更不覆盖旧值而是关闭旧版本，天然支持 `as_of_date` 时间旅行。
- **统一计算接口**：每个标签是一个纯函数 `fn(as_of: date) -> DataFrame`，
  返回列固定为 [stock_code, value_key, value_num, confidence]，由引擎负责 diff 与落库。

目录：
    profiling/
      schema.py               建表 / 分区管理
      registry.py             @tag 装饰器与标签元数据注册表
      engine.py               计算引擎（diff + 版本化写入）
      tags/                   各标签域实现（① identity.py 等）
      backfill/               标签依赖数据的补采脚本
      run.py                  CLI 入口

用法：
    python3 -m profiling.run init                 # 建 schema + 同步标签字典
    python3 -m profiling.run list                 # 列出已注册标签
    python3 -m profiling.run compute --domain ①   # 计算整个域
"""
__version__ = "0.1.0"
