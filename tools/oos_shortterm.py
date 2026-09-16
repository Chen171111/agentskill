"""短线策略 v3 的**样本外检验**（防止「22 个配置里挑最好」的选择性偏差）。

样本内 IS  : 2019-01-01 ~ 2022-12-31
样本外 OOS : 2023-01-01 ~ 2026-09-11

只检验 v3 里表现最好的几个配置 + 随机对照（对照必须同区间）。
若 IS 好而 OOS 崩 → 过拟合，不可用。

用法
----
    $PY tools/oos_shortterm.py --bars data/stockbars/bars_all.parquet \
        --universe data/stockbars/universe_all.csv
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

IS = ("20190101", "20221231")
OOS = ("20230101", "20260911")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--index", default="data/indexes/000300.SH.csv")
    ap.add_argument("--out", default="results/shortterm_oos.csv")
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

    df["c_trio"] = cs("rev20") + cs("vol20") + cs("turn20")
    df["rand"] = pd.Series(
        __import__("numpy").random.default_rng(3).random(len(df)), index=df.index)

    index_above = None
    if os.path.exists(args.index):
        index_above = load_index_ma(args.index)["above"].to_dict()

    base = dict(min_amt=1e8, max_amt=3e9, min_lu=0, require_ma_bull=False)

    configs = [
        ("三重 hold60 topk5", 5, 60, False, "c_trio"),
        ("三重 hold40 topk10", 10, 40, False, "c_trio"),
        ("三重 hold20 topk10", 10, 20, False, "c_trio"),
        ("三重+大盘门 hold20 topk10", 10, 20, True, "c_trio"),
        ("三重+大盘门 hold40 topk20", 20, 40, True, "c_trio"),
        ("三重+大盘门 hold20 topk5", 5, 20, True, "c_trio"),
        ("随机(对照) hold60 topk5", 5, 60, False, "rand"),
        ("随机(对照) hold20 topk10", 10, 20, False, "rand"),
    ]

    def bench_metrics(s, e):
        rows = []
        for dt, g in df[(df.date >= s) & (df.date <= e)].groupby("date"):
            u = ((g.listed >= 120) & (g.close >= 2.0) & (~g.suspended)
                 & (g.amt_ma20 >= 1e8) & (g.amt_ma20 <= 3e9))
            rows.append((dt, float(g.loc[u, "ret1"].mean())))
        b = pd.DataFrame(rows, columns=["date", "r"]).set_index("date").dropna()
        return metrics((1 + b.r).cumprod() * 100_000)

    print()
    print("=" * 116)
    print("  {:<26} {:>18} {:>18} {:>18}".format(
        "配置", "样本内 IS 19~22", "样本外 OOS 23~26", "IS→OOS 夏普变化"))
    print("  {:<26} {:>8} {:>9} {:>8} {:>9} {:>8}".format(
        "", "年化%", "夏普", "年化%", "夏普", "Δ夏普"))
    print("=" * 116)

    bm_is, bm_oos = bench_metrics(*IS), bench_metrics(*OOS)
    print("  {:<26} {:>8.2f} {:>9.2f} {:>8.2f} {:>9.2f} {:>8}".format(
        "★ 等权全池基准", bm_is.get("年化收益", 0), bm_is.get("夏普比率", 0),
        bm_oos.get("年化收益", 0), bm_oos.get("夏普比率", 0), "-"))

    rows = []
    for name, topk, hold, gate, score in configs:
        kw = dict(base, score_col=score)
        if gate:
            kw["index_above"] = index_above
        out = {}
        for tag, (s, e) in (("IS", IS), ("OOS", OOS)):
            eq, tr, meta = run_short(df, start=s, end=e, topk=topk, hold=hold,
                                     cash0=100_000.0, **kw)
            out[tag] = metrics(eq.equity)
        d = out["OOS"].get("夏普比率", 0) - out["IS"].get("夏普比率", 0)
        print("  {:<26} {:>8.2f} {:>9.2f} {:>8.2f} {:>9.2f} {:>+8.2f}".format(
            name, out["IS"].get("年化收益", 0), out["IS"].get("夏普比率", 0),
            out["OOS"].get("年化收益", 0), out["OOS"].get("夏普比率", 0), d), flush=True)
        rows.append({"配置": name,
                     "IS年化": out["IS"].get("年化收益", 0), "IS夏普": out["IS"].get("夏普比率", 0),
                     "IS回撤": out["IS"].get("最大回撤", 0),
                     "OOS年化": out["OOS"].get("年化收益", 0), "OOS夏普": out["OOS"].get("夏普比率", 0),
                     "OOS回撤": out["OOS"].get("最大回撤", 0), "夏普变化": d})
    print("=" * 116)
    print(f"\n基准: IS {bm_is.get('年化收益',0):.2f}%/{bm_is.get('夏普比率',0):.2f}  "
          f"OOS {bm_oos.get('年化收益',0):.2f}%/{bm_oos.get('夏普比率',0):.2f}")
    print("判读：OOS 夏普明显低于 IS（Δ 为负且大）→ 过拟合，不可实盘。")

    if rows:
        pd.DataFrame(rows).to_csv(args.out, index=False, encoding="utf-8-sig")
        print(f"结果已写出 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
