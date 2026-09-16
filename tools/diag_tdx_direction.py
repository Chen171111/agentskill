"""诊断：社区选股策略是「追涨」还是「抄底」？

为什么需要它
------------
`tools/sweep_tdx.py` 跑出 15 条社区策略**全部大幅跑输**（年化 −5% ~ −36%，
基准 +15.61%）。需要判断失败是**结构性的**还是参数问题。

本报告第 2 节已实测：A 股 2018~2026 是**中期反转市**，20/60/120 日动量 IC
**全为负**（−0.03~−0.05），分组严格单调递减。若社区策略集中出现在
「过去涨得多」的股票上，那它们就是**追涨型**，在这段市场里天然吃亏。

做法
----
对每条策略，计算**信号触发当日**该股票的过去 20/60 日收益**横截面分位**
（0.5 = 市场中位）：
- 显著 > 0.5 → 追涨型（买已经涨过的）
- 显著 < 0.5 → 抄底型（买已经跌过的）

用法
----
    $PY tools/diag_tdx_direction.py --bars data/stockbars/bars_all.parquet
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.tdx_strategies import STRATEGIES  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True)
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260911")
    ap.add_argument("--out", default="results/tdx_direction.csv")
    args = ap.parse_args(argv)

    bars = pd.read_parquet(args.bars)
    bars["date"] = bars.date.astype(str).str.replace("-", "", regex=False)
    bars = bars.sort_values(["code", "date"]).reset_index(drop=True)
    g = bars.groupby("code", sort=False)
    bars["ret20"] = g.close.transform(lambda s: s / s.shift(20) - 1)
    bars["ret60"] = g.close.transform(lambda s: s / s.shift(60) - 1)
    bars["r20"] = bars.groupby("date")["ret20"].rank(pct=True)
    bars["r60"] = bars.groupby("date")["ret60"].rank(pct=True)

    d = bars[(bars.date >= args.start) & (bars.date <= args.end)]
    print(f"区间 {args.start}~{args.end}  {len(d):,} 行, {d.code.nunique()} 只\n")

    print("=" * 92)
    print("  {:<22} {:<8} {:>10} {:>10} {:>12} {:>10}".format(
        "策略", "类别", "过去20日分位", "过去60日分位", "信号数", "判定"))
    print("=" * 92)

    rows = []
    for spec in STRATEGIES:
        try:
            sig = spec["fn"](bars).fillna(False)
        except Exception as e:
            print(f"  {spec['name']:<22} 失败: {type(e).__name__}", flush=True)
            continue
        sel = d[sig.loc[d.index].values]
        if len(sel) < 50:
            print(f"  {spec['name']:<22} {spec['cat']:<8} 信号过少（{len(sel)}）")
            continue
        r20, r60 = sel.r20.mean(), sel.r60.mean()
        # 判定阈值：偏离中位 0.03 以上才算有方向
        if r20 > 0.53:
            verdict = "追涨型"
        elif r20 < 0.47:
            verdict = "抄底型"
        else:
            verdict = "中性"
        print("  {:<22} {:<8} {:>10.4f} {:>10.4f} {:>12,} {:>10}".format(
            spec["name"], spec["cat"], r20, r60, len(sel), verdict), flush=True)
        rows.append({"策略": spec["name"], "类别": spec["cat"],
                     "过去20日分位": r20, "过去60日分位": r60,
                     "信号数": len(sel), "判定": verdict})

    print("=" * 92)
    if rows:
        r = pd.DataFrame(rows)
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        r.to_csv(args.out, index=False, encoding="utf-8-sig")
        print(f"\n结果已写出 {args.out}")
        n_chase = int((r.判定 == "追涨型").sum())
        n_dip = int((r.判定 == "抄底型").sum())
        n_mid = int((r.判定 == "中性").sum())
        print(f"\n合计：追涨型 {n_chase} 条 / 抄底型 {n_dip} 条 / 中性 {n_mid} 条")
        print(f"全体信号的平均「过去20日分位」：{r.过去20日分位.mean():.4f}"
              f"（0.5 = 市场中位）")
        print("\n判读：")
        print("  · 若整体 > 0.5，说明社区策略以**追涨**为主。")
        print("    A 股 2018~2026 是反转市（动量 IC 全负），追涨型天然吃亏——")
        print("    这解释了为什么 15 条策略在 hold=5 下全部大幅跑输。")
        print("  · 若整体 < 0.5，则失败原因另有其他（如换手成本、信号过于宽泛）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
