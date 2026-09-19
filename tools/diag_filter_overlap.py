"""阈值邻域之间的**选股重叠度** —— 回答「`≥3次` 和 `≤8%` 是不是在选同一批股票」。

为什么需要（2026-09-18）
------------------------
回灌 `docs/个股线_股息率策略.md` §4.3 时（补 R25~R29 五个扫描点）发现：

| 过滤 | 全区间 | 样本内 | 样本外 |
|---|---|---|---|
| 裸 | 9.01% | 8.91% | 5.95% |
| `≤8%` | 9.57% | **13.72%** | 3.27% |
| `≥2次`（定稿） | 8.24% | 9.03% | 5.83% |
| **`≥3次`** | **11.14%** | **12.05%** | **7.64%** |

`≥3次` 三段全面更优，但 `≤8%` 在样本内也跳到 13.72% —— 两者画像相似。
**在下"该不该把定稿从 `≥2次` 换成 `≥3次`"的结论之前，必须先回答：
它们是不是在挑同一批股票？** 若是，则 `≥3次` 只是"更严的上限"的另一种写法，
两条证据互相重复；若否，则 `≥3次` 是一个独立的信息源。

本脚本只做这一件事（不重写选股逻辑）：
复用 `test_industry_neutral.plan_selections`（**选股逻辑唯一来源**）与
`diag_industry_hyst_overlap.jaccard`，逐调仓日算 8 个阈值配置两两的 Jaccard。

用法
----
    PY=.../python.exe
    $PY tools/diag_filter_overlap.py \
        --bars data/stockbars/bars_total.parquet \
        --bfq  data/stockbars/bars_bfq.parquet \
        --dividends data/dividends/bonus_all.parquet \
        --industry data/industry/industry_all.parquet \
        --universe data/stockbars/universe_all.csv \
        --topn 20 --hold 60 --adj-mode correct \
        --out results/diag_filter_overlap.csv
"""
from __future__ import annotations

import argparse
import os
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_dividend import prepare as prepare_div  # noqa: E402
from tools.test_industry_neutral import plan_selections  # noqa: E402
from tools.diag_industry_hyst_overlap import jaccard  # noqa: E402

# (标签, max_dy, min_div3) —— 与 run_reruns.py 的 R20/R21/R22/R25/R26/R27/R28/R29 逐位对应
CONFIGS: list[tuple[str, float, int]] = [
    ("裸", 999.0, 0),
    ("≤8%", 8.0, 0),
    ("≤10%", 10.0, 0),
    ("≤12%", 12.0, 0),
    ("≤15%", 15.0, 0),
    ("≥1次", 999.0, 1),
    ("≥2次", 999.0, 2),
    ("≥3次", 999.0, 3),
]

