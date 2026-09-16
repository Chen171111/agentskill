"""个股因子逐日横截面 IC 检验（向量化，可在 5000 只票池上跑）。

做法
----
逐日横截面 Spearman IC 用「秩的 Pearson」计算，并**完全向量化**：
先把因子与未来收益在**日内**转成秩，再对 `n, Σx, Σy, Σx², Σy², Σxy` 做
groupby 求和，最后套 Pearson 公式。避免逐日 `apply`（2100 天 × 5000 只会极慢）。

输出
----
- 每个因子的：均值 IC、t 值、IC>0 胜率、覆盖天数
- 分年度 IC（看符号一致性）
- 分 5 组的分组收益（看单调性）

用法
----
    PY=.../python.exe
    $PY tools/test_stock_factors.py --bars data/stockbars/bars.parquet \
        --universe data/stockbars/universe.csv --horizons 1 5
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import build_features, attach_bigorder  # noqa: E402

BASE_FACTORS = ["rev20", "rev60", "rev120", "rev5",
                "vol20", "vol60", "max20", "turn20", "illiq20"]
BIGORDER_FACTORS = ["bo_n_rec", "bo_flow_ratio", "bo_tail_share", "bo_am_net"]


def daily_ic_vectorized(d: pd.DataFrame, fcol: str, ycol: str) -> pd.Series:
    """逐日横截面 Spearman IC（向量化）。"""
    s = d[[fcol, ycol, "date"]].dropna()
    if s.empty:
        return pd.Series(dtype=float)
    s = s.assign(rx=s.groupby("date")[fcol].rank(),
                 ry=s.groupby("date")[ycol].rank())
    s = s.assign(rx2=s.rx ** 2, ry2=s.ry ** 2, rxy=s.rx * s.ry)
    g = s.groupby("date").agg(n=("rx", "size"), sx=("rx", "sum"), sy=("ry", "sum"),
                              sx2=("rx2", "sum"), sy2=("ry2", "sum"),
                              sxy=("rxy", "sum"))
    g = g[g.n >= 30]
    num = g.n * g.sxy - g.sx * g.sy
    den = np.sqrt((g.n * g.sx2 - g.sx ** 2) * (g.n * g.sy2 - g.sy ** 2))
    ic = (num / den).replace([np.inf, -np.inf], np.nan).dropna()
    return ic


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="个股因子 IC 检验")
    ap.add_argument("--bars", required=True)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--bigorder", default=None)
    ap.add_argument("--horizons", type=int, nargs="*", default=[1, 5])
    ap.add_argument("--min-amount", type=float, default=3e7)
    ap.add_argument("--min-listed", type=int, default=120)
    ap.add_argument("--min-price", type=float, default=2.0)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    args = ap.parse_args(argv)

    bars = pd.read_parquet(args.bars)
    bars["date"] = bars.date.astype(str).str.replace("-", "", regex=False)
    if args.universe and os.path.exists(args.universe):
        u = pd.read_csv(args.universe, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
        bars = bars[~bars.code.isin(st)]
    print(f"日线: {len(bars):,} 行, {bars.code.nunique()} 只, "
          f"{bars.date.nunique()} 交易日 ({bars.date.min()} ~ {bars.date.max()})")

    print("计算因子…", flush=True)
    df = build_features(bars)
    factors = list(BASE_FACTORS)
    if args.bigorder and os.path.exists(args.bigorder):
        df = attach_bigorder(df, args.bigorder)
        got = [c for c in BIGORDER_FACTORS if c in df.columns]
        factors += got
        print(f"  并入精灵大单因子: {got}")

    # 逐日候选池
    df = df[(df.listed >= args.min_listed) & (df.amt_ma20 >= args.min_amount)
            & (df.close >= args.min_price) & (~df.suspended)]
    if args.start:
        df = df[df.date >= args.start]
    if args.end:
        df = df[df.date <= args.end]
    print(f"过滤后: {len(df):,} 行, {df.code.nunique()} 只, "
          f"{df.date.nunique()} 交易日\n")

    # 未来收益
    df = df.sort_values(["code", "date"])
    g = df.groupby("code", sort=False)
    for h in args.horizons:
        df[f"fwd{h}"] = g.close.shift(-h) / df.close - 1

    print("=== 逐日横截面 IC（Spearman，秩 Pearson 向量化）===")
    hdr = "  {:<14}".format("因子")
    for h in args.horizons:
        hdr += "{:>26}".format(f"h={h}")
    print(hdr)
    store = {}
    for f in factors:
        if f not in df.columns:
            continue
        line = "  {:<14}".format(f)
        for h in args.horizons:
            ic = daily_ic_vectorized(df, f, f"fwd{h}")
            store[(f, h)] = ic
            if len(ic) < 20:
                line += "{:>26}".format("样本不足")
                continue
            t = ic.mean() / ic.std() * len(ic) ** 0.5
            line += "{:>26}".format(f"{ic.mean():+.4f} (t={t:+.1f})")
        print(line)

    print("\n=== 分年度 IC（h=1 / h=5，看符号一致性）===")
    print("  {:<14}{:>10}{:>10}{:>10}{:>10}{:>10}{:>10}".format(
        "因子", "22-h1", "23-h1", "24-h1", "25-h1", "26-h1", "22~26-h5"))
    for f in factors:
        if f not in df.columns:
            continue
        cells = []
        for yr in ["2022", "2023", "2024", "2025", "2026"]:
            sub = df[df.date.str[:4] == yr]
            ic = daily_ic_vectorized(sub, f, "fwd1")
            cells.append(f"{ic.mean():+.4f}" if len(ic) >= 10 else "n/a")
        ic5 = store.get((f, 5))
        cells.append(f"{ic5.mean():+.4f}" if ic5 is not None and len(ic5) else "n/a")
        print("  {:<14}{:>10}{:>10}{:>10}{:>10}{:>10}{:>10}".format(f, *cells))

    print("\n=== 分组单调性（h=5，按因子分 5 组的平均收益 %）===")
    if 5 in args.horizons:
        for f in factors:
            if f not in df.columns:
                continue
            s = df[["date", f, "fwd5"]].dropna()
            if s.empty:
                continue

            def _q(x):
                try:
                    return pd.qcut(x, 5, labels=False, duplicates="drop")
                except ValueError:
                    return pd.Series(np.nan, index=x.index)

            s = s.assign(grp=s.groupby("date")[f].transform(_q))
            m = s.dropna(subset=["grp"]).groupby("grp").fwd5.mean() * 100
            if len(m) == 5:
                print("  {:<14} ".format(f) + "  ".join(f"{v:+.3f}" for v in m.values))
    return 0


if __name__ == "__main__":
    sys.exit(main())
