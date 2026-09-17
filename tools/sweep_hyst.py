"""「四进三出」滞回阈值扫描 —— 确认 `hyst` 的改善是**平台**还是**刀尖**。

背景
----
2026-09-16 复核发现：`tools/backtest_dividend.py` 的 `hyst` 模式在**默认参数下
完全退化成 `topn`** —— 24 个配置（3 过滤 × 4 个 N × 2 模式）8 项指标逐行相同、
最大绝对差 **0.0**。原因是候选集恒 ≥ N，滞回阈值没有约束力。

把阈值调高让它生效（`--entry 8 --exit 6`）后，`hyst` 明显更好：

| 区间 | `topn` | `hyst`（阈值生效） | Δ |
|---|---|---|---|
| 样本内 2019~2022 | 19.29% | 20.54% | +1.25pp |
| 样本外 2023~2026 | 8.59% | **10.89%** | **+2.30pp** |

但 `entry=8 / exit=6` 是**看到全区间结果后才挑的** → 单点参数不算数。
本脚本扫 `entry × exit` 邻域，回答两件事：

1. 是不是**平台**（邻域内普遍改善）还是**刀尖**（只有那一个点好）；
2. 阈值**是否真的生效**（看「平均持仓」是否 < N）。

⚠️ 本项目铁律 3：**网格太稀疏会把中间点误判为峰值** —— 故必须补邻域。

用法
----
    PY=.../python.exe
    $PY tools/sweep_hyst.py \
        --bars data/stockbars/bars_total.parquet \
        --bfq  data/stockbars/bars_bfq.parquet \
        --dividends data/dividends/bonus_all.parquet \
        --universe data/stockbars/universe_all.csv \
        --topn 20 --hold 60 --max-dy 10 --min-div3 2 \
        --entries 5 6 7 8 9 10 --exits 3 4 5 6 7 8 \
        --out results/hyst_sweep.csv
"""
from __future__ import annotations

import argparse
import os
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_dividend import build_mask, prepare as prepare_div  # noqa: E402
from tools.backtest_stock import metrics, run  # noqa: E402

TRADING_DAYS = 244.0
MIN_COMMISSION = 5.0
NOMINAL_FEE = 0.0005
SLIP = 0.0005
STAMP = 0.001
MODELED_ROUND = NOMINAL_FEE + (NOMINAL_FEE + STAMP) + SLIP * 2


