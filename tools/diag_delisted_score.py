"""诊断：退市股在崩盘前是不是拿了高因子分？

动机
----
修正生存者偏差后出现一个反常现象：topk 系列年化大幅下滑
（topk300 13.90% → 9.64%，−4.26pp），而 tilt 几乎没受影响
（13.58% → 12.71%，−0.87pp）。

怀疑的原因：**退市股在退市前恰好拿高分**。

- `rev20/60/120`（反转因子）= 过去 N 日涨幅取负 → **一路暴跌的股票分数极高**
- `illiq20`（非流动性溢价）= |ret|/成交额 → **流动性枯竭的股票分数极高**

而这两条正是退市股的画像。tilt 分散到 2000+ 只所以被稀释，
topk 集中选前 300 名则会把它们挑进去 → 集中踩雷。

本脚本直接验证：退市股在退市前 20/60/120 日的综合分分位，与全市场均值对比。

用法
----
    $PY tools/diag_delisted_score.py --bars data/stockbars/bars_all.parquet \
        --universe data/stockbars/universe_all.csv
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import build_features  # noqa: E402

FACTORS = ["rev20", "rev60", "rev120", "rev5", "vol20", "max20", "turn20",
           "illiq20"]


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

    # 与回测一致的股票池过滤
    uni = ((df.listed >= 120) & (df.amt_ma20 >= 3e7)
           & (df.close >= 2.0) & (~df.suspended))
    df = df[uni].dropna(subset=FACTORS)
    print(f"股票池内: {len(df):,} 行, {df.code.nunique()} 只", flush=True)

    # 综合分：8 因子横截面百分位等权（与 run() 一致）
    # 顺便留存每个因子的全市场分位，供后面分因子诊断使用
    sc = None
    for f in FACTORS:
        r = df.groupby("date")[f].rank(pct=True)
        df[f + "_pct"] = r
        sc = r if sc is None else sc + r
    df["score"] = sc / len(FACTORS)

    # 退市股 = 最后交易日早于全市场最新日
    dele = os.path.join(os.path.dirname(args.bars), "bars_delisted.parquet")
    d = pd.read_parquet(dele)
    d["date"] = d.date.astype(str).str.replace("-", "", regex=False)
    last = d.groupby("code").date.max()
    latest = df.date.max()
    dead = set(last[last < latest].index)
    print(f"已退市股: {len(dead)} 只（最后交易日 < {latest}）\n", flush=True)

    alive_mean = df.score.mean()
    print(f"全市场综合分均值: {alive_mean:.4f}（理论中位数 0.5）\n")

    sub = df[df.code.isin(dead)].sort_values(["code", "date"])
    if not len(sub):
        print("股票池内没有退市股数据")
        return 1

    print("=" * 78)
    print("  {:<16} {:>10} {:>10} {:>12} {:>10}".format(
        "距退市窗口", "样本股数", "平均分位", "vs 全市场", "落入前20%比例"))
    print("=" * 78)
    for w in (20, 60, 120, 240):
        tail = sub.groupby("code").tail(w)
        if not len(tail):
            continue
        m = tail.score.mean()
        # 前 20% 阈值：综合分 > 0.8 大致对应横截面前 20%（等权合成后分布收缩，
        # 这里用当日 80 分位实际阈值更准，故直接用 score 的经验分位）
        hit = (tail.score > tail.groupby("date").score.transform(
            lambda s: s.quantile(0.8))).mean() * 100
        print("  {:<16} {:>10,} {:>10.4f} {:>12} {:>10.1f}%".format(
            f"最后 {w} 个交易日", tail.code.nunique(), m,
            f"{m - alive_mean:+.4f}", hit))

    print("=" * 78)
    # 各因子单独看：退市股在退市前 60 日的分位
    print("\n分因子看（退市前 60 个交易日的横截面分位均值，0.5 = 市场中位）:")
    print("  分位是在**全市场**上算的，再取退市股——不是在退市股子集内部排名")
    tail = sub.groupby("code").tail(60)
    for f in FACTORS:
        print("  {:<10} {:.4f}".format(f, tail[f + "_pct"].mean()))

    # 结论判据
    m60 = sub.groupby("code").tail(60).score.mean()
    print()
    if m60 > 0.55:
        print("→ 退市股在退市前**显著高分**（>0.55）。因子把「正在崩盘的股票」")
        print("  当成「超跌反弹的好标的」——这正是 topk 集中持仓踩雷的原因。")
        print("  可考虑：对连续暴跌 / 价格创新低的股票加过滤，或对 revN 做跌幅上限截断。")
    elif m60 < 0.45:
        print("→ 退市股在退市前**偏低分**，说明因子没有系统性偏好它们。")
        print("  topk 的下滑可能来自别的原因（如个别极端案例的集中冲击）。")
    else:
        print("→ 退市股分数与全市场接近，不存在系统性的「高分陷阱」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
