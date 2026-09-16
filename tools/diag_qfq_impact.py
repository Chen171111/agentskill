"""量化「腾讯 qfq 仿射复权」对策略结论的影响：修正前后对照。

背景
----
`docs/个股线_复权口径缺陷.md`：腾讯 `qfq` 是仿射复权（`bfq = k·qfq + m`），
**不保持日收益**，池内 28.2% 的行被放大 >5%、14.9% >10%。
本脚本回答最后那个问题：**修正之后，策略结论会不会变？**

三组对照（把两处改动拆开，避免混在一起说不清）
--------------------------------------------
| 组 | 价格 | 成交额 | 说明 |
|---|---|---|---|
| A. 现状 | `qfq`（仿射） | 估算 `close×volume×100` | 目前所有结论用的就是这个 |
| B. 只修收益 | 重建总收益 | 估算（把 `amount` 置空） | 隔离「复权口径」这一项 |
| C. 全修正 | 重建总收益 | **真实成交额** | 最终应采用的口径 |

B 与 A 的差 = 复权口径的影响；C 与 B 的差 = 真实成交额的影响。

用法
----
    PY=.../python.exe
    $PY tools/diag_qfq_impact.py --qfq-bars data/stockbars/bars_all.parquet \
        --total-bars data/stockbars/bars_total.parquet \
        --universe data/stockbars/universe_all.csv --topks 800,1200 --hold 20
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import DEFAULT_FACTORS, build_features, metrics, run


def load(bars_path: str, universe_path: str, drop_amount: bool = False,
         start: str = "20190101", end: str = "20260911"):
    b = pd.read_parquet(bars_path)
    b["date"] = b.date.astype(str).str.replace("-", "", regex=False)
    if universe_path and os.path.exists(universe_path):
        u = pd.read_csv(universe_path, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
        b = b[~b.code.isin(st)]
    if drop_amount and "amount" in b.columns:
        b = b.drop(columns=["amount"])       # 让引擎回落到 close×volume×100
    return b.sort_values(["code", "date"]).reset_index(drop=True)


def factor_ic(df: pd.DataFrame, start: str, end: str, horizon: int = 20):
    """逐日横截面 IC（Spearman），返回每个因子的均值 IC。"""
    d = df[(df.date >= start) & (df.date <= end)].copy()
    d = d.sort_values(["code", "date"])
    # 前向 horizon 日收益
    d["_fwd"] = d.groupby("code", sort=False).close.shift(-horizon) / d.close - 1.0
    pool = ((d.listed >= 120) & (d.amt_ma20 >= 3e7) & (d.close >= 2.0)
            & (~d.suspended))
    d = d[pool & d._fwd.notna()]
    out = {}
    for f, _ in DEFAULT_FACTORS:
        ic = (d.groupby("date")[[f, "_fwd"]]
              .corr(method="spearman").unstack().iloc[:, 1])
        out[f] = float(ic.mean())
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="复权口径缺陷的影响量化")
    ap.add_argument("--qfq-bars", default="data/stockbars/bars_all.parquet")
    ap.add_argument("--total-bars", default="data/stockbars/bars_total.parquet")
    ap.add_argument("--universe", default="data/stockbars/universe_all.csv")
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260911")
    ap.add_argument("--topks", default="800,1200")
    ap.add_argument("--hold", type=int, default=20)
    ap.add_argument("--weight-mode", default="rank")
    ap.add_argument("--out", default="results/qfq_impact.csv")
    args = ap.parse_args(argv)
    topks = [int(x) for x in args.topks.split(",") if x.strip()]

    print("=" * 92)
    print("  腾讯 qfq 仿射复权 —— 修正前后对照")
    print("=" * 92)

    print("\n  载入数据…", flush=True)
    bars_q = load(args.qfq_bars, args.universe)
    bars_t = load(args.total_bars, args.universe)
    has_amt = "amount" in bars_t.columns

    print("  计算因子 A（qfq 现状）…", flush=True)
    dfA = build_features(bars_q)
    print("  计算因子 B（重建收益 + 估算成交额）…", flush=True)
    dfB = build_features(load(args.total_bars, args.universe, drop_amount=True))
    print("  计算因子 C（重建收益 + 真实成交额）…", flush=True)
    dfC = build_features(bars_t)

    print("\n" + "=" * 92)
    print("  一、因子 IC（Spearman，20 日前向收益）")
    print("=" * 92)
    icA = factor_ic(dfA, args.start, args.end)
    icC = factor_ic(dfC, args.start, args.end)
    print(f"  {'因子':<10}{'A: qfq':>12}{'C: 修正后':>12}{'Δ':>12}")
    for f, _ in DEFAULT_FACTORS:
        print(f"  {f:<10}{icA[f]:>12.4f}{icC[f]:>12.4f}{icC[f]-icA[f]:>+12.4f}")
    print(f"  {'|IC| 均值':<10}"
          f"{sum(abs(v) for v in icA.values())/len(icA):>12.4f}"
          f"{sum(abs(v) for v in icC.values())/len(icC):>12.4f}"
          f"{sum(abs(v) for v in icC.values())/len(icC) - sum(abs(v) for v in icA.values())/len(icA):>+12.4f}")

    print("\n" + "=" * 92)
    print(f"  二、组合表现（hold={args.hold} / 权重 {args.weight_mode}）")
    print("=" * 92)
    common = dict(start=args.start, end=args.end, hold=args.hold,
                  weight_mode=args.weight_mode, tilt_min=0.45)
    rows = []
    for tk in topks:
        for tag, dfx in (("A. qfq 现状", dfA),
                         ("B. 只修收益", dfB),
                         ("C. 全修正", dfC)):
            eq, tr, meta = run(dfx, DEFAULT_FACTORS, topk=tk, **common)
            m = metrics(eq.equity)
            rows.append({"组": tag, "topk": tk, "年化": m["年化收益"],
                         "夏普": m["夏普比率"], "最大回撤": m["最大回撤"],
                         "累计": m["累计收益"], "平均持仓": meta.get("avg_hold"),
                         "跳过调仓": meta.get("n_skip", 0)})
            print(f"  [{tag:<12}] topk={tk:<5} 年化 {m['年化收益']:>6.2f}%  "
                  f"夏普 {m['夏普比率']:.2f}  回撤 {m['最大回撤']:>7.2f}%  "
                  f"累计 {m['累计收益']:>7.2f}%", flush=True)
        a = rows[-3]
        b = rows[-2]
        c = rows[-1]
        print(f"    → 复权口径影响(B−A): 年化 {b['年化']-a['年化']:+.2f}pp  "
              f"夏普 {b['夏普']-a['夏普']:+.2f}  回撤 {b['最大回撤']-a['最大回撤']:+.2f}pp")
        print(f"    → 真实成交额影响(C−B): 年化 {c['年化']-b['年化']:+.2f}pp  "
              f"夏普 {c['夏普']-b['夏普']:+.2f}\n")

    res = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    res.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"  结果已写出 {args.out}")
    if not has_amt:
        print("  ⚠️ bars_total 里没有 amount 列，B 与 C 会相同")
    return 0


if __name__ == "__main__":
    sys.exit(main())
