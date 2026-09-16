"""把社区策略「因子化」后，检验它对多因子模型的增量价值。

背景
----
`tools/sweep_tdx.py` 已证明 15 条社区策略作为**布尔条件选股**全部跑输。
但其中唯二的两条**抄底型**（二次金叉、低位金叉）是表现最好的，
且方向与现有 `revN` 反转因子一致。

本脚本回答三个问题：
1. 这两个信号作为**因子**（而非布尔条件）加入模型，能不能改善表现？
2. 它们与现有 8 因子的**相关性**如何——是新增信息还是冗余？
3. 「追涨程度」这个指标，是否等价于已有的 `rev20`？（若是，则该建议冗余）

因子化方式
----------
布尔信号直接当因子会「触发当天为 1、次日归 0」，导致买了立刻卖。
故用 `build_signal(hold=20)` 生成**持有掩码**（触发后 20 天内恒为 1）作为因子值，
与 `sweep_tdx.py` 里 hold=20 表现最好这一实测结果保持一致。

用法
----
    $PY tools/sweep_tdx_factor.py --bars data/stockbars/bars_all.parquet \
        --universe data/stockbars/universe_all.csv
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import build_features, run, metrics, fmt  # noqa: E402
from tools.tdx_strategies import build_signal  # noqa: E402

ALL8 = [("rev20", 1.0), ("rev60", 1.0), ("rev120", 1.0), ("rev5", 1.0),
        ("vol20", 1.0), ("max20", 1.0), ("turn20", 1.0), ("illiq20", 1.0)]
SIGNAL_HOLD = 20


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
    ap.add_argument("--out", default="results/tdx_factor.csv")
    args = ap.parse_args(argv)

    bars = pd.read_parquet(args.bars)
    bars["date"] = bars.date.astype(str).str.replace("-", "", regex=False)
    if args.universe and os.path.exists(args.universe):
        u = pd.read_csv(args.universe, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
        bars = bars[~bars.code.isin(st)]
    print(f"日线: {len(bars):,} 行, {bars.code.nunique()} 只", flush=True)
    print("计算基础列与因子…", flush=True)
    df = build_features(bars)

    print(f"生成社区信号掩码（触发后 {SIGNAL_HOLD} 天内为 1）…", flush=True)
    df["sg"] = build_signal(df, "macd_second_gold", hold=SIGNAL_HOLD).astype(float)
    df["lg"] = build_signal(df, "macd_low_gold", hold=SIGNAL_HOLD).astype(float)

    d = df[(df.date >= args.start) & (df.date <= args.end)]

    # ---------- 1) 信号与现有因子的相关性 ----------
    print("\n" + "=" * 88)
    print("1) 信号与现有 8 因子的相关性（信号=1 的样本上，Pearson）")
    print("=" * 88)
    fcols = [f for f, _ in ALL8]
    # 信号列在触发样本上恒为 1（方差 0），无法直接算相关系数。
    # 改看「触发样本在**当日全市场**各因子上的横截面分位」（0.5 = 中位）：
    # 分位 > 0.6 说明信号偏好该因子的高分组，< 0.4 偏好低分组。
    print("  口径：触发样本在当日全市场该因子上的横截面分位均值（0.5 = 中位）")
    pct_cols = {f: d.groupby("date")[f].rank(pct=True) for f in fcols}
    for sig_name, col in (("二次金叉", "sg"), ("低位金叉", "lg")):
        sel_idx = d.index[d[col] > 0]
        print(f"\n  {sig_name}：触发样本 {len(sel_idx):,} 行")
        for f in fcols:
            m = pct_cols[f].loc[sel_idx].mean()
            flag = "  ← 偏好高分组" if m > 0.6 else (
                "  ← 偏好低分组" if m < 0.4 else "")
            print(f"    {f:<10} {m:.4f}{flag}")

    # ---------- 2) 「追涨程度」是否等价于 rev20 ----------
    print("\n" + "=" * 88)
    print("2) 「追涨程度」与已有 rev20 的关系（检验建议是否冗余）")
    print("=" * 88)
    dd = d.copy()
    dd["ret20"] = dd.groupby("code", sort=False).close.transform(
        lambda s: s / s.shift(20) - 1)
    # 前复权价里可能出现 0 值（退市股），除出来是 inf → 必须先清掉，
    # 否则 corr() 会整体返回 nan（踩过）
    dd = dd.replace([np.inf, -np.inf], np.nan)
    dd["chase20"] = dd.groupby("date")["ret20"].rank(pct=True)
    sub = dd[["chase20", "rev20", "rev5", "rev60"]].dropna()
    print(f"  样本 {len(sub):,} 行")
    print(f"  chase20（追涨程度） vs rev20 ：{sub.chase20.corr(sub.rev20):+.4f}")
    print(f"  chase20              vs rev5  ：{sub.chase20.corr(sub.rev5):+.4f}")
    print(f"  chase20              vs rev60 ：{sub.chase20.corr(sub.rev60):+.4f}")
    c = sub.chase20.corr(sub.rev20)
    print(f"\n  → {'相关性极高，chase20 基本是 rev20 的翻版，无增量价值' if abs(c) > 0.9 else '相关性中等，可能有独立信息'}")

    # ---------- 3) 增量价值：作为第 9/10 个因子回测 ----------
    bench = bench_metrics(df, args.start, args.end)
    print("\n" + "=" * 88)
    print(f"3) 增量价值回测（tilt 0.45，区间 {args.start}~{args.end}）")
    print("=" * 88)
    print(fmt("等权全池基准", bench))

    variants = [
        ("8 因子（基线）", ALL8),
        ("8 因子 + 二次金叉", ALL8 + [("sg", 1.0)]),
        ("8 因子 + 低位金叉", ALL8 + [("lg", 1.0)]),
        ("8 因子 + 两者", ALL8 + [("sg", 1.0), ("lg", 1.0)]),
    ]
    rows = []
    for tag, facs in variants:
        eq, tr, meta = run(df, facs, start=args.start, end=args.end, topk=50,
                           hold=5, weight_mode="tilt", tilt_min=0.45)
        m = metrics(eq.equity)
        print(fmt(tag, m) + f"  持仓 {meta.get('avg_hold', 0):.0f}")
        rows.append({"配置": tag, "年化收益": m.get("年化收益", 0),
                     "年化波动": m.get("年化波动", 0),
                     "夏普比率": m.get("夏普比率", 0),
                     "最大回撤": m.get("最大回撤", 0),
                     "卡玛比率": m.get("卡玛比率", 0),
                     "平均持仓": meta.get("avg_hold", 0),
                     "交易笔数": len(tr),
                     "超额年化pp": m.get("年化收益", 0) - bench.get("年化收益", 0)})

    print("=" * 88)
    if rows:
        r = pd.DataFrame(rows)
        base = r.iloc[0]
        print(f"\n相对 8 因子基线（年化 {base.年化收益:.2f}%、夏普 {base.夏普比率:.2f}）：")
        print("  {:<22} {:>10} {:>10} {:>10}".format("配置", "年化差pp", "夏普差", "回撤差pp"))
        for _, x in r.iloc[1:].iterrows():
            print("  {:<22} {:>10.2f} {:>10.2f} {:>10.2f}".format(
                x.配置, x.年化收益 - base.年化收益, x.夏普比率 - base.夏普比率,
                x.最大回撤 - base.最大回撤))
        print("\n  判据：年化差与夏普差**同时为正**才算有增量价值；")
        print("        若夏普下降，说明它带来的是风险而非收益。")
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        r.to_csv(args.out, index=False, encoding="utf-8-sig")
        print(f"\n结果已写出 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