def real_ann(ann_pct: float, turnover: float, avg_hold: float,
             cap_wan: float) -> float:
    ticket = cap_wan * 1e4 / max(avg_hold, 1.0)
    comm = max(NOMINAL_FEE, MIN_COMMISSION / ticket)
    real_round = (comm + SLIP) + (comm + STAMP + SLIP)
    return ann_pct - turnover * (real_round - MODELED_ROUND) * 100


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="hyst 滞回阈值扫描")
    ap.add_argument("--bars", default="data/stockbars/bars_total.parquet")
    ap.add_argument("--bfq", default="data/stockbars/bars_bfq.parquet")
    ap.add_argument("--dividends", default="data/dividends/bonus_all.parquet")
    ap.add_argument("--universe", default="data/stockbars/universe_all.csv")
    ap.add_argument("--topn", type=int, default=20)
    ap.add_argument("--hold", type=int, default=60)
    ap.add_argument("--dy-col", default="dy_ttm", choices=["dy_ttm", "dy_fwd"])
    ap.add_argument("--min-dy", type=float, default=0.5)
    ap.add_argument("--max-dy", type=float, default=10.0)
    ap.add_argument("--min-div3", type=int, default=2)
    ap.add_argument("--entries", type=float, nargs="+",
                    default=[6, 7, 8, 9])
    ap.add_argument("--exits", type=float, nargs="+",
                    default=[4, 5, 6, 7])
    ap.add_argument("--min-price", type=float, default=2.0)
    ap.add_argument("--min-amount", type=float, default=3e7)
    ap.add_argument("--min-listed", type=int, default=120)
    ap.add_argument("--adj-mode", default="correct", choices=["legacy", "correct"],
                    help="送转调整口径（透传给 build_yield_panel）："
                         "correct=价值中性口径（默认）；legacy=已证伪的旧实现")
    ap.add_argument("--ctrl-only", action="store_true",
                    help="只跑「纯门槛 top-N」对照（不跑 hyst 网格），用于快速分离"
                         "「门槛效应」与「滞回效应」")
    ap.add_argument("--out", default="results/hyst_sweep.csv")
    args = ap.parse_args(argv)

    print("=" * 100)
    print("  「四进三出」滞回阈值扫描")
    print("=" * 100)

    ns = SimpleNamespace(bars=args.bars, bfq=args.bfq, dividends=args.dividends,
                         universe=args.universe, min_listed=args.min_listed,
                         min_amount=args.min_amount, min_price=args.min_price,
                         adj_mode=args.adj_mode)
    df = prepare_div(ns)

    periods = [("全区间", "20190101", "20260911"),
               ("样本内 2019~2022", "20190101", "20221231"),
               ("样本外 2023~2026", "20230101", "20260911")]
    all_dates = sorted(df.date.unique())
    by_date = {t: v for t, v in df.groupby("date", sort=False).indices.items()}
    df["_sig"] = False

    combos = [("topn", None, None)]
    for e in args.entries:
        # ── 对照：纯「最低股息率门槛」的 top-N ──
        # `hyst` 的候选集是 {dy≥entry} ∪ {已持有且 dy≥exit}。
        # 如果 |{dy≥entry}| 本来就 ≥ N，那 hyst 就等于「min_dy=entry 的 top-N」——
        # 此时改善来自**门槛提高**（筛掉了低质高息股），而不是**滞回**。
        # 必须跑这个对照才能把两个效应分开。
        combos.append(("topn_min", e, None))
        if args.ctrl_only:
            continue
        for x in args.exits:
            if x < e:
                combos.append(("hyst", e, x))
    print(f"  配置数 {len(combos)} × {len(periods)} 个区间 = "
          f"{len(combos)*len(periods)} 次回测\n")

    rows = []
    for ptag, s, e_ in periods:
        dates = [t for t in all_dates if s <= t <= e_]
        print(f"  ── {ptag}（{s}~{e_}，{len(dates)} 交易日）" + "─" * 30)
        print("  " + "{:<18}{:>9}{:>8}{:>9}{:>9}{:>8}{:>10}".format(
            "配置", "年化%", "夏普", "回撤%", "平均持仓", "换手", "10万%"))
        print("  " + "-" * 71)
        for mode, ent, ex in combos:
            if mode == "topn":
                tag = "topn N=%d" % args.topn
            elif mode == "topn_min":
                tag = "topn 门槛%g" % ent
            else:
                tag = "hyst %g/%g" % (ent, ex)
            if mode == "topn_min":
                mask = build_mask(df, dates, by_date, mode="topn",
                                  topn=args.topn, hold=args.hold,
                                  dy_col=args.dy_col, entry=4.0, exit_=None,
                                  min_dy=ent, max_dy=args.max_dy,
                                  min_div3=args.min_div3)
            else:
                mask = build_mask(df, dates, by_date, mode=mode, topn=args.topn,
                                  hold=args.hold, dy_col=args.dy_col,
                                  entry=ent or 4.0, exit_=ex,
                                  min_dy=args.min_dy, max_dy=args.max_dy,
                                  min_div3=args.min_div3)
            df["_sig"] = mask
            eq, tr, meta = run(df, [], start=s, end=e_, hold=args.hold,
                               cond_col="_sig", min_price=args.min_price,
                               min_amount=args.min_amount,
                               min_listed=args.min_listed)
            m = metrics(eq.equity)
            yrs = len(eq) / TRADING_DAYS
            nh = meta.get("avg_hold", 0) or 1
            turn = (int((tr.side == "buy").sum()) / yrs / nh) if len(tr) else 0.0
            real = real_ann(m["年化收益"], turn, nh, 10.0)
            rows.append({"区间": ptag, "模式": mode, "entry": ent, "exit": ex,
                         "年化%": m["年化收益"], "夏普": m["夏普比率"],
                         "回撤%": m["最大回撤"], "平均持仓": nh,
                         "年单边换手": turn, "交易笔数": len(tr),
                         "10万真实年化%": real})
            print("  " + "{:<18}{:>9.2f}{:>8.2f}{:>9.2f}{:>9.1f}{:>8.2f}{:>10.2f}".format(
                tag, m["年化收益"], m["夏普比率"], m["最大回撤"], nh, turn, real),
                flush=True)
        print()

    r = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    r.to_csv(args.out, index=False, encoding="utf-8-sig")

    # ---- 网格：年化% 随 (entry, exit) 的分布 ----
    for ptag, _, _ in periods:
        h = r[(r.区间 == ptag) & (r.模式 == "hyst")]
        base = r[(r.区间 == ptag) & (r.模式 == "topn")]
        b = float(base["年化%"].iloc[0]) if len(base) else float("nan")
        if not h.empty:
            piv = h.pivot_table(index="entry", columns="exit", values="年化%")
            print(f"  【{ptag}】hyst 年化%（行=entry，列=exit；空白=exit≥entry）")
            print("  " + piv.to_string(float_format=lambda x: f"{x:>7.2f}"))
            # ⚠️ 「阈值是否生效」不能用「平均持仓 < N」判定 ——
            # 实测 `entry=8/exit=4` 在样本外平均持仓仍是 **20.0**，
            # 但年化 11.35% ≠ 基线 8.59% → 掩码确实变了
            # （keep 把已持有的股票留在候选里，挤掉了别的）。
            # 正确判据：**结果与 topn 基线不同**。
            bound = h[h["年化%"].sub(b).abs() > 1e-9]
            print(f"  topn 基线 {b:.2f}% ｜ hyst {len(h)} 个点中 "
                  f"{int((h['年化%'] > b).sum())} 个优于基线")
            print(f"  阈值**真正生效**（年化 ≠ 基线）的点：{len(bound)}/{len(h)}")
            if len(bound):
                print(f"    其中优于基线的：{int((bound['年化%'] > b).sum())}/{len(bound)}"
                      f"  中位年化 {bound['年化%'].median():.2f}%"
                      f"  中位夏普 {bound['夏普'].median():.2f}")
        # ── 门槛对照：纯「min_dy 门槛 + top-N」 ──
        mn = r[(r.区间 == ptag) & (r.模式 == "topn_min")]
        if len(mn):
            print(f"\n  【{ptag}】对照：纯门槛 top-N vs hyst(exit=4)"
                  f"（分离「门槛效应」与「滞回效应」）")
            print("  " + "{:<16}{:>11}{:>12}{:>12}".format(
                "entry", "纯门槛年化%", "hyst(exit4)%", "滞回增量pp"))
            print("  " + "-" * 51)
            for _, row in mn.sort_values("entry").iterrows():
                hh = h[(h.entry == row.entry) & (h.exit == 4.0)]
                hv = float(hh["年化%"].iloc[0]) if len(hh) else float("nan")
                print("  " + "{:<16}{:>11.2f}{:>12.2f}{:>+12.2f}".format(
                    f"min_dy={row.entry:g}", row["年化%"], hv, hv - row["年化%"]))
        print()

    print("=" * 100)
    print("  判据：邻域内普遍优于基线 → 平台；只有个别点 → 刀尖（不可采用）")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
