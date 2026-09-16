"""个股多因子：换手压缩扫描（调仓频率 × 滞回缓冲带）。

动机
----
推荐配置 tilt(>0.45) 全区间 315,695 笔交易，实盘不可行。

先定位换手来源：引擎**已经是周频**（`hold=5`，见 `sweep_stock_model.py` 里
9 个变体全是 hold=5），所以问题不是调仓太勤。真正来源是 **tilt 每周对全池
重算综合分 → 分数在 `tilt_min` 上下反复横跳的股票被反复买卖**。执行端
`if c in units: continue` 说明已持仓不调权重，交易笔数几乎全部来自「进出组合」。

两把刀
------
1. `hold`   —— 拉长调仓间隔（5 / 10 / 20 个交易日）
2. `buffer` —— 滞回缓冲带：已持仓放宽卖出线（tilt_min − buffer），
               未持仓抬高买入线（tilt_min + buffer），中间地带维持原状

判据：在**夏普不降、回撤不涨**的前提下把交易笔数压下来。
只砍换手却把优势一起砍掉的配置不算成功。

用法
----
    PY=C:/Users/XiaoQi/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe
    $PY tools/sweep_turnover.py --bars data/stockbars/bars.parquet \
        --universe data/stockbars/universe.csv
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

HOLDS = (5, 10, 20)
BUFFERS = (0.0, 0.02, 0.05, 0.10)
TILT_MIN = 0.45


def bench_metrics(df, start, end, min_listed=120, min_amount=3e7, min_price=2.0):
    """等权全池基准：逐日等权，与策略同一股票池过滤。"""
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
    ap.add_argument("--out", default="results/turnover_sweep.csv")
    args = ap.parse_args(argv)

    bars = pd.read_parquet(args.bars)
    bars["date"] = bars.date.astype(str).str.replace("-", "", regex=False)
    if args.universe and os.path.exists(args.universe):
        u = pd.read_csv(args.universe, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
        n0 = bars.code.nunique()
        bars = bars[~bars.code.isin(st)]
        print(f"剔除 ST/退市 {len(st)} 只：{n0} -> {bars.code.nunique()} 只")
    print(f"日线: {len(bars):,} 行, {bars.code.nunique()} 只, "
          f"{bars.date.nunique()} 交易日", flush=True)

    print("计算因子…", flush=True)
    df = build_features(bars)

    bench = bench_metrics(df, args.start, args.end)
    print(f"\n基准 等权全池  年化 {bench['年化收益']:+.2f}%  "
          f"夏普 {bench['夏普比率']:.2f}  回撤 {bench['最大回撤']:.2f}%", flush=True)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    rows = []
    print("\n" + "=" * 128)
    print("  {:<8} {:<7} {:>9} {:>8} {:>7} {:>9} {:>6} {:>8} {:>10} {:>9}".format(
        "调仓", "buffer", "年化%", "波动%", "夏普", "回撤%", "卡玛",
        "持仓数", "交易笔数", "超额pp"))
    print("=" * 128)

    for hold in HOLDS:
        for buf in BUFFERS:
            try:
                eq, tr, meta = run(df, ALL8, start=args.start, end=args.end,
                                   topk=50, hold=hold, weight_mode="tilt",
                                   tilt_min=TILT_MIN, buffer=buf)
            except Exception as e:
                print(f"  hold={hold} buffer={buf}  失败: {type(e).__name__}: {e}",
                      flush=True)
                continue
            m = metrics(eq.equity)
            n_tr = len(tr)
            excess = m.get("年化收益", 0) - bench.get("年化收益", 0)
            avg_hold = meta.get("avg_hold", 0.0)
            print("  {:<8} {:<7} {:>9.2f} {:>8.2f} {:>7.2f} {:>9.2f} {:>6.2f} "
                  "{:>8.0f} {:>10,} {:>9.2f}".format(
                      f"{hold}日", f"{buf:.2f}", m.get("年化收益", 0),
                      m.get("年化波动", 0), m.get("夏普比率", 0),
                      m.get("最大回撤", 0), m.get("卡玛比率", 0), avg_hold,
                      n_tr, excess),
                  flush=True)
            rows.append({
                "hold": hold, "buffer": buf, "年化收益": m.get("年化收益", 0),
                "年化波动": m.get("年化波动", 0), "夏普比率": m.get("夏普比率", 0),
                "最大回撤": m.get("最大回撤", 0), "卡玛比率": m.get("卡玛比率", 0),
                "平均持仓": avg_hold, "交易笔数": n_tr,
                "买": int((tr.side == "buy").sum()) if len(tr) else 0,
                "卖": int((tr.side == "sell").sum()) if len(tr) else 0,
                "超额年化pp": excess, "退市了结": meta.get("n_writeoff", 0),
            })
            # 增量落盘：每跑完一个变体就写一次，机器死机也不丢前面的结果
            pd.DataFrame(rows).to_csv(args.out, index=False,
                                      encoding="utf-8-sig")

    print("=" * 128)
    if rows:
        r = pd.DataFrame(rows)
        base = r[(r.hold == 5) & (r.buffer == 0.0)]
        if len(base):
            b0 = base.iloc[0]
            print(f"\n基准行 hold=5/buffer=0: 年化 {b0.年化收益:.2f}%  "
                  f"夏普 {b0.夏普比率:.2f}  回撤 {b0.最大回撤:.2f}%  "
                  f"持仓 {b0.平均持仓:.0f} 只  交易 {int(b0.交易笔数):,} 笔")
            r2 = r.assign(换手降幅=(1 - r.交易笔数 / b0.交易笔数) * 100,
                          夏普差=r.夏普比率 - b0.夏普比率,
                          回撤差=r.最大回撤 - b0.最大回撤,
                          年化差=r.年化收益 - b0.年化收益)
            print("\n相对基线的变化（夏普差 >= 0 且换手降幅大 = 白赚）：")
            print("  {:<8} {:<7} {:>11} {:>9} {:>11} {:>10}".format(
                "调仓", "buffer", "换手降幅%", "夏普差", "回撤差pp", "年化差pp"))
            for _, x in r2.iterrows():
                print("  {:<8} {:<7} {:>11.1f} {:>9.2f} {:>11.2f} {:>10.2f}".format(
                    f"{int(x.hold)}日", f"{x.buffer:.2f}", x.换手降幅,
                    x.夏普差, x.回撤差, x.年化差))
        print(f"\n结果已写出 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
