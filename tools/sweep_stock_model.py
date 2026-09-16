"""个股多因子模型：变体扫描 + 多空价差诊断。

为什么需要它
------------
首轮回测出现「因子 IC 全为正、组合却跑输等权基准」的矛盾。可能原因：
1. `illiq20`（非流动性因子）挑流动性差的股票 → 抬高波动、且难交易；
2. TopK=50 太集中 → 特质风险大；
3. 因子 IC 虽正但幅度（+0.05）小于横截面离散度，且**等权全池**在 A 股
   是极强基准（等权天然超配小盘、且含再平衡溢价）。

本脚本一次加载数据，跑多个变体，并额外给出**多空价差**（顶档−底档）——
后者剥离基准选择的影响，直接检验因子是否有效。

用法
----
    PY=.../python.exe
    $PY tools/sweep_stock_model.py --bars data/stockbars/bars.parquet \
        --universe data/stockbars/universe.csv
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import build_features, run, metrics, fmt  # noqa: E402

ALL8 = [("rev20", 1.0), ("rev60", 1.0), ("rev120", 1.0), ("rev5", 1.0),
        ("vol20", 1.0), ("max20", 1.0), ("turn20", 1.0), ("illiq20", 1.0)]
NO_ILLIQ = [f for f in ALL8 if f[0] != "illiq20"]
STRONG4 = [("rev20", 1.0), ("turn20", 1.0), ("max20", 1.0), ("vol20", 1.0)]
NO_REV120 = [f for f in ALL8 if f[0] != "rev120"]


def long_short_spread(df, factors, *, start, end, hold=5, q=0.2,
                      min_listed=120, min_amount=3e7, min_price=2.0):
    """顶档(q) − 底档(q) 的组合收益（每日再平衡，不含成本）。"""
    d = df[(df.date >= start) & (df.date <= end)].copy()
    uni = ((d.listed >= min_listed) & (d.amt_ma20 >= min_amount)
           & (d.close >= min_price) & (~d.suspended))
    d = d[uni]
    d = d.sort_values(["code", "date"])
    d["fwd1"] = d.groupby("code", sort=False).close.shift(-1) / d.close - 1
    d = d.dropna(subset=[f for f, _ in factors] + ["fwd1"])
    tot = sum(w for _, w in factors)
    rows = []
    for dt, g in d.groupby("date"):
        if len(g) < 200:
            continue
        sc = None
        for f, w in factors:
            r = g[f].rank(pct=True) * (w / tot)
            sc = r if sc is None else sc + r
        g = g.assign(_s=sc)
        n = max(int(len(g) * q), 10)
        top = g.nlargest(n, "_s").fwd1.mean()
        bot = g.nsmallest(n, "_s").fwd1.mean()
        rows.append((dt, top, bot, top - bot))
    r = pd.DataFrame(rows, columns=["date", "top", "bot", "ls"]).set_index("date")
    return r


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
    print(f"日线: {len(bars):,} 行, {bars.code.nunique()} 只, "
          f"{bars.date.nunique()} 交易日")
    print("计算因子…", flush=True)
    df = build_features(bars)

    # ---------------- 1) 多空价差诊断 ----------------
    print(f"\n=== 1. 多空价差（顶档−底档 20%，每日，{args.start}~{args.end}）===")
    print("  说明：剥离基准选择，直接看因子是否有效。年化为简单折算。")
    for tag, facs in [("全8因子", ALL8), ("去掉 illiq20", NO_ILLIQ),
                      ("强4因子", STRONG4)]:
        r = long_short_spread(df, facs, start=args.start, end=args.end)
        ann = r.ls.mean() * 244 * 100
        t = r.ls.mean() / r.ls.std() * np.sqrt(len(r))
        print(f"  {tag:<14} 日均价差 {r.ls.mean()*100:+.4f}%  年化 {ann:+.2f}%  "
              f"t={t:+.1f}  (n日={len(r)})")

    # ---------------- 2) 变体扫描 ----------------
    print(f"\n=== 2. 组合变体扫描（topk/hold 可变）===")
    print("=" * 108)
    variants = [
        ("全8因子 topk200 等权", ALL8, 200, 5, False, "equal"),
        ("全8因子 topk200 排名加权", ALL8, 200, 5, False, "rank"),
        ("全8因子 topk300 排名加权", ALL8, 300, 5, False, "rank"),
        ("全8因子 topk500 排名加权", ALL8, 500, 5, False, "rank"),
        ("全8因子 tilt(>0.55)", ALL8, 50, 5, False, "tilt"),
        ("全8因子 tilt(>0.45)", ALL8, 50, 5, False, "tilt2"),
        ("全8因子 tilt + 趋势过滤", ALL8, 50, 5, True, "tilt"),
        ("去掉 illiq20 tilt", NO_ILLIQ, 50, 5, False, "tilt"),
        ("去掉 rev120 tilt", NO_REV120, 50, 5, False, "tilt"),
    ]
    bench = None
    for tag, facs, topk, hold, trend, wmode in variants:
        tm = 0.45 if wmode == "tilt2" else 0.55
        wm = "tilt" if wmode == "tilt2" else wmode
        try:
            eq, tr, meta = run(df, facs, start=args.start, end=args.end, topk=topk,
                               hold=hold, trend_filter=trend, weight_mode=wm,
                               tilt_min=tm)
        except Exception as e:
            print(f"  {tag:<26} 失败: {type(e).__name__}: {e}")
            continue
        m = metrics(eq.equity)
        if bench is None:
            b = []
            dd = df[(df.date >= args.start) & (df.date <= args.end)]
            for dt, g in dd.groupby("date"):
                u = ((g.listed >= 120) & (g.amt_ma20 >= 3e7)
                     & (g.close >= 2.0) & (~g.suspended))
                b.append((dt, float(g.loc[u, "ret1"].mean())))
            beq = (1 + pd.DataFrame(b, columns=["d", "r"]).set_index("d").r).cumprod()
            bench = metrics(beq)
            print(fmt("等权全池基准", bench))
        print(fmt(tag, m))
    print("=" * 108)
    print("  超额基准: 年化 {:+.2f}pp  夏普 {:+.2f}".format(
        bench.get("年化收益", 0), bench.get("夏普比率", 0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
