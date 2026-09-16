"""机制归因：`hyst`（四进三出）与「行业内百分位」到底共享了什么？

背景
----
`docs/个股线_股息率实盘方案.md` 发现：两个改进**在样本外互相抵消**
（4 个 hold 的叠加增量均值 **−0.64pp**，2/4 为正），而它们**各自**都有稳定正增量
（行业内 +5.00pp、hyst +1.92pp，均 4/4 为正）。

**两个各自有效的改进，叠加起来却互相抵消** → 只有两种可能：
① 它们选出的股票**高度重叠**（在做同一件事）；
② 它们各自贡献的收益**在不同区间反号**（偶然）。

本脚本回答①（并顺带排除②）：逐调仓日量化四种构造的
**选股重叠度 / 股息率分布 / 行业集中度 / 候选池大小**。

三个待验假设
------------
| # | 假设 | 判据 |
|---|---|---|
| H1 | `indpct` 隐含地**提高了股息率门槛**（≈ 某个 `min_dy` 的 `topn`） | 扫描 `min_dy`，看哪个值与 `indpct` 的重叠度最高；并看 `indpct` 选股里 dy < 8% 的占比 |
| H2 | `hyst` 与 `indpct` **选出大部分相同的股票** | 两者 Jaccard 重叠度 |
| H3 | 两者都在**压缩行业集中度 / 候选池** | 组合行业数、行业 HHI、候选池大小 |

用法
----
    PY=.../python.exe
    $PY tools/diag_industry_hyst_overlap.py \
        --bars data/stockbars/bars_total_tax10.parquet \
        --bfq  data/stockbars/bars_bfq.parquet \
        --dividends data/dividends/bonus_all.parquet \
        --industry data/industry/industry_all.parquet \
        --universe data/stockbars/universe_all.csv \
        --topn 20 --hold 60
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

CONSTRUCTS = [
    ("原始 topn", "topn", None, None),
    ("行业内百分位", "indpct", None, None),
    ("hyst 8/4", "topn", 8.0, 4.0),
    ("hyst+行业内", "indpct", 8.0, 4.0),
]
PERIODS = [("全区间", "20190101", "20260911"),
           ("样本内 2019~2022", "20190101", "20221231"),
           ("样本外 2023~2026", "20230101", "20260911")]
# 用于 H1：给纯 topn 加不同门槛，看哪个最接近 indpct 的选股
MIN_DY_GRID = [0.5, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]


def jaccard(a, b):
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return np.nan
    return len(sa & sb) / len(sa | sb)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="hyst × 行业内选股：机制归因")
    ap.add_argument("--bars", default="data/stockbars/bars_total_tax10.parquet")
    ap.add_argument("--bfq", default="data/stockbars/bars_bfq.parquet")
    ap.add_argument("--dividends", default="data/dividends/bonus_all.parquet")
    ap.add_argument("--industry", default="data/industry/industry_all.parquet")
    ap.add_argument("--universe", default="data/stockbars/universe_all.csv")
    ap.add_argument("--topn", type=int, default=20)
    ap.add_argument("--hold", type=int, default=60)
    ap.add_argument("--dy-col", default="dy_ttm")
    ap.add_argument("--min-dy", type=float, default=0.5)
    ap.add_argument("--max-dy", type=float, default=10.0)
    ap.add_argument("--min-div3", type=int, default=2)
    ap.add_argument("--min-price", type=float, default=2.0)
    ap.add_argument("--min-amount", type=float, default=3e7)
    ap.add_argument("--min-listed", type=int, default=120)
    ap.add_argument("--out", default="results/diag_ind_hyst_overlap.csv")
    args = ap.parse_args(argv)

    print("=" * 100)
    print("  机制归因：hyst × 行业内百分位 —— 它们共享了什么？")
    print("=" * 100)

    ns = SimpleNamespace(bars=args.bars, bfq=args.bfq, dividends=args.dividends,
                         universe=args.universe, min_listed=args.min_listed,
                         min_amount=args.min_amount, min_price=args.min_price)
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
    ind_all = df.ind_l1.values

    def pool_view(t):
        """返回该日的 (合格池 dy Series, 行业 Series)。"""
        rows = by_date.get(t)
        if rows is None:
            return None, None
        rows = rows[in_uni[rows]]
        if len(rows) == 0:
            return None, None
        dy = pd.Series(dy_all[rows], index=code_arr[rows])
        nd3 = pd.Series(nd3_all[rows], index=code_arr[rows])
        ind_s = pd.Series(ind_all[rows], index=code_arr[rows])
        ok = dy.notna() & (dy >= args.min_dy) & (dy <= args.max_dy) \
            & (nd3.fillna(0) >= args.min_div3) & (ind_s != "未分类")
        return dy[ok], ind_s[ok]

    # ---------------- 1) 四种构造的选股与画像 ----------------
    plans = {}
    for tag, mode, he, hx in CONSTRUCTS:
        plans[tag] = plan_selections(df, dates, by_date, mode=mode, topn=args.topn,
                                     hold=args.hold, dy_col=args.dy_col,
                                     min_dy=args.min_dy, max_dy=args.max_dy,
                                     min_div3=args.min_div3,
                                     hyst_entry=he, hyst_exit=hx)

    rows = []
    for t in sorted(plans["原始 topn"]):
        dy_pool, ind_pool = pool_view(t)
        if dy_pool is None or dy_pool.empty:
            continue
        rec = {"date": t, "合格池": len(dy_pool)}
        # 合格池的行业股息率水平（用于衡量"组合是否集中在高股息行业"）
        pool_ind_dy = dy_pool.groupby(ind_pool).mean()
        rec["池·行业dy中位"] = pool_ind_dy.median()
        for tag, _, he, _ in CONSTRUCTS:
            sel = plans[tag].get(t, [])
            rec[f"{tag}·持仓数"] = len(sel)
            if not sel:
                continue
            d = dy_pool.reindex(sel).dropna()
            # ⚠️ `dy_ttm` 已经是**百分数**（`min_dy=0.5` 即 0.5%），不要再乘 100
            rec[f"{tag}·dy均值"] = d.mean()
            rec[f"{tag}·dy中位"] = d.median()
            rec[f"{tag}·dy最低"] = d.min() if len(d) else np.nan
            rec[f"{tag}·dy<8占比"] = (d < 8.0).mean() * 100 if len(d) else np.nan
            ii = ind_pool.reindex(sel).dropna()
            rec[f"{tag}·行业数"] = ii.nunique()
            w = ii.value_counts(normalize=True)
            rec[f"{tag}·行业HHI"] = float((w ** 2).sum()) if len(w) else np.nan
            # 行业股息率暴露 = Σ(行业权重 × 该行业在合格池里的平均 dy)
            # 越大 = 组合越集中在"高股息行业" → 这是行业 beta 暴露的直接度量
            if len(w):
                rec[f"{tag}·行业dy暴露"] = float(
                    (w * pool_ind_dy.reindex(w.index)).sum())
            # hyst 候选池大小（{dy>=entry} ∪ {held & dy>=exit} 近似：只算 dy>=entry 的部分）
            if he:
                rec[f"{tag}·dy≥{he:g}数"] = int((dy_pool >= he).sum())
        # 重叠度
        rec["J·indpct_vs_topn"] = jaccard(plans["行业内百分位"].get(t, []),
                                          plans["原始 topn"].get(t, []))
        rec["J·hyst_vs_topn"] = jaccard(plans["hyst 8/4"].get(t, []),
                                        plans["原始 topn"].get(t, []))
        rec["J·叠加_vs_indpct"] = jaccard(plans["hyst+行业内"].get(t, []),
                                          plans["行业内百分位"].get(t, []))
        rec["J·叠加_vs_hyst"] = jaccard(plans["hyst+行业内"].get(t, []),
                                        plans["hyst 8/4"].get(t, []))
        rec["J·hyst_vs_indpct"] = jaccard(plans["hyst 8/4"].get(t, []),
                                          plans["行业内百分位"].get(t, []))
        rows.append(rec)
    r = pd.DataFrame(rows)
    r.to_csv(args.out, index=False, encoding="utf-8-sig")

    # ---------------- 2) 汇总表 ----------------
    for ptag, s, e in PERIODS:
        sub = r[(r.date >= s) & (r.date <= e)]
        if sub.empty:
            continue
        print("\n" + "=" * 100)
        print(f"  {ptag}（{s}~{e}，{len(sub)} 个调仓日，N={args.topn} hold={args.hold}）")
        print("=" * 100)
        print("  " + "{:<16}{:>9}{:>9}{:>9}{:>10}{:>8}{:>9}{:>10}".format(
            "构造", "dy均值%", "dy中位%", "dy最低%", "dy<8%占比", "行业数", "行业HHI",
            "行业dy暴露"))
        print("  " + "-" * 84)
        for tag, _, _, _ in CONSTRUCTS:
            print("  " + "{:<16}{:>9.2f}{:>9.2f}{:>9.2f}{:>10.1f}{:>8.1f}{:>9.3f}{:>10.2f}".format(
                tag, sub[f"{tag}·dy均值"].mean(), sub[f"{tag}·dy中位"].median(),
                sub[f"{tag}·dy最低"].mean(), sub[f"{tag}·dy<8占比"].mean(),
                sub[f"{tag}·行业数"].mean(), sub[f"{tag}·行业HHI"].mean(),
                sub[f"{tag}·行业dy暴露"].mean()))
        print(f"\n  合格池（过滤后候选数）中位 {sub['合格池'].median():.0f}"
              f" ｜ 其中 dy≥8% 的股票数中位 {sub['hyst 8/4·dy≥8数'].median():.0f}"
              f" ｜ 池内各行业 dy 中位 {sub['池·行业dy中位'].mean():.2f}%")
        print("\n  ── 选股重叠度（Jaccard，1=完全相同）──")
        for k, lbl in [("J·hyst_vs_indpct", "hyst  vs 行业内"),
                       ("J·indpct_vs_topn", "行业内 vs 原始 topn"),
                       ("J·hyst_vs_topn", "hyst  vs 原始 topn"),
                       ("J·叠加_vs_indpct", "叠加  vs 行业内"),
                       ("J·叠加_vs_hyst", "叠加  vs hyst")]:
            print(f"    {lbl:<22} {sub[k].mean():.3f}   （中位 {sub[k].median():.3f}）")

    # ---------------- 3) H1：indpct 是否等价于「某个门槛的 topn」 ----------------
    print("\n" + "=" * 100)
    print("  H1 检验：行业内百分位 ≈ 提高股息率门槛的 topn 吗？")
    print("=" * 100)
    print("  对每个 min_dy 跑一遍纯 topn，看与「行业内百分位」的选股重叠度：")
    print("  " + "{:<12}{:>14}{:>14}{:>14}".format("min_dy%", "全区间J", "样本内J", "样本外J"))
    print("  " + "-" * 54)
    indpct_plan = plans["行业内百分位"]
    rows_h1 = []
    for mdy in MIN_DY_GRID:
        pl = plan_selections(df, dates, by_date, mode="topn", topn=args.topn,
                             hold=args.hold, dy_col=args.dy_col, min_dy=mdy,
                             max_dy=args.max_dy, min_div3=args.min_div3)
        js = {}
        for ptag, s, e in PERIODS:
            v = [jaccard(indpct_plan.get(t, []), pl.get(t, []))
                 for t in sorted(indpct_plan) if s <= t <= e]
            js[ptag] = float(np.nanmean(v)) if v else np.nan
        rows_h1.append((mdy, js["全区间"], js["样本内 2019~2022"], js["样本外 2023~2026"]))
    # 找出第一个"开始 binding"的门槛（重叠度与低门槛不同）
    base = rows_h1[0][1]
    for mdy, j_all, j_in, j_out in rows_h1:
        tag = "  ← 开始 binding" if abs(j_all - base) > 1e-9 and mdy == min(
            m for m, a, _, _ in rows_h1 if abs(a - base) > 1e-9) else ""
        print("  " + "{:<12.1f}{:>14.3f}{:>14.3f}{:>14.3f}".format(
            mdy, j_all, j_in, j_out) + tag)
    print("\n  判读：")
    print("    · 若「行业内百分位」与**低门槛** topn 的重叠度高（>0.6）→ 它**不是**靠提高门槛起作用。")
    print("    · 若与某个**高门槛** topn 重叠度高 → H1 成立（共享机制 = 隐含门槛）。")
    print("    · ⚠️ 若 `min_dy` 从 0.5 到 7.0 的重叠度**完全相同** → 说明这些门槛值**都不 binding**")
    print("      （另一个「参数没生效」实例：合格池里的前 20 名 dy 本来就都高于 7%）。")

    print(f"\n  结果已写出 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
