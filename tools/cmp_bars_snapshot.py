"""比对两份 `bars_total*.parquet` 快照 —— 回答「数据刷新后，回测结果会变吗？」

为什么需要（2026-09-18）
------------------------
`tools/run_reruns.py --list` 的「产物是否有效」判据是 **mtime**：产物比输入新才算有效。
这个判据是**保守**的（宁可多跑），但会**误报** —— 数据刷新后 31 个产物会全部显示"待跑"，
而实际上**分析窗口可能根本没变**。

具体机制：所有分析工具的 `--end` 默认值都**硬编码**（当前 `20260911`），
而数据刷新只是**往后延长**（`_bfq_parts` → merge → rebuild）。
→ 只要刷新**没有改动窗口内的历史**，重跑的结果就**逐位相同**，7 小时纯属白跑。

而"有没有改动窗口内历史"这件事，光看文件大小/mtime 判断不了，必须**比内容**。

本脚本做的判断
--------------
`bars_total*.parquet` 的 `close` 是**重建出来的总收益路径**（`P(t)=P(t−1)·(1+ret)`），
它的**绝对价位**会被整体缩放（`rebuild_returns` 归一化到首个交易日），
所以直接比 `close` 会看到差异、但那是假差异。**真正要看的是"收益率"。**

于是本脚本对每只股票算 `ret(t) = close(t)/close(t−1) − 1`，在**重叠区间**内比对：

| 结论 | 含义 | 处置 |
|---|---|---|
| **收益率逐位一致** | 历史段没被改写 | ✅ **不需要重跑**（`--list` 的"待跑"是 mtime 误报） |
| 收益率有差异 | 历史段被改写 | ⚠️ **必须整批重跑**（分批跑会造成产物混合快照） |

同时报**整体缩放比**（`close_new/close_old` 的每只恒定比值）——
缩放比 ≠ 1 只影响 `min_price` 这类**价格类过滤**，不影响收益类指标。

用法
----
    PY=.../python.exe
    # 默认：拿 data/stockbars/_snapshots/ 里最新的快照 vs 当前文件
    $PY tools/cmp_bars_snapshot.py --kind total

    # 指定两份
    $PY tools/cmp_bars_snapshot.py \
        --old data/stockbars/_snapshots/bars_total_20260911.parquet \
        --new data/stockbars/bars_total.parquet \
        --until 20260911
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 各分析工具 `--end` 的硬编码默认值（**窗口末根**）—— 改了工具就要同步这里
WINDOW_END = {
    "total": "20260911",
    "tax10": "20260911",
    "tax20": "20260911",
}


def load(path: str, until: str) -> pd.DataFrame:
    d = pd.read_parquet(path, columns=["code", "date", "close"])
    d["date"] = d.date.astype(str).str.replace("-", "", regex=False)
    d = d[d.date <= until]
    d = d.sort_values(["code", "date"])
    d["ret"] = d.groupby("code", sort=False).close.pct_change()
    return d


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="比对两份 bars_total 快照：收益率是否变了")
    ap.add_argument("--old", default=None, help="旧快照路径（默认自动找 _snapshots 里最新的）")
    ap.add_argument("--new", default=None, help="新文件路径（默认当前 data/stockbars 下的）")
    ap.add_argument("--kind", default="total", choices=["total", "tax10", "tax20"])
    ap.add_argument("--until", default=None, help="只比到这个交易日（默认用窗口末根）")
    args = ap.parse_args(argv)

    snap_dir = os.path.join(ROOT, "data", "stockbars", "_snapshots")
    new = args.new or os.path.join(ROOT, "data", "stockbars",
                                   "bars_total.parquet" if args.kind == "total"
                                   else f"bars_total_{args.kind}.parquet")
    if not args.old:
        cands = sorted(glob.glob(os.path.join(snap_dir, f"bars_total*{args.kind}*.parquet"))
                       if args.kind != "total"
                       else glob.glob(os.path.join(snap_dir, "bars_total_2*.parquet")))
        if not cands:
            raise SystemExit(f"❌ 在 {snap_dir} 找不到旧快照；请用 --old 显式指定")
        args.old = cands[-1]
    until = args.until or WINDOW_END.get(args.kind, "20260911")

    for p in (args.old, new):
        if not os.path.exists(p):
            raise SystemExit(f"❌ 文件不存在：{p}")

    print("=" * 96)
    print(f"  快照比对（kind={args.kind}，窗口末根 {until}）")
    print("=" * 96)
    print(f"  旧 {os.path.relpath(args.old, ROOT)}")
    print(f"  新 {os.path.relpath(new, ROOT)}\n")

    a, b = load(args.old, until), load(new, until)
    print(f"  旧：{len(a):,} 行 / {a.code.nunique():,} 只｜{a.date.min()} ~ {a.date.max()}")
    print(f"  新：{len(b):,} 行 / {b.code.nunique():,} 只｜{b.date.min()} ~ {b.date.max()}\n")

    m = a.merge(b, on=["code", "date"], how="outer", suffixes=("_o", "_n"), indicator=True)
    vc = m._merge.value_counts().to_dict()
    print(f"  行匹配：{vc}")
    both = m[m._merge == "both"].copy()

    # ---- 1) 收益率是否变了（决定性判据）----
    dr = (both.ret_n - both.ret_o).abs()
    nbad = int((dr > 1e-9).sum())
    print("\n  ── 1. 收益率（决定性判据）──")
    print(f"    重叠 {len(both):,} 行｜收益率有差异的 {nbad:,} 行 "
          f"（{nbad / max(len(both), 1) * 100:.4f}%）｜最大绝对差 {dr.max():.3e}")

    # ---- 2) 价位缩放比（只影响价格类过滤）----
    both["ratio"] = both.close_n / both.close_o
    g = both.groupby("code").ratio.agg(["min", "max"])
    g["spread"] = g["max"] - g["min"]
    nonconst = int((g.spread > 1e-6).sum())
    off1 = int((np.abs(g["max"] - 1) > 1e-9).sum())
    off05 = int((np.abs(g["max"] - 1) > 0.005).sum())
    print("\n  ── 2. 价位缩放比（close_new/close_old）──")
    print(f"    缩放比**非恒定**的代码：{nonconst:,} 只"
          f"（>0 = 收益率本身变了，需看第 1 项）")
    print(f"    缩放比 ≠ 1 的代码：{off1:,} / {len(g):,} 只；"
          f"偏离 >0.5% 的：{off05:,} 只｜最小 {g['max'].min():.6f}")
    print("    （缩放比 ≠ 1 只影响 `min_price >= 2.0` 这类价格类过滤，不影响收益类指标）")

    # ---- 3) 结论 ----
    print("\n" + "=" * 96)
    if nbad == 0 and nonconst == 0:
        print("  ✅ 结论：**窗口内收益率逐位一致 → 不需要重跑**")
        print("     `run_reruns.py --list` 显示的「待跑」是 **mtime 误报**（判据保守）。")
        if off05:
            print(f"     ⚠️ 但 {off05} 只股票的**绝对价位**变了（缩放比偏离 >0.5%）→")
            print("        若某步对价格敏感（`min_price` / `min_amount`），建议单独实跑复核。")
    else:
        print("  ⚠️ 结论：**窗口内历史被改写 → 必须整批重跑**")
        print("     ⚠️ 不要分批跑：分批会让产物**混合两个快照**（本项目明令禁止）。")
        print(f"     命令：$PY tools/run_reruns.py --from R1 --force")
    print("=" * 96)

    # ---- 4) 写「复核凭证」—— 让 run_reruns --list 的 mtime 误报能自我说明 ----
    receipt = os.path.join(ROOT, "results", "_snapshot_verified.json")
    data = {}
    if os.path.exists(receipt):
        try:
            data = json.load(open(receipt, encoding="utf-8"))
        except Exception:
            data = {}
    data[args.kind] = {
        "ts": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "until": until,
        "old": os.path.relpath(args.old, ROOT),
        "new": os.path.relpath(new, ROOT),
        "new_size": os.path.getsize(new),
        "new_mtime": int(os.path.getmtime(new)),
        "rows_compared": int(len(both)),
        "ret_diff_rows": nbad,
        "ret_max_abs_diff": float(dr.max()),
        "price_rescaled_codes_gt_0.5pct": off05,
        "verdict": "OK_NO_RERUN" if (nbad == 0 and nonconst == 0) else "MUST_RERUN",
    }
    with open(receipt, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    print(f"\n  复核凭证已写入 {os.path.relpath(receipt, ROOT)}"
          f"（`run_reruns.py --list` 会读它，自动标注哪些「待跑」是 mtime 误报）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