PERIODS = [("全区间", "20190101", "20260911"),
           ("样本内 2019~2022", "20190101", "20221231"),
           ("样本外 2023~2026", "20230101", "20260911")]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="阈值邻域之间的选股重叠度（Jaccard）")
    ap.add_argument("--bars", default="data/stockbars/bars_total.parquet")
    ap.add_argument("--bfq", default="data/stockbars/bars_bfq.parquet")
    ap.add_argument("--dividends", default="data/dividends/bonus_all.parquet")
    ap.add_argument("--industry", default="data/industry/industry_all.parquet")
    ap.add_argument("--universe", default="data/stockbars/universe_all.csv")
    ap.add_argument("--dy-col", default="dy_ttm")
    ap.add_argument("--min-dy", type=float, default=0.5)
    ap.add_argument("--min-listed", type=int, default=120)
    ap.add_argument("--min-amount", type=float, default=3e7)
    ap.add_argument("--min-price", type=float, default=2.0)
    ap.add_argument("--topn", type=int, default=20)
    ap.add_argument("--hold", type=int, default=60)
    ap.add_argument("--adj-mode", default=None)
    ap.add_argument("--out", default="results/diag_filter_overlap.csv")
    args = ap.parse_args(argv)

    if not args.adj_mode:
        raise SystemExit("必须显式给 --adj-mode（legacy|correct），不允许静默默认")

    ns = SimpleNamespace(bars=args.bars, bfq=args.bfq, dividends=args.dividends,
                         universe=args.universe, min_listed=args.min_listed,
                         min_amount=args.min_amount, min_price=args.min_price,
                         adj_mode=args.adj_mode)
    df = prepare_div(ns)
    ind = pd.read_parquet(args.industry)[["code_full", "ind_l1"]]
    ind = ind.rename(columns={"code_full": "code"})
    df = df.merge(ind, on="code", how="left")
    df["ind_l1"] = df.ind_l1.fillna("未分类")

    dates = sorted(df.date.unique())
    by_date = {t: v for t, v in df.groupby("date", sort=False).indices.items()}
    code_arr = df.code.values
    in_uni = df._in_uni.values
    dy_all = df[args.dy_col].values
    nd3_all = df["n_div3"].values

    print(f"日线 {len(df):,} 行 / {df.code.nunique():,} 只 ｜ 调仓日 {len(dates[::args.hold])} 个"
          f" ｜ adj_mode={args.adj_mode}")

    # ---------------- 1) 各配置的选股（复用唯一来源） ----------------
    plans: dict[str, dict] = {}
    for tag, mdy, md3 in CONFIGS:
        plans[tag] = plan_selections(
            df, dates, by_date, mode="topn", topn=args.topn, hold=args.hold,
            dy_col=args.dy_col, min_dy=args.min_dy, max_dy=mdy, min_div3=md3)
    all_dates = sorted(plans["裸"])

    # ---------------- 2) 逐调仓日：基准池 + 各配置持仓 ----------------
    def base_pool_size(t) -> int:
        """**基准池**（在池 ∩ dy≥min_dy ∩ 有行业标签）—— 只用于对照，不是任何选股规则。
        ⚠️ 各配置的**实际**候选池（含 max_dy / min_div3）由 `plan_selections` 决定，
        本脚本**不重写**那份逻辑（铁律 14：同一判据多处实现＝迟早分叉）。"""
        rows = by_date.get(t)
        if rows is None:
            return 0
        rows = rows[in_uni[rows]]
        if len(rows) == 0:
            return 0
        dy = dy_all[rows]
        ind_ok = df.ind_l1.values[rows] != "未分类"
        return int((~np.isnan(dy) & (dy >= args.min_dy) & ind_ok).sum())

    rows = []
    for t in all_dates:
        rec = {"date": t, "基准池": base_pool_size(t)}
        for tag, mdy, md3 in CONFIGS:
            sel = plans[tag].get(t, [])
            rec[f"{tag}·持仓"] = len(sel)
            if sel:
                d = pd.Series(dy_all[by_date[t]], index=code_arr[by_date[t]]).reindex(sel).dropna()
                rec[f"{tag}·dy中位"] = float(d.median()) if len(d) else np.nan
        # 两两 Jaccard
        for i, (ta, _, _) in enumerate(CONFIGS):
            for tb, _, _ in CONFIGS[i + 1:]:
                rec[f"J·{ta}|{tb}"] = jaccard(plans[ta].get(t, []), plans[tb].get(t, []))
        rows.append(rec)
    r = pd.DataFrame(rows)
    r.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"逐日明细已写出 {args.out}（{len(r)} 行）\n")

    # ---------------- 3) 分区间汇总 ----------------
    print("=" * 96)
    print("  基准池 / 各配置持仓 / 持仓 dy 中位（区间中位数）")
    print("=" * 96)
    for ptag, s, e in PERIODS:
        sub = r[(r.date >= s) & (r.date <= e)]
        print(f"\n  【{ptag}】调仓日 {len(sub)} 个")
        print(f"    {'配置':8s} {'基准池':>8s} {'持仓':>6s} {'持仓dy中位':>10s}")
        print("    " + "-" * 38)
        for tag, _, _ in CONFIGS:
            print(f"    {tag:8s} {sub['基准池'].median():8.0f} "
                  f"{sub[f'{tag}·持仓'].median():6.0f} "
                  f"{sub[f'{tag}·dy中位'].median():10.2f}")

    print()
    print("=" * 96)
    print("  Jaccard 重叠度矩阵（区间均值，1 = 选股完全相同）")
    print("=" * 96)
    tags = [t for t, _, _ in CONFIGS]
    for ptag, s, e in PERIODS:
        sub = r[(r.date >= s) & (r.date <= e)]
        print(f"\n  【{ptag}】")
        header = "    " + " " * 8 + "".join(f"{t:>9s}" for t in tags)
        print(header)
        for i, ta in enumerate(tags):
            cells = []
            for j, tb in enumerate(tags):
                if i == j:
                    cells.append(f"{'1.000':>9s}")
                elif j > i:
                    cells.append(f"{sub[f'J·{ta}|{tb}'].mean():9.3f}")
                else:
                    cells.append(f"{sub[f'J·{tb}|{ta}'].mean():9.3f}")
            print(f"    {ta:8s}" + "".join(cells))

    # ---------------- 4) 结论判读 ----------------
    print()
    print("=" * 96)
    print("  判读")
    print("=" * 96)
    print("  · J(≥3次, ≥2次) 高（>0.8）→ `≥3次` 只是 `≥2次` 的**加严版**，两条证据高度重复")
    print("  · J(≥3次, ≤8%) 高（>0.8）→ 两者在挑同一批股票 → **不是独立信息源**")
    print("  · 两者都低（<0.5）→ `≥3次` 是**独立的过滤维度**，其增量不与上限族共享")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
