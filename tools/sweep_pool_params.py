"""股票池参数扫描：`min_amount`（流动性门槛）与 `min_listed`（上市天数）。

动机
----
`run()` 的默认 `min_listed=120`、`min_amount=3e7`、`min_price=2.0`
—— 和 `hold=5` 一样，**从没系统扫过**（§10 已证明默认的 `hold=5` 不是最优）。

`min_amount` 此前只试过**提高**方向（3e7→1e8，结论是变差，因为砍掉了小盘 alpha），
但**降低**方向（1e7 / 2e7）从未测过 —— 放宽门槛可能纳入更多小盘股。

固定用当前最优配置：`topk=700`、`hold=20`、排名加权。

用法
----
    $PY tools/sweep_pool_params.py --bars data/stockbars/bars_all.parquet \
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

MIN_AMOUNTS = (1e7, 2e7, 3e7, 5e7, 1e8)
MIN_LISTED = (60, 120, 250)
BASE = dict(topk=700, hold=20, weight_mode="rank", tilt_min=0.45)


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
    ap.add_argument("--min-amounts", default=None,
                    help="覆盖 min_amount 扫描列表（逗号分隔），用于确认单调性；"
                         "给出时跳过 min_listed 维度")
    ap.add_argument("--out", default="results/pool_params.csv")
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
    years = d.date.nunique() / 244.0
    idx = sorted(d.date.unique())
    bm = bench_index(args.indexes, "SH000852", idx)
    print(f"区间 {args.start}~{args.end} ({years:.1f} 年)  "
          f"基准中证1000：年化 {bm.get('年化收益', 0):+.2f}%\n")

    rows = []

    def sweep(tag, param, values):
        print("=" * 100)
        print(f"{tag}（其他参数固定为 topk=700 / hold=20）")
        print("=" * 100)
        print("  {:<14} {:>8} {:>8} {:>7} {:>9} {:>6} {:>8} {:>10}".format(
            param, "年化%", "波动%", "夏普", "回撤%", "卡玛", "持仓数", "vs中证1000"))
        for v in values:
            kw = dict(BASE)
            kw[param] = v
            try:
                eq, tr, meta = run(df, ALL8, start=args.start, end=args.end, **kw)
            except Exception as e:
                print(f"  {v:<14} 失败: {type(e).__name__}: {e}", flush=True)
                continue
            m = metrics(eq.equity)
            ex = m.get("年化收益", 0) - bm.get("年化收益", 0)
            label = f"{v:.0e}" if isinstance(v, float) else str(v)
            print("  {:<14} {:>8.2f} {:>8.2f} {:>7.2f} {:>9.2f} {:>6.2f} {:>8.0f} "
                  "{:>10.2f}".format(
                      label, m.get("年化收益", 0), m.get("年化波动", 0),
                      m.get("夏普比率", 0), m.get("最大回撤", 0),
                      m.get("卡玛比率", 0), meta.get("avg_hold", 0), ex), flush=True)
            rows.append({"扫描维度": tag, "参数": param, "取值": label,
                         "年化收益": m.get("年化收益", 0),
                         "夏普比率": m.get("夏普比率", 0),
                         "最大回撤": m.get("最大回撤", 0),
                         "平均持仓": meta.get("avg_hold", 0),
                         "vs中证1000": ex})
            pd.DataFrame(rows).to_csv(args.out, index=False, encoding="utf-8-sig")
        print()

    if args.min_amounts:
        amts = tuple(float(x) for x in args.min_amounts.split(","))
        sweep("流动性门槛 min_amount", "min_amount", amts)
    else:
        sweep("流动性门槛 min_amount", "min_amount", MIN_AMOUNTS)
        sweep("上市天数 min_listed", "min_listed", MIN_LISTED)

    if rows:
        r = pd.DataFrame(rows)
        print("=" * 100)
        print("各维度最优（按夏普）：")
        for tag, g in r.groupby("扫描维度", sort=False):
            b = g.loc[g.夏普比率.idxmax()]
            print(f"  {tag:<24} {b.参数}={b.取值}  年化 {b.年化收益:.2f}%  "
                  f"夏普 {b.夏普比率:.2f}  回撤 {b.最大回撤:.2f}%  "
                  f"超额 {b.vs中证1000:+.2f}pp")
        print(f"\n结果已写出 {args.out}")
        print("\n注意：默认值是 min_amount=3e7、min_listed=120。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
