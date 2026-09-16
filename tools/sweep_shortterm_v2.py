"""短线策略扫描 v2：**顺着 A 股的「中期反转」规律**，而不是追强势股。

为什么推翻 v1
-------------
v1 假设「强势股延续」（聚宽 182% 策略的日线版：20日涨停基因 + 均线多头）。
`tools/diag_shortterm.py` 的分解实验证明这是**负 alpha**（零成本、随机选股下）：

| 过滤条件 | 年化 |
|---|---|
| 无过滤（对照） | +2.92% |
| 仅 20日涨停≥1 | **-7.56%** |
| 仅 均线多头 | **-13.44%** |
| 两者都有 | **-23.77%** |

且等权全池基准是 +9.63% → 引擎无致命 bug。
这与本项目早已验证的结论一致：**A 股 2018~2026 是中期反转市，
20/60/120 日动量 IC 全为负、分组严格单调递减**（见 tools/test_stock_factors.py）。

v2 方向
-------
把打分换成**反转族 + 低波动族**（build_features 里已统一为「越大越看好」）：
- `rev5/rev20/rev60`：过去 N 日跌幅越大 → 分数越高
- `vol20`：波动越小 → 分数越高
- `turn20`：换手越低 → 分数越高
- `illiq20`：非流动性越高 → 分数越高（本项目实测有溢价，但注意退市陷阱）

且**去掉** 20日涨停 / 均线多头 两个过滤。

用法
----
    $PY tools/sweep_shortterm_v2.py --bars data/stockbars/bars_all.parquet \
        --universe data/stockbars/universe_all.csv --start 20190101 --end 20260911
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260911")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--out", default="results/shortterm_v2.csv")
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
    rng = np.random.default_rng(11)
    df["rand"] = rng.random(len(df))

    # 横截面分位（按日），用于构造组合因子
    def cs(col):
        return df.groupby("date")[col].rank(pct=True)

    print("构造组合因子…", flush=True)
    df["c_rev20_vol"] = cs("rev20") + cs("vol20")
    df["c_rev5_vol"] = cs("rev5") + cs("vol20")
    df["c_rev20_turn"] = cs("rev20") + cs("turn20")
    df["c_trio"] = cs("rev20") + cs("vol20") + cs("turn20")

    # 无 lu / 无 ma_bull（两者均为负 alpha）
    base = dict(min_amt=1e8, max_amt=3e9, min_lu=0, require_ma_bull=False)
    zero = dict(commission=0.0, stamp=0.0, slippage=0.0, min_comm=0.0)

    configs = [
        ("01 随机(对照)", dict(base, score_col="rand"), 5, dict()),
        ("02 rev5", dict(base, score_col="rev5"), 5, dict()),
        ("03 rev20", dict(base, score_col="rev20"), 5, dict()),
        ("04 rev60", dict(base, score_col="rev60"), 5, dict()),
        ("05 vol20(低波)", dict(base, score_col="vol20"), 5, dict()),
        ("06 turn20(低换手)", dict(base, score_col="turn20"), 5, dict()),
        ("07 rev20+vol20", dict(base, score_col="c_rev20_vol"), 5, dict()),
        ("08 rev5+vol20", dict(base, score_col="c_rev5_vol"), 5, dict()),
        ("09 rev20+turn20", dict(base, score_col="c_rev20_turn"), 5, dict()),
        ("10 三重(rev20+vol20+turn20)", dict(base, score_col="c_trio"), 5, dict()),
        ("11 三重·持10日", dict(base, score_col="c_trio"), 10, dict()),
        ("12 三重·持20日", dict(base, score_col="c_trio"), 20, dict()),
        # ---- 真实成本 ----
        ("13 三重·持5日·真成本", dict(base, score_col="c_trio"), 5, dict()),
        ("14 三重·持20日·真成本", dict(base, score_col="c_trio"), 20, dict()),
        ("15 rev20·持20日·真成本", dict(base, score_col="rev20"), 20, dict()),
        # ---- 零成本版对照 ----
        ("16 三重·持20日·零成本", dict(base, score_col="c_trio"), 20, dict(**zero)),
    ]

    # 基准
    bench = []
    for dt, g in df[(df.date >= args.start) & (df.date <= args.end)].groupby("date"):
        u = ((g.listed >= 120) & (g.close >= 2.0) & (~g.suspended)
             & (g.amt_ma20 >= 1e8) & (g.amt_ma20 <= 3e9))
        bench.append((dt, float(g.loc[u, "ret1"].mean())))
    b = pd.DataFrame(bench, columns=["date", "r"]).set_index("date").dropna()
    beq = (1 + b.r).cumprod() * 100_000
    bm = metrics(beq)

    print(f"\n区间 {args.start}~{args.end}  topk={args.topk}  （基础过滤：成交额1~30亿，"
          f"已去掉涨停/均线过滤）")
    print("=" * 122)
    print("  {:<28} {:>9} {:>8} {:>7} {:>9} {:>7} {:>9} {:>11}".format(
        "配置", "年化%", "波动%", "夏普", "回撤%", "持仓", "交易数", "末净值"))
    print("=" * 122)
    print("  {:<28} {:>9.2f} {:>8.2f} {:>7.2f} {:>9.2f} {:>7} {:>9} {:>11,.0f}".format(
        "★ 等权全池基准", bm.get("年化收益", 0), bm.get("年化波动", 0), bm.get("夏普比率", 0),
        bm.get("最大回撤", 0), "-", "-", float(beq.iloc[-1])))

    rows = []
    for name, kw, hold, extra in configs:
        try:
            m_kw = dict(kw)
            m_kw.update(extra)
            eq, tr, meta = run_short(df, start=args.start, end=args.end, topk=args.topk,
                                     hold=hold, cash0=100_000.0, **m_kw)
        except Exception as e:
            print("  {:<28} 失败: {}: {}".format(name, type(e).__name__, e))
            continue
        m = metrics(eq.equity)
        last = float(eq.equity.iloc[-1])
        exc = m.get("年化收益", 0) - bm.get("年化收益", 0)
        print("  {:<28} {:>9.2f} {:>8.2f} {:>7.2f} {:>9.2f} {:>7.1f} {:>9,d} {:>11,.0f}"
              "  {:+.2f}pp".format(
                  name, m.get("年化收益", 0), m.get("年化波动", 0), m.get("夏普比率", 0),
                  m.get("最大回撤", 0), meta["avg_hold"], len(tr), last, exc), flush=True)
        rows.append({"配置": name, "年化收益": m.get("年化收益", 0),
                     "年化波动": m.get("年化波动", 0), "夏普比率": m.get("夏普比率", 0),
                     "最大回撤": m.get("最大回撤", 0), "平均持仓": meta["avg_hold"],
                     "交易笔数": len(tr), "末净值": last, "超额pp": exc})
    print("=" * 122)

    if rows:
        pd.DataFrame(rows).to_csv(args.out, index=False, encoding="utf-8-sig")
        print(f"结果已写出 {args.out}")
        best = max(rows, key=lambda r: r["夏普比率"])
        print(f"\n夏普最高: {best['配置']}  年化 {best['年化收益']:.2f}%  "
              f"夏普 {best['夏普比率']:.2f}  超额 {best['超额pp']:+.2f}pp")
    return 0


if __name__ == "__main__":
    sys.exit(main())
