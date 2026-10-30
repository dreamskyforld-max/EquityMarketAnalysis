"""分时切分研究 · 代码包

⚠ 本包是**研究性探索**，不参与生产：生产窗口口径仍是
   `trend_segmenter.layer1_segments` → `force_dynamics.rounds_by_trend`。

模块：
  common    公共工具（线性拟合、噪声、效应量、方向/标记、极值锚定）
  rules     规则堆 v1 / v3（对照基线，已被训练模型超过）
  dp        带权代价 + DP 全局最优（当前最好）
  labels    人工标注集（5 票 × 19 日 = 237 窗）
  panels    分钟面板加载（复用生产 load_panel 口径）
  train     权重训练 + 留一股票交叉验证
  viz       单日切分可视化（需 matplotlib）
  labelpack 批量生成待标注包
"""
