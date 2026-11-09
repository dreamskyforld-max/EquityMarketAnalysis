#!/usr/bin/env python3
"""`BIZ.MARKET_PATTERN` 字典标定：冻结 12 组 (market, dimension) ECDF + 写字典。

落地口径（2026-11-07 定案，见 `doc/market_state_data_architecture.md` §6.2 v1.32）：

  · 六维 `score` 虽都是 0-100 分位读数，但聚合结构不同导致分布基数不可比
    （绝对 70/30 下 CN STRESS 高仅 3.05%、CN VALUATION 57.67%、HK FLOW 56.65%）。
    故按 `(market, dimension)` 各自历史 score 冻结 101 个分位点，把 `score`
    转成**自身历史分位** u_i 后再分档，符合 §2.4 规则 7「先转自身分位」的
    合法例外（此处做条件合取，不做平均）。
  · 标定窗口截至 `PATTERN_CALIB_END`（2025-12-31），2026 起为样本外；
    冻结切点存在**参数层前视**，PIT 敏感性由 `tools/validate_pattern.py` 报告。
  · ECDF / 规则表 / 优先级 / 六维 params_hash 快照全部进 `business_dict.params`
    并纳入 `params_hash`，改任一处即触发历史全量重算。
  · 本脚本是字典的常规写入口；`build_business_value.py --seed` 走同一
    `pattern_build_dict_row()`，两者结果逐值一致。

用法：
    .venv/bin/python3 tools/calibrate_pattern.py --probe    # 只读报告，不写库
    .venv/bin/python3 tools/calibrate_pattern.py --write    # 写 business_dict 一行
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from db import bulk_upsert, get_conn  # noqa: E402
import build_business_value as bb  # noqa: E402


def _report(calib: dict, row: dict) -> None:
    print(f"标定窗口截至 {calib['calib_end']}｜样本 {calib['n_obs']} 行｜"
          f"params_hash = {row['params_hash']}")
    print(f"{'市场':<4} {'维度':<18} {'n':>7} {'P30':>8} {'P70':>8} "
          f"{'P10':>8} {'P90':>8}")
    for market in bb.BUSINESS_MARKETS:
        for dim in bb.PATTERN_DIMS:
            e = calib["ecdf"][market][dim]
            q = e["quantiles"]
            print(f"{market:<4} {dim:<18} {e['n']:>7} "
                  f"{e['p30']:>8.2f} {e['p70']:>8.2f} {q[10]:>8.2f} {q[90]:>8.2f}")
    print("规则优先级：" + " > ".join(row["params"]["pattern_priority"]))
    print("确认口径：dwell_days >= %d → status=已确认（state 恒当日 raw）"
          % row["params"]["confirm_days"])


def main() -> int:
    ap = argparse.ArgumentParser(description="冻结市场形态指标的六维 ECDF 并写字典")
    ap.add_argument("--probe", action="store_true", help="只读报告")
    ap.add_argument("--write", action="store_true", help="写 business_dict")
    args = ap.parse_args()
    if not (args.probe or args.write):
        ap.error("需指定 --probe / --write 之一")

    with get_conn() as conn:
        calib = bb.pattern_calibrate(conn)
        row = bb.pattern_build_dict_row(calib, bb.pattern_upstream_hashes(conn))
        _report(calib, row)
        if args.write:
            bulk_upsert(conn, bb.BUSINESS_DICT_TABLE, [row],
                        conflict_cols=["biz_code"], skip_null_updates=True)
            conn.commit()
            print(f"✅ 已写入 {bb.BUSINESS_DICT_TABLE} {bb.PATTERN_BIZ_CODE}")
        else:
            print("（--probe 未写库）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
