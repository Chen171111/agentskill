"""把送转口径修正的 Δ 拆成两块：① 同一批股票的 **dy 数值变化** ② **名单换血**。

为什么要拆
----------
`legacy → correct` 后所有配置的 Δ 都为负，但**降幅从 −0.75pp 到 −14.13pp 跨了 19 倍** ——
说明这不是"水平缩放"。要解释清楚，必须知道 Δ 里有多少来自：
- **(A) 数值**：同一批股票、只是 dy 算得不一样（换了排名）
- **(B) 换血**：选出来的 20 只直接换了一批

做法（**不需要跑回测**，只要两份选股计划）：
```
prepare(口径 legacy)  ─┐
                       ├─> plan_selections(mode='indpct', topn=20, hold=60) ─> {调仓日: [code]}
prepare(口径 correct) ─┘
```
再按调仓日算 **Jaccard 重叠**；并报**共同持仓那部分的 dy 变化**（把 (A) 隔离出来）。

⚠️ **每个区间必须用它自己锚定的调仓日**（`dates` 用区间内的日期重新起算）——
沿用全区间的相位会导致两个区间的调仓日不相交（`tools/README.md` 铁律 7② 踩过）。

⚠️ 只比 `indpct`（定稿形态）。

用法
----
    PY="C:/Users/XiaoQi/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
    cd "E:/MyWorkAndProject/量化/agentskill"
    $PY tools/cmp_selection_overlap.py
"""
from __future__ import annotations

import argparse
import os
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_dividend import prepare as prepare_div          # noqa: E402
from tools.test_industry_neutral import plan_selections             # noqa: E402

SEGS = [("全区间", "20190101", "20260911"),
        ("样本内 2019~2022", "20190101", "20221231"),
        ("样本外 2023~2026", "20230101", "20260911")]


def _panel(args, mode: str, ind: pd.DataFrame) -> pd.DataFrame:
    ns = SimpleNamespace(bars=args.bars, bfq=args.bfq, dividends=args.dividends,
                         universe=args.universe, min_listed=args.min_listed,
                         min_amount=args.min_amount, min_price=args.min_price,
                         adj_mode=mode)
    print("\n---- 构建面板 adj_mode = {} ----".format(mode), flush=True)
    df = prepare_div(ns)
    df = df.merge(ind, on="code", how="left")
    df["ind_l1"] = df.ind_l1.fillna("未分类")
    return df


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="legacy vs correct 选股清单重叠度")
    ap.add_argument("--bars", default="data/stockbars/bars_total_tax10.parquet")
    ap.add_argument("--bfq", default="data/stockbars/bars_bfq.parquet")
    ap.add_argument("--dividends", default="data/dividends/bonus_all.parquet")
    ap.add_argument("--industry", default="data/industry/industry_all.parquet")
    ap.add_argument("--universe", default="data/stockbars/universe_all.csv")
    ap.add_argument("--topn", type=int, default=20)
    ap.add_argument("--hold", type=int, default=60)
    ap.add_argument("--max-dy", type=float, default=10.0)
    ap.add_argument("--min-dy", type=float, default=0.5)
    ap.add_argument("--min-div3", type=int, default=2)
    ap.add_argument("--min-price", type=float, default=2.0)
    ap.add_argument("--min-amount", type=float, default=3e7)
    ap.add_argument("--min-listed", type=int, default=120)
    ap.add_argument("--out", default="results/adj_selection_overlap.csv")
    args = ap.parse_args(argv)

    print("=" * 104)
    print("  Δ 分解：同一批股票的「dy 数值变化」 vs 「名单换血」")
    print("=" * 104)

    ind = pd.read_parquet(args.industry)[["code_full", "ind_l1"]]
    ind = ind.rename(columns={"code_full": "code"})

    panels, seg_plans = {}, {}
    for mode in ("legacy", "correct"):
        df = _panel(args, mode, ind)
        panels[mode] = df
        by_date = {t: v for t, v in df.groupby("date", sort=False).indices.items()}
        all_dates = sorted(df.date.unique())
        seg_plans[mode] = {}
        for name, s, e in SEGS:
            # ⚠️ 每个区间用**自己锚定**的调仓日（否则相位不相交）
            dts = [t for t in all_dates if s <= t <= e]
            seg_plans[mode][name] = plan_selections(
                df, dts, by_date, mode="indpct", topn=args.topn, hold=args.hold,
                dy_col="dy_ttm", min_dy=args.min_dy, max_dy=args.max_dy,
                min_div3=args.min_div3)
            print("    计划 {} / {} -> {} 个调仓日".format(
                mode, name, len(seg_plans[mode][name])), flush=True)

    print()
    print("  {:<18}{:>8}{:>10}{:>12}{:>12}{:>13}{:>13}{:>10}".format(
        "区间", "调仓期", "平均持仓", "Jaccard均", "完全相同", "correct dy", "legacy dy", "dy 差"))
    rows = []
    for name, s, e in SEGS:
        pl_l = seg_plans["legacy"][name]
        pl_c = seg_plans["correct"][name]
        df_l, df_c = panels["legacy"], panels["correct"]
        jac, nh, same, dy_c_list, dy_l_list = [], [], 0, [], []
        for k in sorted(set(pl_l) & set(pl_c)):
            a, b = set(pl_l[k] or []), set(pl_c[k] or [])
            if not a and not b:
                continue
            jac.append(len(a & b) / max(len(a | b), 1))
            nh.append(len(b))
            if a == b:
                same += 1
            common = sorted(a & b)
            if common:
                yc = df_c[(df_c.date == k) & (df_c.code.isin(common))].dy_ttm
                yl = df_l[(df_l.date == k) & (df_l.code.isin(common))].dy_ttm
                if len(yc) and len(yl):
                    dy_c_list.append(float(yc.mean()))
                    dy_l_list.append(float(yl.mean()))
        if not jac:
            print("  {:<18}（无相交调仓日）".format(name))
            continue
        mc = float(np.mean(dy_c_list)) if dy_c_list else np.nan
        ml = float(np.mean(dy_l_list)) if dy_l_list else np.nan
        rows.append((name, len(jac), float(np.mean(nh)), float(np.mean(jac)),
                     "{}/{}".format(same, len(jac)), mc, ml, mc - ml))
        print("  {:<18}{:>8}{:>10.1f}{:>12.3f}{:>12}{:>13.2f}{:>13.2f}{:>10.2f}".format(*rows[-1]))

    print()
    print("  读法：")
    print("    · **Jaccard 均** = 两口径名单的重叠度：1.0 = 完全相同（纯数值差异）、0 = 完全换血。")
    print("    · **完全相同** = 名单一字不差的调仓日数 / 总期数。")
    print("    · **dy 差** = 共同持仓那部分，correct 比 legacy 低几个百分点 → 这是 (A) 数值部分；")
    print("      名单重叠越低，(B) 换血 的占比越大。")
    print("    ⚠️ 注意「共同持仓的 dy 差」只描述**交集那部分**，不代表整个组合的降幅。")

    if rows:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        pd.DataFrame(rows, columns=["区间", "调仓期", "平均持仓", "Jaccard均",
                                    "完全相同", "correct_dy", "legacy_dy", "dy差"]
                     ).to_csv(args.out, index=False, encoding="utf-8-sig")
        print("\n  已写出 {}".format(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
