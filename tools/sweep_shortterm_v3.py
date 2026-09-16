"""短线策略扫描 v3：在 v2 基础上压榨三个杠杆。

v2 结论
-------
- 持有期是决定性变量：持5日 -20.05% → 持10日 -8.37% → 持20日 **+4.12%**
- 成本吃掉 3.83pp（真成本 4.12% vs 零成本 7.95%）
- 最优配置风险调整后仍跑输 ETF 线

v3 压榨的三个杠杆
----------------
1. **持有期**：20 → 40 日（换手再降一半）
2. **持仓数**：topk 5 → 10 → 20（分散特质风险，但每只金额变小）
3. **大盘门**：沪深300 站上 MA20 才持仓（避开熊市）

因子沿用 v2 最优的「三重(rev20+vol20+turn20)」，基础过滤同为成交额 1~30 亿。

用法
----
    $PY tools/sweep_shortterm_v3.py --bars data/stockbars/bars_all.parquet \
        --universe data/stockbars/universe_all.csv --start 20190101 --end 20260911
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import metrics  # noqa: E402
from tools.backtest_shortterm import (build_short_features, run_short,  # noqa: E402
                                      load_index_ma)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--index", default="data/indexes/000300.SH.csv")
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260911")
    ap.add_argument("--out", default="results/shortterm_v3.csv")
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

    def cs(col):
        return df.groupby("date")[col].rank(pct=True)

    print("构造组合因子…", flush=True)
    df["c_trio"] = cs("rev20") + cs("vol20") + cs("turn20")
    df["c_trio5"] = cs("rev5") + cs("vol20") + cs("turn20")

    index_above = None
    if os.path.exists(args.index):
        index_above = load_index_ma(args.index)["above"].to_dict()

    base = dict(min_amt=1e8, max_amt=3e9, min_lu=0, require_ma_bull=False)

    # (名称, topk, hold, 大盘门, 因子列)
    configs = []
    for score, tag in (("c_trio", "三重"), ("c_trio5", "三重(rev5)")):
        for hold in (20, 40, 60):
            for topk in (5, 10, 20):
                configs.append((f"{tag} hold{hold} topk{topk}",
                                topk, hold, False, score))
    # 大盘门版本（仅 hold20/40 × topk10/20）
    for hold in (20, 40):
        for topk in (10, 20):
            configs.append((f"三重+大盘门 hold{hold} topk{topk}",
                            topk, hold, True, "c_trio"))

    # 基准
    bench = []
    for dt, g in df[(df.date >= args.start) & (df.date <= args.end)].groupby("date"):
        u = ((g.listed >= 120) & (g.close >= 2.0) & (~g.suspended)
             & (g.amt_ma20 >= 1e8) & (g.amt_ma20 <= 3e9))
        bench.append((dt, float(g.loc[u, "ret1"].mean())))
    b = pd.DataFrame(bench, columns=["date", "r"]).set_index("date").dropna()
    beq = (1 + b.r).cumprod() * 100_000
    bm = metrics(beq)

    print(f"\n区间 {args.start}~{args.end}  真成本(万5/最低5元/印花0.1%/滑点0.05%)")
    print("=" * 124)
    print("  {:<30} {:>8} {:>7} {:>8} {:>7} {:>9} {:>8} {:>7} {:>11}".format(
        "配置", "年化%", "波动%", "夏普", "卡玛", "回撤%", "持仓", "交易数", "末净值"))
    print("=" * 124)
    print("  {:<30} {:>8.2f} {:>7.2f} {:>8.2f} {:>7.2f} {:>9.2f} {:>8} {:>7} {:>11,.0f}".format(
        "★ 等权全池基准", bm.get("年化收益", 0), bm.get("年化波动", 0),
        bm.get("夏普比率", 0), bm.get("卡玛比率", 0), bm.get("最大回撤", 0),
        "-", "-", float(beq.iloc[-1])))

    rows = []
    for name, topk, hold, gate, score in configs:
        kw = dict(base, score_col=score)
        if gate:
            kw["index_above"] = index_above
        try:
            eq, tr, meta = run_short(df, start=args.start, end=args.end, topk=topk,
                                     hold=hold, cash0=100_000.0, **kw)
        except Exception as e:
            print("  {:<30} 失败: {}".format(name, e))
            continue
        m = metrics(eq.equity)
        last = float(eq.equity.iloc[-1])
        print("  {:<30} {:>8.2f} {:>7.2f} {:>8.2f} {:>7.2f} {:>9.2f} {:>8.1f} {:>7,d} "
              "{:>11,.0f}".format(
                  name, m.get("年化收益", 0), m.get("年化波动", 0), m.get("夏普比率", 0),
                  m.get("卡玛比率", 0), m.get("最大回撤", 0), meta["avg_hold"],
                  len(tr), last), flush=True)
        rows.append({"配置": name, "年化收益": m.get("年化收益", 0),
                     "年化波动": m.get("年化波动", 0), "夏普比率": m.get("夏普比率", 0),
                     "卡玛比率": m.get("卡玛比率", 0), "最大回撤": m.get("最大回撤", 0),
                     "平均持仓": meta["avg_hold"], "交易笔数": len(tr), "末净值": last})
    print("=" * 124)

    if rows:
        pd.DataFrame(rows).to_csv(args.out, index=False, encoding="utf-8-sig")
        print(f"结果已写出 {args.out}")
        best = max(rows, key=lambda r: r["夏普比率"])
        print(f"\n夏普最高: {best['配置']}  年化 {best['年化收益']:.2f}%  "
              f"夏普 {best['夏普比率']:.2f}  回撤 {best['最大回撤']:.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
