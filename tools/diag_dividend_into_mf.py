"""股息率并入多因子 —— **深挖诊断**（在 `sweep_dividend_into_mf.py` 判出「样本外无增量」之后）

背景
----
`tools/sweep_dividend_into_mf.py` 的单点结果（`topk=800` / `hold=20` / `rank`）：

| 区间 | +dy(w=1) 年化差 | 夏普差 | 判定 |
|---|---|---|---|
| 全区间 | +0.89pp | +0.06 | ✅ |
| 样本内 2019~2022 | +1.42pp | +0.08 | ✅ |
| **样本外 2023~2026** | **−1.76pp** | **−0.04** | ❌ |

→ 典型的「样本内改善、样本外反转」。但**一个网格点不足以否决一个因子**
（铁律 3 正反两个方向都适用）。本脚本补三件事：

1. **样本外的负增量是单期拖累还是系统性？**
   —— 逐期（每个调仓窗口）增量分布（胜率 / 中位 / 最差期占比）+ 逐年。
2. **换邻域还成立吗？** —— `hold` / `weight_mode` / `topk` 邻域，各跑 基线 vs +dy。
3. **dy 到底在做什么？** —— 它同时降波动、降换手、降回撤，像「降风险」不像「提收益」；
   再跑一个 `dy` 单独的组合，看它自己的 alpha（不靠 8 因子）。

⚠️ 本脚本只做**增量对比**，同一区间内基线与变体用同一引擎同一参数 →
绝对数字的口径问题不影响差值。`超额pp` 列是相对**等权全池**（不可投资）的，
**不是**相对中证1000；别和 `docs/个股多因子模型报告.md` 的超额混用。

用法
----
    $PY tools/diag_dividend_into_mf.py \
        --bars data/stockbars/bars_total.parquet \
        --bfq  data/stockbars/bars_bfq.parquet \
        --dividends data/dividends/bonus_all.parquet \
        --universe data/stockbars/universe_all.csv
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import metrics, run  # noqa: E402
from tools.progress import flush_partial  # noqa: E402
from tools.sweep_dividend_into_mf import (  # noqa: E402
    ALL8, TRADING_DAYS, bench_metrics, prepare)

DY1 = ALL8 + [("dy_ttm", 1.0)]
DY2 = ALL8 + [("dy_ttm", 2.0)]
DY_ONLY = [("dy_ttm", 1.0)]

PERIODS = [("全区间", "20190101", "20260911"),
           ("样本内 2019~2022", "20190101", "20221231"),
           ("样本外 2023~2026", "20230101", "20260911")]

# 主配置（sweep 用的那个）
MAIN = dict(topk=800, hold=20, weight_mode="rank")
# 邻域（只跑 全区间 + 样本外，样本内不是决定性区间）
NEIGHBORS = [
    dict(tag="topk=800 hold=20 rank（主）", topk=800, hold=20, weight_mode="rank"),
    dict(tag="topk=800 hold=20 equal", topk=800, hold=20, weight_mode="equal"),
    dict(tag="topk=800 hold=10 rank", topk=800, hold=10, weight_mode="rank"),
    dict(tag="topk=800 hold=40 rank", topk=800, hold=40, weight_mode="rank"),
    dict(tag="topk=400 hold=20 rank", topk=400, hold=20, weight_mode="rank"),
    dict(tag="topk=1200 hold=20 rank", topk=1200, hold=20, weight_mode="rank"),
]
NEIGHBOR_PERIODS = [PERIODS[0], PERIODS[2]]


def run_cfg(df, facs, s, e, cfg, args):
    eq, tr, meta = run(df, facs, start=s, end=e, topk=cfg["topk"],
                       hold=cfg["hold"], weight_mode=cfg["weight_mode"],
                       min_price=args.min_price, min_amount=args.min_amount,
                       min_listed=args.min_listed)
    m = metrics(eq.equity)
    nh = meta.get("avg_hold", 0) or 1
    turn = (int((tr.side == "buy").sum()) / (len(eq) / TRADING_DAYS) / nh
            if len(tr) else 0.0)
    m.update({"平均持仓": nh, "年单边换手": turn, "交易笔数": len(tr),
              "n_skip": meta.get("n_skip", 0)})
    return eq, m


def yearly_ret(eq):
    """按自然年复利。用 `rate` 列（日收益）而不是净值切片，避免漏掉每年首日。"""
    r = eq.rate.copy()
    r.index = pd.to_datetime(r.index, format="%Y%m%d")
    return (1 + r).groupby(r.index.year).prod() - 1


def window_diff(eq_b, eq_v, hold):
    """按调仓窗口切，返回每个窗口的「变体 − 基线」收益差（小数）。"""
    rb = eq_b.rate.values
    rv = eq_v.rate.values
    n = min(len(rb), len(rv)) // hold
    out = []
    for i in range(n):
        a, b = i * hold, (i + 1) * hold
        out.append(float((1 + rv[a:b]).prod() - (1 + rb[a:b]).prod()))
    return np.array(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="股息率并入多因子：深挖诊断")
    ap.add_argument("--bars", default="data/stockbars/bars_total.parquet")
    ap.add_argument("--bfq", default="data/stockbars/bars_bfq.parquet")
    ap.add_argument("--dividends", default="data/dividends/bonus_all.parquet")
    ap.add_argument("--universe", default="data/stockbars/universe_all.csv")
    ap.add_argument("--min-price", type=float, default=2.0)
    ap.add_argument("--min-amount", type=float, default=3e7)
    ap.add_argument("--min-listed", type=int, default=120)
    # ⚠️ 2026-09-17 补：本脚本原先**没有** --adj-mode。
    # 它走 `sweep_dividend_into_mf.prepare`，而那里是
    # `adj_mode=getattr(args, "adj_mode", "correct")` —— args 没这个属性时
    # **静默**落到 correct → 只能跑新口径，**无法复现 legacy 的旧数字**，
    # 而 `docs/个股线_股息率并入多因子.md` 的横幅写着「加 --adj-mode legacy 可复现」。
    # 加这个参数才让那句说明成立（纯透传，不改变任何默认行为）。
    ap.add_argument("--adj-mode", default=None, choices=["legacy", "correct"],
                    help="送转调整口径（透传给 build_yield_panel）："
                         "correct=价值中性口径（默认）；legacy=已证伪的旧实现")
    ap.add_argument("--out", default="results/dividend_into_mf_diag.csv")
    ap.add_argument("--out-window", default="results/dividend_into_mf_windows.csv")
    args = ap.parse_args(argv)

    print("=" * 104)
    print("  股息率并入多因子 —— 深挖诊断")
    print("=" * 104, flush=True)
    df = prepare(args)

    rows, win_rows = [], []

    # ================= 1) 主配置：逐期 + 逐年 =================
    print("\n" + "=" * 104)
    print("  1) 主配置（topk=800 / hold=20 / rank）：逐年 + 逐调仓窗口增量")
    print("=" * 104, flush=True)
    for ptag, s, e in PERIODS:
        bench = bench_metrics(df, s, e)
        eqs = {}
        for tag, facs in [("8 因子（基线）", ALL8), ("+dy(w=1)", DY1),
                          ("+dy(w=2)", DY2), ("dy 单独", DY_ONLY)]:
            eq, m = run_cfg(df, facs, s, e, MAIN, args)
            eqs[tag] = eq
            rows.append({"组": "主配置", "配置": tag, "区间": ptag,
                         "年化%": m.get("年化收益", 0), "波动%": m.get("年化波动", 0),
                         "夏普": m.get("夏普比率", 0), "回撤%": m.get("最大回撤", 0),
                         "平均持仓": m["平均持仓"], "年单边换手": m["年单边换手"],
                         "交易笔数": m["交易笔数"], "n_skip": m["n_skip"],
                         "超额pp": m.get("年化收益", 0) - bench.get("年化收益", 0)})
            print(f"  [{ptag}] {tag:<14} 年化 {m['年化收益']:>7.2f}%  "
                  f"夏普 {m['夏普比率']:>5.2f}  回撤 {m['最大回撤']:>7.2f}%  "
                  f"波动 {m['年化波动']:>6.2f}%  换手 {m['年单边换手']:.2f}x", flush=True)

        # 逐年
        yb = yearly_ret(eqs["8 因子（基线）"])
        y1 = yearly_ret(eqs["+dy(w=1)"])
        y2 = yearly_ret(eqs["+dy(w=2)"])
        print(f"\n  [{ptag}] 逐年收益（%）")
        print("  " + "{:<8}{:>10}{:>10}{:>10}{:>12}{:>12}".format(
            "年度", "基线", "+dy(w=1)", "+dy(w=2)", "w=1 增量", "w=2 增量"))
        print("  " + "-" * 62)
        for y in yb.index:
            d1 = (y1.get(y, np.nan) - yb[y]) * 100
            d2 = (y2.get(y, np.nan) - yb[y]) * 100
            print("  " + "{:<8}{:>10.2f}{:>10.2f}{:>10.2f}{:>+12.2f}{:>+12.2f}".format(
                y, yb[y] * 100, y1.get(y, np.nan) * 100, y2.get(y, np.nan) * 100, d1, d2))

        # 逐调仓窗口
        for vtag, veq in [("+dy(w=1)", eqs["+dy(w=1)"]), ("+dy(w=2)", eqs["+dy(w=2)"])]:
            d = window_diff(eqs["8 因子（基线）"], veq, MAIN["hold"])
            d = d[1:]                      # 去掉建仓首窗
            worst_share = (d.min() / d.sum()) if d.sum() != 0 else np.nan
            print(f"\n  [{ptag}] {vtag} 逐调仓窗口增量（{len(d)} 窗，每窗 {MAIN['hold']} 日）")
            print(f"    均值 {d.mean()*100:+.2f}pp  中位 {np.median(d)*100:+.2f}pp  "
                  f"胜率 {(d > 0).mean()*100:.1f}%  最好 {d.max()*100:+.2f}pp  "
                  f"最差 {d.min()*100:+.2f}pp")
            print(f"    最差单窗占全部增量的 {worst_share*100:.0f}%"
                  f"（>100% = 单期拖累放大，剔除后仍为负）")
            win_rows.append({"区间": ptag, "配置": vtag, "窗口数": len(d),
                             "均值pp": d.mean() * 100, "中位pp": np.median(d) * 100,
                             "胜率%": (d > 0).mean() * 100,
                             "最好pp": d.max() * 100, "最差pp": d.min() * 100,
                             "最差占比%": worst_share * 100})
        # 增量落盘：跑完一个区间就把主配置 + 逐窗口诊断写盘（R8 要跑 40 分钟，最需要）
        flush_partial(rows, args.out, tag=ptag)
        flush_partial(win_rows, args.out_window, tag=ptag, quiet=True)

    # ================= 2) 邻域 =================
    print("\n" + "=" * 104)
    print("  2) 邻域扫描：基线 vs +dy(w=1)（只跑 全区间 + 样本外）")
    print("=" * 104, flush=True)
    print("  " + "{:<28}{:<18}{:>10}{:>10}{:>9}{:>9}{:>10}".format(
        "邻域", "区间", "基线年化", "+dy年化", "年化差", "夏普差", "回撤差"))
    print("  " + "-" * 94)
    for cfg in NEIGHBORS:
        for ptag, s, e in NEIGHBOR_PERIODS:
            _, mb = run_cfg(df, ALL8, s, e, cfg, args)
            _, mv = run_cfg(df, DY1, s, e, cfg, args)
            rows.append({"组": "邻域", "配置": cfg["tag"], "区间": ptag,
                         "年化%": mv.get("年化收益", 0), "波动%": mv.get("年化波动", 0),
                         "夏普": mv.get("夏普比率", 0), "回撤%": mv.get("最大回撤", 0),
                         "平均持仓": mv["平均持仓"], "年单边换手": mv["年单边换手"],
                         "交易笔数": mv["交易笔数"], "n_skip": mv["n_skip"],
                         "超额pp": np.nan})
            rows.append({"组": "邻域(基线)", "配置": cfg["tag"], "区间": ptag,
                         "年化%": mb.get("年化收益", 0), "波动%": mb.get("年化波动", 0),
                         "夏普": mb.get("夏普比率", 0), "回撤%": mb.get("最大回撤", 0),
                         "平均持仓": mb["平均持仓"], "年单边换手": mb["年单边换手"],
                         "交易笔数": mb["交易笔数"], "n_skip": mb["n_skip"],
                         "超额pp": np.nan})
            da = mv.get("年化收益", 0) - mb.get("年化收益", 0)
            ds = mv.get("夏普比率", 0) - mb.get("夏普比率", 0)
            dd = mv.get("最大回撤", 0) - mb.get("最大回撤", 0)
            flag = "✅" if (da > 0 and ds > 0) else "❌"
            print("  " + "{:<28}{:<18}{:>10.2f}{:>10.2f}{:>+10.2f}{:>+9.2f}"
                  "{:>+10.2f}  {}".format(cfg["tag"], ptag,
                                          mb.get("年化收益", 0), mv.get("年化收益", 0),
                                          da, ds, dd, flag), flush=True)

    pd.DataFrame(rows).to_csv(args.out, index=False, encoding="utf-8-sig")
    pd.DataFrame(win_rows).to_csv(args.out_window, index=False, encoding="utf-8-sig")
    print(f"\n  结果已写出 {args.out} / {args.out_window}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
