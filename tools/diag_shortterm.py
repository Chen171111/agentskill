"""诊断：短线引擎的亏损到底来自「引擎机制」还是「选股过滤」。

背景
----
`tools/sweep_shortterm.py` 发现：精心选股（-37.27%/年）与**随机选股**（-37.59%/年）
几乎一样差，且**零成本随机选股仍亏 26.99%/年**。
这说明亏损不是交易成本造成的，必须逐层剥离。

测试矩阵（全部：随机选股 + 零成本 + topk=5 + hold=5）
------------------------------------------------------
1. 无过滤          → 应 ≈ 市场平均（等权全池约 +10%/年）
2. 仅 lu_20>=1     → 检验「涨停基因」是否负 alpha
3. 仅 ma_bull      → 检验「均线多头」是否负 alpha
4. 两者都有        = Z4，已知 -26.99%
5. 无过滤 + hold=20 → 检验换手/持有期影响

判读
----
- 若 1 也远离市场平均 → **引擎机制有 bug**（成交价/记账/复权）
- 若 1 正常、2/3 差     → 是**过滤条件**选到了负 alpha 的股票（追高）

用法
----
    $PY tools/diag_shortterm.py --bars data/stockbars/bars_all.parquet
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import metrics  # noqa: E402
from tools.backtest_shortterm import build_short_features, run_short  # noqa: E402

ZERO = dict(commission=0.0, stamp=0.0, slippage=0.0, min_comm=0.0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260911")
    ap.add_argument("--topk", type=int, default=5)
    args = ap.parse_args(argv)

    bars = pd.read_parquet(args.bars)
    bars["date"] = bars.date.astype(str).str.replace("-", "", regex=False)
    if args.universe and os.path.exists(args.universe):
        u = pd.read_csv(args.universe, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
        bars = bars[~bars.code.isin(st)]

    print(f"日线 {len(bars):,} 行 / {bars.code.nunique()} 只", flush=True)
    print("计算因子…", flush=True)
    df = build_short_features(bars)
    rng = np.random.default_rng(7)
    df["rand"] = rng.random(len(df))

    # 基础过滤（无 lu / 无 ma_bull）
    base_no_filter = dict(min_amt=1e8, max_amt=3e9, min_lu=0, require_ma_bull=False)
    base_lu = dict(min_amt=1e8, max_amt=3e9, min_lu=1, require_ma_bull=False)
    base_ma = dict(min_amt=1e8, max_amt=3e9, min_lu=0, require_ma_bull=True)
    base_both = dict(min_amt=1e8, max_amt=3e9, min_lu=1, require_ma_bull=True)

    # 参照：等权全池（无过滤）
    bench = []
    for dt, g in df[(df.date >= args.start) & (df.date <= args.end)].groupby("date"):
        u = ((g.listed >= 120) & (g.close >= 2.0) & (~g.suspended)
             & (g.amt_ma20 >= 1e8) & (g.amt_ma20 <= 3e9))
        bench.append((dt, float(g.loc[u, "ret1"].mean())))
    b = pd.DataFrame(bench, columns=["date", "r"]).set_index("date").dropna()
    beq = (1 + b.r).cumprod() * 100_000
    bm = metrics(beq)

    print(f"\n区间 {args.start}~{args.end}  topk={args.topk}")
    print("=" * 118)
    print("  {:<28} {:>9} {:>8} {:>7} {:>9} {:>8} {:>10}".format(
        "配置(均随机选股/零成本)", "年化%", "波动%", "夏普", "回撤%", "持仓", "末净值"))
    print("=" * 118)
    print("  {:<28} {:>9.2f} {:>8.2f} {:>7.2f} {:>9.2f} {:>8} {:>10,.0f}".format(
        "★ 等权全池基准", bm.get("年化收益", 0), bm.get("年化波动", 0),
        bm.get("夏普比率", 0), bm.get("最大回撤", 0), "-", beq.iloc[-1]))

    cases = [
        ("1 无过滤", base_no_filter, 5),
        ("2 仅 20日涨停≥1", base_lu, 5),
        ("3 仅 均线多头", base_ma, 5),
        ("4 两者都有(复现Z4)", base_both, 5),
        ("5 无过滤·持20日", base_no_filter, 20),
        ("6 仅涨停·持20日", base_lu, 20),
    ]
    for name, filt, hold in cases:
        eq, tr, meta = run_short(df, start=args.start, end=args.end, topk=args.topk,
                                 hold=hold, score_col="rand", cash0=100_000.0,
                                 **dict(filt, **ZERO))
        m = metrics(eq.equity)
        print("  {:<28} {:>9.2f} {:>8.2f} {:>7.2f} {:>9.2f} {:>8.1f} {:>10,.0f}".format(
            name + ("" if hold == 5 else ""), m.get("年化收益", 0), m.get("年化波动", 0),
            m.get("夏普比率", 0), m.get("最大回撤", 0), meta["avg_hold"],
            float(eq.equity.iloc[-1])), flush=True)
    print("=" * 118)
    print("\n判读：若「1 无过滤」明显低于基准 → 引擎机制有问题；")
    print("      若「1」正常而「2/3」显著更差 → 是过滤条件选到了负 alpha 股票。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
