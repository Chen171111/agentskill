"""策略 vs 可投资指数：等权全池是不是一个过强的基准？

动机
----
此前所有对比都用**等权全池**（5000 只等权、每周再平衡）做基准。
但那个基准有两个问题：

1. **不可投资** —— 持有 5000 只、每周全量再平衡，实盘需要 3700 万级资金
   （见 `tools/sweep_feasibility.py`）；
2. **自带异象收益** —— 等权组合在 A 股有已知的"小盘 beta + 再平衡溢价"，
   年化 15.61%、夏普 0.70，本身就是一个很强的基准。

拿它当尺子，可能低估了策略的真实水平。本脚本改用**可投资指数**做对照。

用法
----
    $PY tools/diag_vs_index.py --indexes data/indexes/indexes_all.parquet
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import metrics  # noqa: E402

MODELS = [
    ("results/stock_tilt_all.parquet", "个股模型 tilt(>0.45)"),
    ("results/stock_tilt_b10.parquet", "个股模型 buffer=0.10"),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--indexes", default="data/indexes/indexes_all.parquet")
    ap.add_argument("--out", default="results/vs_index.csv")
    args = ap.parse_args(argv)

    ix = pd.read_parquet(args.indexes)
    ix["date"] = ix.date.astype(str).str.replace("-", "", regex=False)
    names = ix.drop_duplicates("code").set_index("code")["name"].to_dict()

    # 以第一个模型的时间轴为基准
    m0 = pd.read_parquet(MODELS[0][0])
    m0.index = m0.index.astype(str).str.replace("-", "", regex=False)
    m0 = m0.sort_index()
    idx = m0.index
    start, end = idx.min(), idx.max()
    print(f"对齐区间：{start} ~ {end}（{len(idx)} 个交易日）\n")

    rows = []

    def add(tag, kind, eq):
        m = metrics(eq)
        rows.append({"标的": tag, "类型": kind, "年化收益": m.get("年化收益", 0),
                     "年化波动": m.get("年化波动", 0), "夏普比率": m.get("夏普比率", 0),
                     "最大回撤": m.get("最大回撤", 0), "卡玛比率": m.get("卡玛比率", 0)})

    # 指数（收盘价 -> 净值）
    for sym in ix.code.unique():
        px = (ix[ix.code == sym].set_index("date").close
              .sort_index().reindex(idx).ffill())
        px = px.dropna()
        if len(px) < 100:
            continue
        add(f"{names.get(sym, sym)}", "指数", px / px.iloc[0])

    # 等权全池基准（来自模型净值文件里的 bench 列）
    bench = m0["bench"].dropna()
    add("等权全池（不可投资）", "自制基准", bench / bench.iloc[0])

    # 模型
    for path, tag in MODELS:
        mm = pd.read_parquet(path)
        mm.index = mm.index.astype(str).str.replace("-", "", regex=False)
        eq = mm.equity.sort_index().reindex(idx).dropna()
        add(tag, "策略", eq / eq.iloc[0])

    r = pd.DataFrame(rows)
    print("=" * 100)
    print("  {:<26} {:<10} {:>9} {:>8} {:>7} {:>10} {:>7}".format(
        "标的", "类型", "年化%", "波动%", "夏普", "回撤%", "卡玛"))
    print("=" * 100)
    for _, x in r.iterrows():
        print("  {:<26} {:<10} {:>9.2f} {:>8.2f} {:>7.2f} {:>10.2f} {:>7.2f}".format(
            x.标的, x.类型, x.年化收益, x.年化波动, x.夏普比率, x.最大回撤, x.卡玛比率))
    print("=" * 100)

    # 策略 vs 各指数的超额
    print("\n策略相对各基准的超额（年化 pp）：")
    base = r[r.类型 != "策略"]
    strat = r[r.类型 == "策略"]
    print("  {:<26} {:>14} {:>14}".format("基准", *[s.标的[:10] for _, s in strat.iterrows()]))
    for _, b in base.iterrows():
        line = "  {:<26}".format(b.标的)
        for _, s in strat.iterrows():
            line += "{:>14.2f}".format(s.年化收益 - b.年化收益)
        print(line)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    r.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"\n结果已写出 {args.out}")
    print("\n判读：若策略能跑赢可投资指数（沪深300/中证1000 等），")
    print("      说明此前「跑不赢基准」的结论主要来自「等权全池过强」，而非策略本身弱。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
