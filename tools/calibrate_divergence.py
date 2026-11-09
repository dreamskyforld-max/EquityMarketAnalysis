#!/usr/bin/env python3
"""`BIZ.MARKET_DIVERGENCE` 字典标定：冻结 12 组 (market, dimension) P30/P70 + 写字典。

落地口径（2026-11-07 定案，见 `doc/market_state_data_architecture.md` §6.3 v1.33）：

  · 撤销原规格的「六维 score 直接相减 ≥25」——六维 score 是各自历史 expanding
    分位，分位基数 / 分布宽度 / 有效起点均不同，跨维加减违反
    `doc/market_profile.md` 的硬约束。实测同一阈值 25 下 D2 命中率
    CN 36.8% / HK 64.3%，且原四档切点第 4 档全历史 0 天。
  · 改为按 `(market, dimension)` 各自历史 score 冻结 P30/P70，把 score 切成
    L/M/H 三档后取**序数档位差** delta∈{0,1,2}；state 按归一强度
    Σdelta/(2×n_valid) 切四档。这与 §6.2「先转自身相对位置再判定」同范式。
  · 标定窗口截至 `DIVERGENCE_CALIB_END`（2025-12-31），2026 起为样本外；
    冻结切点存在**参数层前视**，PIT 敏感性由 `tools/validate_divergence.py` 报告。
  · 切点 / 对清单 / 强度切点 / 六维 params_hash 快照全部进 `business_dict.params`
    并纳入 `params_hash`，改任一处即触发历史全量重算。
  · 本脚本是字典的常规写入口；`build_business_value.py --seed` 走同一
    `divergence_build_dict_row()`，两者结果逐值一致。

用法：
    .venv/bin/python3 tools/calibrate_divergence.py --probe    # 只读报告，不写库
    .venv/bin/python3 tools/calibrate_divergence.py --write    # 写 business_dict 一行
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
    print(f"{'市场':<4} {'维度':<18} {'n':>7} {'P30':>8} {'P70':>8}")
    for market in bb.BUSINESS_MARKETS:
        for dim in bb.DIVERGENCE_DIMS:
            c = calib["band_cuts"][market][dim]
            print(f"{market:<4} {dim:<18} {c['n']:>7} "
                  f"{c['p30']:>8.2f} {c['p70']:>8.2f}")
    p = row["params"]
    print("强度切点：" + str(p["strength_bands"]) +
          " → " + " / ".join(p["state_labels"]))
    print(f"发布门禁：min_pairs={p['min_pairs']} / "
          f"min_distinct_dims={p['min_distinct_dims']} / "
          f"publish_policy={p['publish_policy']}")
    print("对清单：" + " > ".join(r["id"] for r in p["pair_rules"]))


def main() -> int:
    ap = argparse.ArgumentParser(description="冻结侧面背离指标的六维档位切点并写字典")
    ap.add_argument("--probe", action="store_true", help="只读报告")
    ap.add_argument("--write", action="store_true", help="写 business_dict")
    args = ap.parse_args()
    if not (args.probe or args.write):
        ap.error("需指定 --probe / --write 之一")

    with get_conn() as conn:
        calib = bb.divergence_calibrate(conn)
        row = bb.divergence_build_dict_row(calib, bb.divergence_upstream_hashes(conn))
        _report(calib, row)
        if args.write:
            bulk_upsert(conn, bb.BUSINESS_DICT_TABLE, [row],
                        conflict_cols=["biz_code"], skip_null_updates=True)
            conn.commit()
            print(f"✅ 已写入 {bb.BUSINESS_DICT_TABLE} {bb.DIVERGENCE_BIZ_CODE}")
        else:
            print("（--probe 未写库）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
