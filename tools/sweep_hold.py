"""调仓周期（hold）扫描：`hold=5` 这个默认值从没验证过。

动机
----
`backtest_stock.run()` 的默认 `hold=5`（周频），此项目所有回测都用它，
**但从未扫过这个参数**。而社区策略那边已证明持有期影响巨大
（hold 5→20 天，同一批策略年化提升 15~25pp）。

调仓周期直接影响两件事：
- **信号时效**：太长会错过反转（A 股反转因子的 IC 在 h=5 最高，h=1 更低）
- **换手成本**：太短则换手高、成本吃掉收益

本脚本扫 hold ∈ {3,5,10,15,20,30}，对当前两个候选配置各跑一遍。

用法
----
    $PY tools/sweep_hold.py --bars data/stockbars/bars_all.parquet \
        --universe data/stockbars/universe_all.csv
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import build_features, run, metrics  # noqa: E402

ALL8 = [("rev20", 1.0), ("rev60", 1.0), ("rev120", 1.0), ("rev5", 1.0),
        ("vol20", 1.0), ("max20", 1.0), ("turn20", 1.0), ("illiq20", 1.0)]

HOLDS = (3, 5, 10, 15, 20, 30)
CONFIGS = [
    ("topk700 排名加权", dict(topk=700, weight_mode="rank", tilt_min=0.45)),
    ("tilt(>0.45)", dict(topk=50, weight_mode="tilt", tilt_min=0.45)),
]


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
    ap.add_argument("--out", default="results/hold_sweep.csv")
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

    d = df[(df.date >= args.start) & (df.date <= args.end)]
    ndays = d.date.nunique()
    years = ndays / 244.0
    idx = sorted(d.date.unique())
    bm = bench_index(args.indexes, "SH000852", idx)
    print(f"区间 {args.start}~{args.end}  {ndays} 交易日 ({years:.1f} 年)")
    print(f"基准 中证1000：年化 {bm.get('年化收益', 0):+.2f}%  "
          f"夏普 {bm.get('夏普比率', 0):.2f}  回撤 {bm.get('最大回撤', 0):.2f}%\n")

    rows = []
    for cname, kw in CONFIGS:
        print("=" * 108)
        print(f"{cname}   调仓周期扫描")
        print("=" * 108)
        print("  {:<8} {:>8} {:>8} {:>7} {:>9} {:>6} {:>8} {:>11} {:>9}".format(
            "hold", "年化%", "波动%", "夏普", "回撤%", "卡玛", "持仓数",
            "交易笔数", "vs中证1000"))
        for h in HOLDS:
            try:
                eq, tr, meta = run(df, ALL8, start=args.start, end=args.end,
                                   hold=h, **kw)
            except Exception as e:
                print(f"  {h:<8} 失败: {type(e).__name__}: {e}", flush=True)
                continue
            m = metrics(eq.equity)
            ex = m.get("年化收益", 0) - bm.get("年化收益", 0)
            print("  {:<8} {:>8.2f} {:>8.2f} {:>7.2f} {:>9.2f} {:>6.2f} {:>8.0f} "
                  "{:>11,} {:>9.2f}".format(
                      f"{h}日", m.get("年化收益", 0), m.get("年化波动", 0),
                      m.get("夏普比率", 0), m.get("最大回撤", 0),
                      m.get("卡玛比率", 0), meta.get("avg_hold", 0), len(tr), ex),
                  flush=True)
            rows.append({"配置": cname, "hold": h,
                         "年化收益": m.get("年化收益", 0),
                         "年化波动": m.get("年化波动", 0),
                         "夏普比率": m.get("夏普比率", 0),
                         "最大回撤": m.get("最大回撤", 0),
                         "卡玛比率": m.get("卡玛比率", 0),
                         "平均持仓": meta.get("avg_hold", 0),
                         "交易笔数": len(tr), "vs中证1000": ex})
            pd.DataFrame(rows).to_csv(args.out, index=False, encoding="utf-8-sig")
        print()

    if rows:
        r = pd.DataFrame(rows)
        print("=" * 108)
        print("最优 hold（按夏普）：")
        for cname, _ in CONFIGS:
            g = r[r.配置 == cname]
            if len(g):
                b = g.loc[g.夏普比率.idxmax()]
                print(f"  {cname:<20} hold={int(b.hold)}日  "
                      f"年化 {b.年化收益:.2f}%  夏普 {b.夏普比率:.2f}  "
                      f"回撤 {b.最大回撤:.2f}%  交易 {int(b.交易笔数):,} 笔")
        print(f"\n结果已写出 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
