"""把股息率（`dy_ttm`）作为**第 9 个因子**并入多因子模型 —— 增量检验。

背景
----
`docs/HANDOFF_项目总结与下一步.md` §八 第 4 项。

已知事实（见 `docs/个股线_股息率策略.md`、`docs/个股线_四进三出阈值检验.md`）：

| | 多因子线（8 因子） | 股息率线 |
|---|---|---|
| 信号类型 | 弱信号（IC 0.014~0.067） | 弱信号（IC **0.0605**） |
| 年单边换手 | **8.2x** | **2.9x** |
| 推荐形态 | `topk=800` / `hold=20` / 排名加权 | `N=20` / `hold=60` / `hyst 8/4` |
| 资金门槛 | 800 万 | 10 万 |

两条线的**信号强度同量级**，但**换手差 3 倍** → 理论上互补。
本脚本回答：**把 `dy_ttm` 当第 9 个因子加进去，能不能同时改善那条 15.83% 的线？**

⚠️ 必须用**口径正确**的 `bars_total.parquet`（不复权价 + 分红回放），
**不能**用 `bars_all.parquet`（腾讯 `qfq` 是仿射复权，见 `docs/个股线_复权口径缺陷.md`）。
股息率的分母用 `bars_bfq.close`（**真实市价**，不能用总收益指数）。

判据（与 `sweep_tdx_factor.py` 一致）
------------------------------------
- **年化差与夏普差同时为正**才算有增量价值
- 若夏普下降，说明它带来的是**风险**而非收益
- **必须过样本内外**（本项目铁律 4：任何参数改动都要过样本内外）

用法
----
    PY=.../python.exe
    $PY tools/sweep_dividend_into_mf.py \
        --bars data/stockbars/bars_total.parquet \
        --bfq  data/stockbars/bars_bfq.parquet \
        --dividends data/dividends/bonus_all.parquet \
        --universe data/stockbars/universe_all.csv \
        --topk 800 --hold 20 --weight-mode rank
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import build_features, fmt, metrics, run  # noqa: E402
from tools.test_dividend_factor import build_yield_panel  # noqa: E402

ALL8 = [("rev20", 1.0), ("rev60", 1.0), ("rev120", 1.0), ("rev5", 1.0),
        ("vol20", 1.0), ("max20", 1.0), ("turn20", 1.0), ("illiq20", 1.0)]
TRADING_DAYS = 244.0


def bench_metrics(df, start, end, min_listed=120, min_amount=3e7, min_price=2.0):
    rows = []
    for dt, g in df[(df.date >= start) & (df.date <= end)].groupby("date"):
        u = ((g.listed >= min_listed) & (g.amt_ma20 >= min_amount)
             & (g.close >= min_price) & (~g.suspended))
        rows.append((dt, float(g.loc[u, "ret1"].mean())))
    b = pd.DataFrame(rows, columns=["date", "r"]).set_index("date").dropna()
    return metrics((1 + b.r).cumprod())


def prepare(args) -> pd.DataFrame:
    bars = pd.read_parquet(args.bars)
    bars["date"] = bars.date.astype(str).str.replace("-", "", regex=False)
    if args.universe and os.path.exists(args.universe):
        u = pd.read_csv(args.universe, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
        bars = bars[~bars.code.isin(st)]
    print(f"  日线 {len(bars):,} 行 / {bars.code.nunique():,} 只 "
          f"({bars.date.min()} ~ {bars.date.max()})", flush=True)

    # 股息率分母必须是**真实市价**
    px = pd.read_parquet(args.bfq, columns=["code", "date", "close"])
    px["date"] = px.date.astype(str).str.replace("-", "", regex=False)
    px = px.rename(columns={"close": "px_real"})
    bars = bars.merge(px, on=["code", "date"], how="left")
    cov = bars.px_real.notna().mean()
    print(f"  并入真实价覆盖率 {cov*100:.2f}%", flush=True)
    if cov < 0.99:
        raise SystemExit("❌ 真实价覆盖率不足 99%，股息率分母不可靠")

    div = pd.read_parquet(args.dividends)
    print("  计算引擎特征…", flush=True)
    df = build_features(bars)
    print("  构建 point-in-time 股息率面板…", flush=True)
    dy = build_yield_panel(bars, div, price_col="px_real",
                           adj_mode=getattr(args, "adj_mode", "correct"))[
        ["code", "date", "dps_ttm", "dy_ttm", "n_div3"]]
    df = df.merge(dy, on=["code", "date"], how="left")
    df["dy_ttm"] = df.dy_ttm.fillna(0.0)          # 无分红 = 股息率 0（最低分位）
    return df


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="股息率并入多因子：增量检验")
    ap.add_argument("--bars", default="data/stockbars/bars_total.parquet")
    ap.add_argument("--bfq", default="data/stockbars/bars_bfq.parquet")
    ap.add_argument("--dividends", default="data/dividends/bonus_all.parquet")
    ap.add_argument("--universe", default="data/stockbars/universe_all.csv")
    ap.add_argument("--topk", type=int, default=800)
    ap.add_argument("--hold", type=int, default=20)
    ap.add_argument("--weight-mode", default="rank",
                    choices=["equal", "rank", "tilt"])
    ap.add_argument("--min-price", type=float, default=2.0)
    ap.add_argument("--min-amount", type=float, default=3e7)
    ap.add_argument("--min-listed", type=int, default=120)
    ap.add_argument("--adj-mode", default="correct", choices=["legacy", "correct"],
                    help="送转调整口径（透传给 build_yield_panel）："
                         "correct=价值中性口径（默认）；legacy=已证伪的旧实现")
    ap.add_argument("--out", default="results/dividend_into_mf.csv")
    args = ap.parse_args(argv)

    print("=" * 100)
    print("  股息率（dy_ttm）并入多因子模型：增量检验")
    print("=" * 100)
    df = prepare(args)

    periods = [("全区间", "20190101", "20260911"),
               ("样本内 2019~2022", "20190101", "20221231"),
               ("样本外 2023~2026", "20230101", "20260911")]

    # ---------- 1) dy_ttm 与现有 8 因子的相关性（是新增信息还是冗余？）----------
    print("\n" + "=" * 100)
    print("  1) dy_ttm 与现有 8 因子的横截面秩相关（全区间，逐日平均）")
    print("=" * 100)
    d = df[(df.date >= "20190101") & (df.date <= "20260911")].copy()
    pool = ((d.listed >= args.min_listed) & (d.amt_ma20 >= args.min_amount)
            & (d.close >= args.min_price) & (~d.suspended))
    d = d[pool]
    print("  " + "{:<12}{:>12}{:>10}".format("因子", "秩相关", "解读"))
    print("  " + "-" * 34)
    for f, _ in ALL8:
        sub = d[[f, "dy_ttm", "date"]].replace([np.inf, -np.inf], np.nan).dropna()
        if sub.empty:
            continue
        c = sub.groupby("date")[[f, "dy_ttm"]].corr(method="spearman") \
            .unstack().iloc[:, 1].mean()
        tag = "高度重叠" if abs(c) > 0.5 else ("中等" if abs(c) > 0.25 else "基本独立")
        print("  " + "{:<12}{:>+12.4f}{:>10}".format(f, c, tag))

    # ---------- 2) 增量价值回测 ----------
    variants = [
        ("8 因子（基线）", ALL8),
        ("8 因子 + dy(w=1)", ALL8 + [("dy_ttm", 1.0)]),
        ("8 因子 + dy(w=2)", ALL8 + [("dy_ttm", 2.0)]),
    ]
    rows = []
    for ptag, s, e in periods:
        bench = bench_metrics(df, s, e)
        print("\n" + "=" * 100)
        print(f"  2) 增量价值回测 ── {ptag}（{s}~{e}）"
              f"  topk={args.topk} hold={args.hold} {args.weight_mode}")
        print("=" * 100)
        print("  " + fmt("等权全池基准", bench))
        for tag, facs in variants:
            eq, tr, meta = run(df, facs, start=s, end=e, topk=args.topk,
                               hold=args.hold, weight_mode=args.weight_mode,
                               min_price=args.min_price,
                               min_amount=args.min_amount,
                               min_listed=args.min_listed)
            m = metrics(eq.equity)
            nh = meta.get("avg_hold", 0) or 1
            turn = (int((tr.side == "buy").sum()) / (len(eq) / TRADING_DAYS) / nh
                    if len(tr) else 0.0)
            print("  " + fmt(tag, m)
                  + f"  持仓 {nh:.0f}  换手 {turn:.2f}x  交易 {len(tr):,} 笔"
                  + (f"  ⚠️跳过 {meta['n_skip']} 次" if meta.get("n_skip") else ""),
                  flush=True)
            rows.append({"区间": ptag, "配置": tag, "年化%": m.get("年化收益", 0),
                         "波动%": m.get("年化波动", 0),
                         "夏普": m.get("夏普比率", 0),
                         "回撤%": m.get("最大回撤", 0),
                         "卡玛": m.get("卡玛比率", 0),
                         "平均持仓": nh, "年单边换手": turn,
                         "交易笔数": len(tr),
                         "超额pp": m.get("年化收益", 0) - bench.get("年化收益", 0)})
        # 区间内对比
        r = pd.DataFrame([x for x in rows if x["区间"] == ptag])
        if len(r) > 1:
            b = r.iloc[0]
            print(f"\n  相对 8 因子基线（年化 {b['年化%']:.2f}%、夏普 {b['夏普']:.2f}）：")
            print("  " + "{:<20}{:>12}{:>10}{:>12}".format(
                "配置", "年化差pp", "夏普差", "回撤差pp"))
            print("  " + "-" * 54)
            for _, x in r.iloc[1:].iterrows():
                print("  " + "{:<20}{:>+12.2f}{:>+10.2f}{:>+12.2f}".format(
                    x.配置, x["年化%"] - b["年化%"], x["夏普"] - b["夏普"],
                    x["回撤%"] - b["回撤%"]))

    r = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    r.to_csv(args.out, index=False, encoding="utf-8-sig")

    # ---------- 3) 判据 ----------
    print("\n" + "=" * 100)
    print("  3) 判据：年化差与夏普差**同时为正**才算有增量；且必须过样本内外")
    print("=" * 100)
    for ptag, _, _ in periods:
        s_ = r[r.区间 == ptag]
        if len(s_) < 2:
            continue
        b = s_.iloc[0]
        print(f"\n  {ptag}：")
        for _, x in s_.iloc[1:].iterrows():
            ok = (x["年化%"] > b["年化%"]) and (x["夏普"] > b["夏普"])
            print(f"    {x.配置:<20} 年化 {x['年化%']-b['年化%']:+.2f}pp  "
                  f"夏普 {x['夏普']-b['夏普']:+.2f}  "
                  f"→ {'✅ 有增量' if ok else '❌ 无增量/带来风险'}")
    print(f"\n  结果已写出 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
