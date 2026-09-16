"""能否救回 topk300？针对 `illiq20` 的截断 / 剔除实验。

动机
----
`tools/diag_delisted_score.py` 诊断出：退市股在退市前的因子分显著偏高，
且**主要来自 `illiq20`**（非流动性因子分位 0.8042，远高于中位 0.5）。
流动性枯竭正是退市股的画像，而 illiq20 会给它们打高分。

tilt 分散到 2000+ 只 → 被稀释；topk300 精选前 300 名 → 正好把 illiq 最高的
那批（含大量退市股）挑进去。这解释了修正生存者偏差后 topk 系列为何大跌
（topk300 13.90% → 9.64%）。

三种处理方式
------------
1. 不处理（基线）
2. **剔除** `illiq20`
3. **截断**（winsorize）：把 illiq20 的横截面分位压到 0.95 / 0.90 上限，
   保住它的选股能力，但让它不再把"极端枯竭"的股票推到最高档

用法
----
    $PY tools/sweep_illiq_cap.py --bars data/stockbars/bars_all.parquet \
        --universe data/stockbars/universe_all.csv
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import build_features, run, metrics  # noqa: E402

BASE = ["rev20", "rev60", "rev120", "rev5", "vol20", "max20", "turn20"]
TILT_MIN = 0.45


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
    ap.add_argument("--out", default="results/illiq_cap.csv")
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

    pct = df.groupby("date")["illiq20"].rank(pct=True)
    df["illiq20_c95"] = pct.clip(upper=0.95)
    df["illiq20_c90"] = pct.clip(upper=0.90)
    print("已生成 illiq20 截断列（分位上限 0.95 / 0.90）\n", flush=True)

    bench = bench_metrics(df, args.start, args.end)
    print(f"基准 等权全池  年化 {bench['年化收益']:+.2f}%  "
          f"夏普 {bench['夏普比率']:.2f}  回撤 {bench['最大回撤']:.2f}%\n", flush=True)

    full = [(f, 1.0) for f in BASE] + [("illiq20", 1.0)]
    noil = [(f, 1.0) for f in BASE]
    c95 = [(f, 1.0) for f in BASE] + [("illiq20_c95", 1.0)]
    c90 = [(f, 1.0) for f in BASE] + [("illiq20_c90", 1.0)]

    variants = [
        ("tilt  原始illiq", full, "tilt", 50),
        ("tilt  剔除illiq", noil, "tilt", 50),
        ("tilt  截断0.95", c95, "tilt", 50),
        ("tilt  截断0.90", c90, "tilt", 50),
        ("topk300 原始illiq", full, "rank", 300),
        ("topk300 剔除illiq", noil, "rank", 300),
        ("topk300 截断0.95", c95, "rank", 300),
        ("topk300 截断0.90", c90, "rank", 300),
    ]

    print("=" * 118)
    print("  {:<20} {:>8} {:>8} {:>7} {:>9} {:>6} {:>8} {:>10} {:>9}".format(
        "配置", "年化%", "波动%", "夏普", "回撤%", "卡玛", "持仓数",
        "交易笔数", "超额pp"))
    print("=" * 118)

    rows = []
    for tag, facs, wmode, topk in variants:
        try:
            eq, tr, meta = run(df, facs, start=args.start, end=args.end,
                               topk=topk, hold=5, weight_mode=wmode,
                               tilt_min=TILT_MIN)
        except Exception as e:
            print(f"  {tag}  失败: {type(e).__name__}: {e}", flush=True)
            continue
        m = metrics(eq.equity)
        excess = m.get("年化收益", 0) - bench.get("年化收益", 0)
        print("  {:<20} {:>8.2f} {:>8.2f} {:>7.2f} {:>9.2f} {:>6.2f} {:>8.0f} "
              "{:>10,} {:>9.2f}".format(
                  tag, m.get("年化收益", 0), m.get("年化波动", 0),
                  m.get("夏普比率", 0), m.get("最大回撤", 0), m.get("卡玛比率", 0),
                  meta.get("avg_hold", 0), len(tr), excess), flush=True)
        rows.append({"配置": tag, "年化收益": m.get("年化收益", 0),
                     "年化波动": m.get("年化波动", 0),
                     "夏普比率": m.get("夏普比率", 0),
                     "最大回撤": m.get("最大回撤", 0),
                     "卡玛比率": m.get("卡玛比率", 0),
                     "平均持仓": meta.get("avg_hold", 0),
                     "交易笔数": len(tr), "超额年化pp": excess})
        pd.DataFrame(rows).to_csv(args.out, index=False, encoding="utf-8-sig")

    print("=" * 118)
    print("\n判据：若「截断」能在**保住 tilt 表现**的同时**明显抬升 topk300**，")
    print("说明 illiq20 的极端值确实是 topk 踩雷的元凶，截断就是有效解药。")
    print(f"\n结果已写出 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
