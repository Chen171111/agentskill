"""样本内外检验：新推荐的落地配置（topk500）是不是只在样本内有效？

背景
----
`tools/validate_stock_model.py` 已对 `tilt(>0.45)` 做过样本内外检验，
但 `docs/落地路径与资金门槛.md` 新推荐的落地形态是 **`topk500 排名加权`**
（年化 11.90%、门槛 200 万）—— **这个配置从未独立验证过**。

若它只在样本内有效，推荐就不成立。本脚本补这一课。

口径
----
- 样本内：2019-01-01 ~ 2022-12-31
- 样本外：2023-01-01 ~ 2026-09-11
- 基准：**中证1000**（可投资指数，非等权全池）
- 配置：`topk300` / `topk500` / `tilt(>0.45)`（对照组）

用法
----
    $PY tools/validate_oos.py --bars data/stockbars/bars_all.parquet \
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

DEFAULT_TOPKS = (500, 700)

PERIODS = [("样本内 2019~2022", "20190101", "20221231"),
           ("样本外 2023~2026", "20230101", "20260911")]


def index_metrics(path, sym, start, end):
    ix = pd.read_parquet(path)
    ix["date"] = ix.date.astype(str).str.replace("-", "", regex=False)
    px = ix[(ix.code == sym) & (ix.date >= start) & (ix.date <= end)]
    px = px.set_index("date").close.sort_index()
    return metrics(px / px.iloc[0]) if len(px) > 60 else {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--indexes", default="data/indexes/indexes_all.parquet")
    ap.add_argument("--topks", default=",".join(str(t) for t in DEFAULT_TOPKS),
                    help="要检验的 topk 列表（排名加权），逗号分隔")
    ap.add_argument("--hold", type=int, default=5,
                    help="调仓周期（交易日）。§10 发现 hold=20 全面优于默认的 5")
    ap.add_argument("--min-amount", type=float, default=3e7,
                    help="20 日均成交额下限。§11 发现降到 1e7 更优")
    ap.add_argument("--out", default="results/oos_scaling.csv")
    args = ap.parse_args(argv)
    print(f"调仓周期 hold={args.hold}\n")

    configs = [(f"topk{t} 排名加权",
                dict(topk=t, weight_mode="rank", tilt_min=0.45))
               for t in (int(x) for x in args.topks.split(","))]
    configs.append(("tilt(>0.45) 对照",
                    dict(topk=50, weight_mode="tilt", tilt_min=0.45)))

    bars = pd.read_parquet(args.bars)
    bars["date"] = bars.date.astype(str).str.replace("-", "", regex=False)
    if args.universe and os.path.exists(args.universe):
        u = pd.read_csv(args.universe, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
        bars = bars[~bars.code.isin(st)]
    print(f"日线: {len(bars):,} 行, {bars.code.nunique()} 只", flush=True)
    print("计算因子…", flush=True)
    df = build_features(bars)

    rows = []
    for pname, s, e in PERIODS:
        bm = index_metrics(args.indexes, "SH000852", s, e)
        print("\n" + "=" * 104)
        print(f"{pname}（{s} ~ {e}）   基准 中证1000：年化 "
              f"{bm.get('年化收益', 0):+.2f}%  夏普 {bm.get('夏普比率', 0):.2f}  "
              f"回撤 {bm.get('最大回撤', 0):.2f}%")
        print("=" * 104)
        print("  {:<20} {:>9} {:>8} {:>7} {:>9} {:>6} {:>8} {:>11}".format(
            "配置", "年化%", "波动%", "夏普", "回撤%", "卡玛", "持仓数", "vs中证1000"))
        for cname, kw in configs:
            try:
                eq, tr, meta = run(df, ALL8, start=s, end=e, hold=args.hold,
                                   min_amount=args.min_amount, **kw)
            except Exception as ex:
                print(f"  {cname:<20} 失败: {type(ex).__name__}: {ex}", flush=True)
                continue
            m = metrics(eq.equity)
            ex_ = m.get("年化收益", 0) - bm.get("年化收益", 0)
            print("  {:<20} {:>9.2f} {:>8.2f} {:>7.2f} {:>9.2f} {:>6.2f} {:>8.0f} "
                  "{:>11.2f}".format(
                      cname, m.get("年化收益", 0), m.get("年化波动", 0),
                      m.get("夏普比率", 0), m.get("最大回撤", 0),
                      m.get("卡玛比率", 0), meta.get("avg_hold", 0), ex_), flush=True)
            rows.append({"区间": pname, "配置": cname,
                         "年化收益": m.get("年化收益", 0),
                         "夏普比率": m.get("夏普比率", 0),
                         "最大回撤": m.get("最大回撤", 0),
                         "平均持仓": meta.get("avg_hold", 0),
                         "基准年化": bm.get("年化收益", 0),
                         "vs中证1000": ex_})

    r = pd.DataFrame(rows)
    if len(r):
        print("\n" + "=" * 104)
        print("样本内外对比（看超额是否收窄）")
        print("=" * 104)
        print("  {:<20} {:>12} {:>12} {:>12} {:>12}".format(
            "配置", "样本内超额", "样本外超额", "样本内夏普", "样本外夏普"))
        for cname, _ in configs:
            a = r[(r.配置 == cname) & (r.区间.str.startswith("样本内"))]
            b = r[(r.配置 == cname) & (r.区间.str.startswith("样本外"))]
            if len(a) and len(b):
                print("  {:<20} {:>12.2f} {:>12.2f} {:>12.2f} {:>12.2f}".format(
                    cname, a.iloc[0]["vs中证1000"], b.iloc[0]["vs中证1000"],
                    a.iloc[0]["夏普比率"], b.iloc[0]["夏普比率"]))
        print("\n  判据：样本外超额若明显收窄或转负，说明该配置不可靠。")
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        r.to_csv(args.out, index=False, encoding="utf-8-sig")
        print(f"\n结果已写出 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
