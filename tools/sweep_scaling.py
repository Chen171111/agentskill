"""从小资金做起的落地路径：**资金 × 持仓数** 的最优配置矩阵。

动机
----
此前只算过 tilt(>0.45) 一个配置的门槛（3738 万），结论是"小资金做不了"。
但这是错的问法 —— 游资起步也不是大资金，是**逐步做起来**的。
正确的问题是：**给定资金，最优配置是什么？**

两个约束
--------
1. **最低佣金 5 元**：单笔金额 < 16,667 元时，实际费率被抬高到 `5 / 单笔金额`。
   持仓越少 → 单笔越大 → 这个惩罚越轻。所以**小资金应该用更少的持仓**。
2. **持仓数 vs 表现**：持仓越少，跟踪误差越大、回撤越深（已实测，单调下坡）。
   但小资金本来就用不了大持仓。

做法
----
- 跑 `topk ∈ {30,50,100,200,300,500,1000}` 排名加权
- 对每个配置 × 每个资金档，算**计入最低佣金后的真实年化**
- 基准用**可投资指数**（中证1000 / 国证2000），而非不可投资的等权全池

用法
----
    $PY tools/sweep_scaling.py --bars data/stockbars/bars_all.parquet \
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

ALL8 = [("rev20", 1.0), ("rev60", 1.0), ("rev120", 1.0), ("rev5", 1.0),
        ("vol20", 1.0), ("max20", 1.0), ("turn20", 1.0), ("illiq20", 1.0)]

TOPKS = (30, 50, 100, 200, 300, 500, 1000)
CAPITALS = (10, 20, 50, 100, 200, 300, 500, 1000, 3000)   # 万元

# 成本模型统一到 tools/costs.py（**单一来源**，勿在此重定义 —— 铁律 14）
from tools.costs import (MIN_COMMISSION, MODELED_ROUND, NOMINAL_FEE,  # noqa: E402
                         SLIP, STAMP)


def bench_index(path, sym, idx):
    ix = pd.read_parquet(path)
    ix["date"] = ix.date.astype(str).str.replace("-", "", regex=False)
    px = ix[ix.code == sym].set_index("date").close.sort_index()
    px = px.reindex(idx).ffill().dropna()
    return metrics(px / px.iloc[0]) if len(px) > 100 else {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260911")
    ap.add_argument("--indexes", default="data/indexes/indexes_all.parquet")
    ap.add_argument("--out", default="results/scaling.csv")
    ap.add_argument("--topks", default=None,
                    help="覆盖默认的 topk 网格，逗号分隔（用于邻域稳健性检查）")
    ap.add_argument("--hold", type=int, default=5,
                    help="调仓周期（交易日）。实测 hold=20 年化与换手都优于默认的 5")
    args = ap.parse_args(argv)

    topks = TOPKS
    if args.topks:
        topks = tuple(int(x) for x in args.topks.split(","))
        print(f"使用自定义 topk 网格: {topks}\n")

    bars = pd.read_parquet(args.bars)
    bars["date"] = bars.date.astype(str).str.replace("-", "", regex=False)
    if args.universe and os.path.exists(args.universe):
        u = pd.read_csv(args.universe, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
        bars = bars[~bars.code.isin(st)]
    print(f"日线: {len(bars):,} 行, {bars.code.nunique()} 只", flush=True)
    print("计算因子…", flush=True)
    df = build_features(bars)

    ndays = df[(df.date >= args.start) & (df.date <= args.end)].date.nunique()
    years = ndays / 244.0
    print(f"区间 {args.start}~{args.end}  {ndays} 交易日 ({years:.1f} 年)\n", flush=True)

    # ---------- 可投资指数基准 ----------
    idx = sorted(df[(df.date >= args.start) & (df.date <= args.end)].date.unique())
    benches = {}
    if os.path.exists(args.indexes):
        for sym, nm in (("SH000852", "中证1000"), ("SZ399303", "国证2000"),
                        ("SH000300", "沪深300")):
            m = bench_index(args.indexes, sym, idx)
            if m:
                benches[nm] = m
        print("可投资指数基准：")
        for nm, m in benches.items():
            print(f"  {nm:<8} 年化 {m['年化收益']:>7.2f}%  夏普 {m['夏普比率']:.2f}  "
                  f"回撤 {m['最大回撤']:>8.2f}%")
        print()

    # ---------- 持仓数网格 ----------
    rows = []
    print("=" * 108)
    print("  {:<8} {:>8} {:>8} {:>8} {:>9} {:>8} {:>10} {:>9}".format(
        "topk", "年化%", "波动%", "夏普", "回撤%", "持仓数", "年单边换手", "交易笔数"))
    print("=" * 108)
    for topk in topks:
        try:
            eq, tr, meta = run(df, ALL8, start=args.start, end=args.end, topk=topk,
                               hold=args.hold, weight_mode="rank")
        except Exception as e:
            print(f"  topk={topk} 失败: {type(e).__name__}: {e}", flush=True)
            continue
        m = metrics(eq.equity)
        n_hold = meta.get("avg_hold", 0.0) or 1.0
        n_buy = int((tr.side == "buy").sum()) if len(tr) else 0
        turnover = (n_buy / years) / n_hold
        print("  {:<8} {:>8.2f} {:>8.2f} {:>8.2f} {:>9.2f} {:>8.0f} {:>10.1f}x "
              "{:>9,}".format(topk, m.get("年化收益", 0), m.get("年化波动", 0),
                              m.get("夏普比率", 0), m.get("最大回撤", 0),
                              n_hold, turnover, len(tr)), flush=True)
        rows.append({"topk": topk, "年化收益": m.get("年化收益", 0),
                     "年化波动": m.get("年化波动", 0),
                     "夏普比率": m.get("夏普比率", 0),
                     "最大回撤": m.get("最大回撤", 0),
                     "平均持仓": n_hold, "年单边换手": turnover,
                     "交易笔数": len(tr)})
        pd.DataFrame(rows).to_csv(args.out, index=False, encoding="utf-8-sig")

    print("=" * 108)
    if not rows:
        return 1
    r = pd.DataFrame(rows)

    # ---------- 资金 × 配置 真实年化矩阵 ----------
    print("\n计入「最低佣金 5 元」后的**真实年化**（%，行=资金，列=topk）")
    print("  " + "{:<8}".format("资金") + "".join(f"{t:>9}" for t in r.topk))
    matrix = []
    for cap in CAPITALS:
        W = cap * 1e4
        line = "  {:<8}".format(f"{cap}万")
        best, best_v = None, -1e9
        for _, x in r.iterrows():
            ticket = W / max(x.平均持仓, 1.0)
            comm = max(NOMINAL_FEE, MIN_COMMISSION / ticket)
            real_round = (comm + SLIP) + (comm + STAMP + SLIP)
            extra = x.年单边换手 * (real_round - MODELED_ROUND) * 100
            real = x.年化收益 - extra
            line += f"{real:>9.2f}"
            if real > best_v:
                best_v, best = real, int(x.topk)
        print(line + f"   ← 最优 topk={best}（{best_v:.2f}%）")
        matrix.append({"资金万元": cap, "最优topk": best, "最优真实年化": best_v})
    pd.DataFrame(matrix).to_csv(args.out.replace(".csv", "_matrix.csv"),
                               index=False, encoding="utf-8-sig")

    # ---------- 每个资金档最优配置 vs 可投资指数 ----------
    print("\n各资金档最优配置 vs 可投资指数（超额 pp）")
    hdr = "  {:<8} {:<8} {:>10}".format("资金", "最优topk", "真实年化")
    for nm in benches:
        hdr += f"{nm:>12}"
    print(hdr)
    for row in matrix:
        line = "  {:<8} {:<8} {:>10.2f}".format(
            f"{row['资金万元']}万", row["最优topk"], row["最优真实年化"])
        for nm, m in benches.items():
            line += f"{row['最优真实年化'] - m['年化收益']:>12.2f}"
        print(line)
    print("\n  说明：真实年化 = 回测年化 − 最低佣金带来的额外成本；")
    print("        基准为可投资指数（非等权全池）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
