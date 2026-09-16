"""个股多因子：风格中性化检验（因子是真 alpha，还是在裸赌小盘？）

为什么需要它
------------
报告「局限 6」写着：行业/风格中性化未做，因子倾斜可能隐含行业暴露
（如 `illiq20` 偏小盘、`vol20` 偏低 beta），但缺行业分类数据。

本脚本绕开"必须有行业标签"这个前提，用**风格代理**做中性化：
以 `log(20 日均成交额)` 作为市值/流动性的代理（成交额与市值高度相关，
且**历史全可得**，不依赖外部数据、无前视）。

对每个因子逐日做横截面 OLS：`因子 ~ log(amt_ma20)`，取**残差**作为中性化因子。
残差剔除了"因子值高只是因为这只股票盘子小"的那部分。

判据
----
- 中性化后超额**大幅缩水** → 因子本质上在赚小盘/流动性的钱，不是真 alpha
- 中性化后超额**基本保持** → 是独立于风格的真 alpha

用法
----
    $PY tools/sweep_neutral.py --bars data/stockbars/bars_all.parquet \
        --universe data/stockbars/universe_all.csv
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import build_features, run, metrics  # noqa: E402

ALL8 = ["rev20", "rev60", "rev120", "rev5", "vol20", "max20", "turn20", "illiq20"]
TILT_MIN = 0.45


def xs_residual(df: pd.DataFrame, ycol: str, xcol: str,
                datecol: str = "date") -> pd.Series:
    """逐日横截面 OLS 残差：y ~ 1 + x，按日分组向量化。

    等价于对每个交易日单独跑一次一元回归并取残差，但不循环、不 apply。
    """
    d = df[datecol]
    x = df[xcol]
    y = df[ycol]
    xm = x - x.groupby(d).transform("mean")
    ym = y - y.groupby(d).transform("mean")
    sxx = (xm * xm).groupby(d).transform("sum")
    sxy = (xm * ym).groupby(d).transform("sum")
    b = sxy / sxx.replace(0.0, np.nan)
    b = b.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return ym - b * xm


def add_neutralized(df: pd.DataFrame, factors: list[str],
                    proxy: str = "log_amt") -> list[str]:
    """给每个因子加一份中性化版本（列名 + `_n`），返回新列名。"""
    df[proxy] = np.log(df.amt_ma20.replace(0, np.nan))
    # 用横截面百分位做回归：因子量纲差异极大（illiq20 是 1e-8 级），
    # 直接对原值回归会有数值问题；rank 之后再回归更稳
    df["_x"] = df.groupby("date")[proxy].rank(pct=True)
    cols = []
    for f in factors:
        rk = f + "_rk"
        df[rk] = df.groupby("date")[f].rank(pct=True)
        nc = f + "_n"
        df[nc] = xs_residual(df, rk, "_x")
        cols.append(nc)
    return cols


def bench_metrics(df, start, end, min_listed=120, min_amount=3e7, min_price=2.0):
    rows = []
    for dt, g in df[(df.date >= start) & (df.date <= end)].groupby("date"):
        u = ((g.listed >= min_listed) & (g.amt_ma20 >= min_amount)
             & (g.close >= min_price) & (~g.suspended))
        rows.append((dt, float(g.loc[u, "ret1"].mean())))
    b = pd.DataFrame(rows, columns=["date", "r"]).set_index("date").dropna()
    return metrics((1 + b.r).cumprod())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260911")
    args = ap.parse_args(argv)

    bars = pd.read_parquet(args.bars)
    bars["date"] = bars.date.astype(str).str.replace("-", "", regex=False)
    if args.universe and os.path.exists(args.universe):
        u = pd.read_csv(args.universe, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
        bars = bars[~bars.code.isin(st)]
    print(f"日线: {len(bars):,} 行, {bars.code.nunique()} 只", flush=True)
    print("计算因子…", flush=True)
    df = build_features(bars)
    df = df[(df.date >= args.start) & (df.date <= args.end)].copy()
    print(f"回测区间数据: {len(df):,} 行\n", flush=True)

    print("对 log(20日均成交额) 做横截面中性化…", flush=True)
    ncols = add_neutralized(df, ALL8)
    print(f"  中性化因子: {', '.join(ncols)}\n", flush=True)

    bench = bench_metrics(df, args.start, args.end)
    print(f"基准 等权全池  年化 {bench['年化收益']:+.2f}%  "
          f"夏普 {bench['夏普比率']:.2f}  回撤 {bench['最大回撤']:.2f}%\n", flush=True)

    variants = [
        ("原始因子 tilt(>0.45)", [(f, 1.0) for f in ALL8]),
        ("中性化因子 tilt(>0.45)", [(f, 1.0) for f in ncols]),
    ]

    print("=" * 118)
    print("  {:<26} {:>8} {:>8} {:>7} {:>9} {:>6} {:>8} {:>10} {:>9}".format(
        "配置", "年化%", "波动%", "夏普", "回撤%", "卡玛", "持仓数",
        "交易笔数", "超额pp"))
    print("=" * 118)

    base = None
    for tag, facs in variants:
        eq, tr, meta = run(df, facs, start=args.start, end=args.end, topk=50,
                           hold=5, weight_mode="tilt", tilt_min=TILT_MIN)
        m = metrics(eq.equity)
        excess = m.get("年化收益", 0) - bench.get("年化收益", 0)
        print("  {:<26} {:>8.2f} {:>8.2f} {:>7.2f} {:>9.2f} {:>6.2f} {:>8.0f} "
              "{:>10,} {:>9.2f}".format(
                  tag, m.get("年化收益", 0), m.get("年化波动", 0),
                  m.get("夏普比率", 0), m.get("最大回撤", 0), m.get("卡玛比率", 0),
                  meta.get("avg_hold", 0), len(tr), excess), flush=True)
        if base is None:
            base = m

    print("=" * 118)
    if base:
        print(f"\n基线（原始因子）年化 {base['年化收益']:.2f}%  "
              f"夏普 {base['夏普比率']:.2f}  回撤 {base['最大回撤']:.2f}%")
        print("中性化后的变化：")
        print("  —— 若超额大幅缩水，说明因子主要在小盘/流动性上赚钱；")
        print("  —— 若基本保持，说明是独立于风格的真 alpha。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
