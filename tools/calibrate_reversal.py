#!/usr/bin/env python3
"""`BIZ.REVERSAL_RISK` 字典标定：冻结 5 族切点 / 判读阈值 / 发布日历并写字典。

落地口径（2026-11-07 定案，见 `doc/market_state_data_architecture.md` §6.4 v1.34）：

  · §6.4 原规格的「理想 18 顶 + 12 底」族清单无法落地：底部 12 条无 `category`、
    FSI 无原子、基本面族 CN 2026-10-08 起仅 22 行 / HK 无源，8 族全齐区间
    CN 约 22 个交易日。离线 Go/No-Go 预检后收缩为「固定 5 族 × 每族 1 代表原子」，
    宏观 / 基本面 / 结构移出分母，写进 `detail.unavailable_families`。
  · 切点按 `(market, family)` 在标定窗口内取 P90 / P10（输入本身是 0-100 as-of
    分位），避免 §6.2 v1.32 证伪过的跨市场绝对切点。
  · 判读阈值按市场标定：在「5 族全齐 + 发布起点后」样本里，选使高风险档占比
    最接近 `REVERSAL_TARGET_HIGH_SHARE` 的整数族数切点。原初值 `[0,2,4]`
    实测 HK 全历史仅 1 日高风险，按 §8.5 属阈值失效。
  · 发布日历按市场冻结（CN 2019-01-02 / HK 2013-09-10），当日须 5 族分位齐全。
  · 切点 / 阈值 / 日历 / 族清单版本 / 6 个去重 L1 原子 `params_hash` 快照全部进
    `business_dict.params` 并纳入 `params_hash`，改任一处即触发历史全量重算。
  · 本脚本是字典的常规写入口；`build_business_value.py --seed` 走同一
    `reversal_build_dict_row()`，两者结果逐值一致。

用法：
    .venv/bin/python3 tools/calibrate_reversal.py --probe    # 只读报告，不写库
    .venv/bin/python3 tools/calibrate_reversal.py --write    # 写 business_dict 一行
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
    p = row["params"]
    print(f"族清单版本 {p['family_list_version']}｜标定窗口截至 "
          f"{calib['calib_end']}｜样本 {calib['n_obs']} 行｜"
          f"params_hash = {row['params_hash']}")
    print(f"{'市场':<4} {'族':<12} {'原子':<32} {'n':>7} {'P10':>8} {'P90':>8}")
    for market in bb.BUSINESS_MARKETS:
        for fam in bb.REVERSAL_FAMILIES:
            c = calib["family_cuts"][market][fam]
            print(f"{market:<4} {fam:<12} {p['family_atoms'][market][fam]:<32} "
                  f"{c['n']:>7} {c['bottom']:>8.2f} {c['top']:>8.2f}")
    for market in bb.BUSINESS_MARKETS:
        b = calib["market_bands"][market]
        print(f"{market} 判读阈值：warn_from={b['warn_from']} / "
              f"high_from={b['high_from']}｜标定样本 {b['calib_n']} 日，"
              f"高风险占比 {b['high_share']:.2%}（目标 "
              f"{p['target_high_share']:.0%}）｜发布起点 {p['publish_from'][market]}")
    print("移出分母的族：" + "、".join(p["excluded_families"]))
    print("共变簇（描述性标注，不宣称独立侧面）：" +
          str(p["correlated_families"]))


def main() -> int:
    ap = argparse.ArgumentParser(description="冻结反转风险 5 族切点与判读阈值并写字典")
    ap.add_argument("--probe", action="store_true", help="只读报告")
    ap.add_argument("--write", action="store_true", help="写 business_dict")
    args = ap.parse_args()
    if not (args.probe or args.write):
        ap.error("需指定 --probe / --write 之一")

    with get_conn() as conn:
        calib = bb.reversal_calibrate(conn)
        row = bb.reversal_build_dict_row(calib, bb.reversal_upstream_hashes(conn))
        _report(calib, row)
        if args.write:
            bulk_upsert(conn, bb.BUSINESS_DICT_TABLE, [row],
                        conflict_cols=["biz_code"], skip_null_updates=True)
            conn.commit()
            print(f"✅ 已写入 {bb.BUSINESS_DICT_TABLE} {bb.REVERSAL_BIZ_CODE}")
        else:
            print("（--probe 未写库）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
