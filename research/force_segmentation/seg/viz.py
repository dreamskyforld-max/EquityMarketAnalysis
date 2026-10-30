"""分时切分研究 · 单日切分可视化（需 matplotlib，仅研究用）

    python3 -m research.force_segmentation.seg.viz HK.00700 2026-10-29
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import numpy as np

sys.path.insert(0, os.getcwd())

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .common import sigma_noise, dir_label, flag_of, chg_of, er_of
from .panels import load_panel
from .rules import segment_v3

plt.rcParams["font.sans-serif"] = ["PingFang SC", "Hiragino Sans GB", "Arial Unicode MS"]
plt.rcParams["axes.unicode_minus"] = False

CIRC = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚"
C = {"上涨": "#d9534f", "下跌": "#2e9e5b", "横盘": "#9aa0a6"}


def plot_day(code, day, segs=None, out=None, title_extra=""):
    tt, p = load_panel(code, day)
    if not len(tt):
        return None
    n = len(p)
    sn = sigma_noise(p)
    if segs is None:
        segs = segment_v3(p, tt)
    segs = [(s, e) for s, e, *_ in segs]
    fig, ax = plt.subplots(figsize=(15, 6.2))
    ymax, ymin = p.max() * (1 + 0.004), p.min() * (1 - 0.003)
    rows = []
    for i, (s, e) in enumerate(segs):
        d = dir_label(p, s, e, sn)
        chg, er = chg_of(p, s, e), er_of(p, s, e)
        f = flag_of(p, s, e, sn, d)
        rows.append((CIRC[i], f"{tt[s]}-{tt[e]}", d, chg, e - s + 1, er, f or ""))
        ax.axvspan(s - 0.5, e + 0.5, color=C[d], alpha=0.10, lw=0)
        ax.axvline(e + 0.5, color="#444", lw=0.9, alpha=0.7)
        ax.axvline(s - 0.5, color="#444", lw=0.9, alpha=0.7)
        ax.text((s + e) / 2, ymax - (ymax - ymin) * 0.012, CIRC[i], ha="center", va="top",
                fontsize=13, color=C[d], fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.16", fc="white", ec=C[d], lw=1.2))
        ax.text((s + e) / 2, ymax - (ymax - ymin) * 0.065,
                f"{d}{'·' + f if f else ''}\n{chg:+.2f}% {e-s+1}min\nER {er:.2f}",
                ha="center", va="top", fontsize=7.5, color="#333", linespacing=1.3)
    ax.plot(np.arange(n), p, color="#c8763c", lw=1.2, zorder=5)
    mm = [int(t[:2]) * 60 + int(t[3:]) for t in tt]
    for i in range(1, n):
        if mm[i] - mm[i - 1] > 1:
            ax.axvline(i - 0.5, color="#111", ls="--", lw=1.3)
    ax.set_ylim(ymin, ymax)
    ax.set_ylabel("价格")
    amp = (p.max() - p.min()) / p.min() * 100
    ax.set_title(f"{code} {day}｜{len(segs)} 个窗口｜振幅 {amp:.2f}%{title_extra}",
                 fontsize=12, loc="left")
    ticks = list(range(0, n, 20))
    ax.set_xticks(ticks)
    ax.set_xticklabels([tt[i] for i in ticks], fontsize=8)
    ax.set_xlim(-1, n)
    fig.tight_layout()
    out = out or f"/tmp/{code}_{day}.png"
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out, rows


def windows_md(code, day, rows, png):
    md = [f"## {code} {day}（{len(rows)} 窗，图：{png}）", "",
          "| 编号 | 时段 | 方向 | 涨跌% | 时长 | ER | 标记 |", "|---|---|---|---|---|---|---|"]
    for c, tz, d, chg, m, er, f in rows:
        md.append(f"| {c} | {tz} | {d} | {chg:+.3f} | {m}min | {er:.2f} | {f} |")
    md += ["", f"**待标注**：`{day}: `", ""]
    return "\n".join(md)


if __name__ == "__main__":
    code = sys.argv[1] if len(sys.argv) > 1 else "HK.00700"
    day = dt.date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else dt.date(2026, 10, 29)
    out, rows = plot_day(code, day)
    print(out)
    for r in rows:
        print("  ", " ".join(str(x) for x in r))
