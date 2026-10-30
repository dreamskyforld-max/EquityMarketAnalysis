"""分时切分研究 · 批量生成待标注包（图 + 窗口清单 + 反馈模板）

    python3 -m research.force_segmentation.seg.labelpack HK.09988 2026-10-29 2026-10-28
    python3 -m research.force_segmentation.seg.labelpack --auto 4 2      # 自动挑 4 票 × 2 天
输出：research/force_segmentation/labels/<code>/<date>.png + windows.md
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.getcwd())

from .panels import codes_with_ticks, load_panel, recent_days
from .viz import plot_day, windows_md

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "labels")


def make(code, days):
    d_out = os.path.join(OUT, code)
    os.makedirs(d_out, exist_ok=True)
    md = [f"# {code} 待标注窗口（v3 口径：flat=10 / ER_Z=1.3 / amp=8%当日振幅）", "",
          "反馈格式：`2026-10-29: ①②合并；⑤在13:20拆开；⑦起点改到14:05`", ""]
    n_win = 0
    for day in days:
        tt, p = load_panel(code, day)
        if len(tt) < 60:
            print(f"  {code} {day}: 数据不足（{len(tt)} 分钟），跳过")
            continue
        png = os.path.join(d_out, f"{day}.png")
        plot_day(code, day, out=png)
        _, rows = plot_day(code, day, out=png)
        md.append(windows_md(code, day, rows, png))
        n_win += len(rows)
        print(f"  {code} {day}: {len(rows)} 窗 → {png}")
    with open(os.path.join(d_out, "windows.md"), "w") as fh:
        fh.write("\n".join(md))
    print(f"  清单：{d_out}/windows.md（{n_win} 窗）")
    return n_win


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--auto":
        n_code = int(args[1]) if len(args) > 1 else 4
        n_day = int(args[2]) if len(args) > 2 else 2
        picks = [c for c in codes_with_ticks() if c != "HK.00700"][:n_code]
        total = 0
        for c in picks:
            ds = recent_days(c, n_day)
            total += make(c, ds[-n_day:])
        print(f"合计 {total} 个窗口待标注")
    else:
        code = args[0]
        days = [dt.date.fromisoformat(a) for a in args[1:]]
        make(code, days)
