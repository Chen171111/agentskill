"""股息率因子有效性检验 —— 「强信号 × 低频」方向的第一步。

背景
----
`docs/HANDOFF_项目总结与下一步.md` 第零节：现有因子集是「**弱信号 × 高换手**」
（IC 仅 0.05~0.07、年单边换手 8 倍），靠广度赚钱 → 需要几千只持仓 → 10 万做不了。
主人举的高股息反例（聚宽「四进三出」：股息率 >4% 买、<3% 卖）指出正确方向是
「**强信号 × 低频**」，10~30 只持仓在 10 万下可行。

本脚本检验：**股息率在 A 股到底有没有 alpha？有多强？**

两个口径（都要测）
------------------
| 口径 | 定义 | 用途 |
|---|---|---|
| `dy_ttm` | 过去 12 个月**已除权**的每股现金分红 ÷ 当日价 | 经典「历史股息率」，完全 point-in-time |
| `dy_fwd` | 过去 12 个月**已公告**（按 `NOTICE_DATE`）的每股分红 ÷ 当日价 | 「已知股息率」——信息可用日比除权日**早几周**，更接近实盘能看到的数 |

⚠️ **两个都必须 point-in-time**：`dy_ttm` 只用 `EX_DIVIDEND_DATE <= t`、
`dy_fwd` 只用 `NOTICE_DATE <= t`。用未来分红算历史股息率是最常见的隐性前视。

⚠️ **`dy_fwd` 的第一版定义是错的**（本次踩过）：一开始写成「已公告**且尚未除权**」
（`NOTICE_DATE <= t < EX_DIVIDEND_DATE`）—— 但这个窗口只有公告到除权之间的一两周，
**只有 1.6% 的行非零**，IC 被大量 0 稀释成 0.003，看着像"因子无效"。
**→ 因子覆盖面太窄时，先怀疑定义，别急着下"无效"的结论。**

⚠️ **送转调整**：分红数据里的「每 10 股派息」是**当时**的股本口径。
若之后发生送转，1 股变成 (1+r) 股，历史 DPS 要按累计送转因子放大到**当前股本口径**，
否则与当前价不可比。

用法
----
    PY=.../python.exe
    $PY tools/test_dividend_factor.py \
        --bars data/stockbars/bars_total.parquet \
        --dividends data/dividends/bonus_all.parquet \
        --universe data/stockbars/universe_all.csv \
        --horizons 20 60 120 --out results/dividend_factor_ic.csv
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _num(x) -> float:
    """NaN 安全取数（`nan or 0` 会返回 nan —— NaN 在 Python 里是真值）。"""
    return 0.0 if pd.isna(x) else float(x)


def build_yield_panel(bars: pd.DataFrame, div: pd.DataFrame,
                      price_col: str = "close") -> pd.DataFrame:
    """给长表加上 dy_ttm / dy_fwd / dps_ttm 列。

    ⚠️ **`price_col` 必须是「真实市价」，不能用总收益指数**。
    股息率 = 每股分红 ÷ **当前市价**，分母是投资者真正付出去的钱。
    若用 `bars_total.close`（总收益指数，历史区间比真实价低约 14%），
    会**系统性高估历史股息率**，且高估幅度与股票的分红历史相关 ——
    等于把「长期分红多」混进「当前股息率高」。
    → 请传入 `bars_bfq.close`（不复权真实价）。
    """
    bars = bars.sort_values(["code", "date"]).reset_index(drop=True)
    if price_col not in bars.columns:
        raise KeyError(f"缺少价格列 {price_col}")
    d = div.copy()
    d = d[d.EX_DIVIDEND_DATE.notna()]
    if "ASSIGN_PROGRESS" in d.columns:
        d = d[d.ASSIGN_PROGRESS.astype(str).str.contains("实施", na=False)]
    d["D"] = pd.to_numeric(d.PRETAX_BONUS_RMB, errors="coerce").fillna(0) / 10.0
    d["r"] = pd.to_numeric(d.BONUS_IT_RATIO, errors="coerce").fillna(0) / 10.0
    d = d[(d.D > 0) | (d.r > 0)]

    div_by_code = {c: g.sort_values("EX_DIVIDEND_DATE")
                   for c, g in d.groupby("code", sort=False)}

    n = len(bars)
    dps_ttm = np.zeros(n)
    dps_fwd = np.zeros(n)
    n_div3 = np.zeros(n)          # 过去 3 个自然年内的现金分红次数（持续性代理）
    # 整表算一次「t − 365 天」，别在每股循环里重复 to_datetime（9M 行只做一次）
    d365 = _shift_days(bars.date.values, -365)
    d3y = _shift_days(bars.date.values, -1095)
    for code, idx in bars.groupby("code", sort=False).indices.items():
        g = div_by_code.get(code)
        dates = bars.date.values[idx]
        if g is None or g.empty:
            continue
        ex = g.EX_DIVIDEND_DATE.values
        D = g.D.values
        r = g.r.values
        # 累计送转因子：把历史 DPS 换算到「当前股本口径」
        # adj[i] = Π_{j>i} (1 + r_j)
        adj = np.ones(len(D))
        acc = 1.0
        for i in range(len(D) - 1, -1, -1):
            adj[i] = acc
            acc *= (1.0 + r[i])
        Dps = D * adj
        cs = np.concatenate([[0.0], np.cumsum(Dps)])

        # --- dy_ttm：过去 365 天内已除权的分红 ---
        hi = np.searchsorted(ex, dates, side="right")           # ex <= t
        lo = np.searchsorted(ex, d365[idx], side="right")       # ex <= t-365
        dps_ttm[idx] = cs[hi] - cs[lo]
        # 持续性代理：过去 3 年内**现金分红**的次数（送转不算）
        cash = D > 0
        n_div3[idx] = (np.searchsorted(ex[cash], dates, side="right")
                       - np.searchsorted(ex[cash], d3y[idx], side="right"))

        # --- dy_fwd：过去 365 天内**已公告**的分红（信息可用日 = NOTICE_DATE）---
        # 不能用 `NOTICE_DATE <= t < EX_DIVIDEND_DATE`：那个窗口只有一两周，
        # 只有 1.6% 的行非零，IC 被 0 稀释成 0.003（第一版就是这么写错的）。
        nt = None
        if "NOTICE_DATE" in g.columns:
            nt = pd.to_datetime(g.NOTICE_DATE, format="%Y%m%d",
                                errors="coerce").dt.strftime("%Y%m%d").values
        if nt is not None and np.any(pd.notna(nt)):
            # 缺失 NOTICE_DATE 的用除权日兜底（公告不可能晚于除权）
            nt_eff = np.where(pd.isna(nt) | (nt == ""), ex, nt)
            order = np.argsort(nt_eff)
            cs_nt = np.concatenate([[0.0], np.cumsum(Dps[order])])
            hi = np.searchsorted(nt_eff[order], dates, side="right")
            lo = np.searchsorted(nt_eff[order], d365[idx], side="right")
            dps_fwd[idx] = cs_nt[hi] - cs_nt[lo]

    out = bars.copy()
    out["dps_ttm"] = dps_ttm
    out["dps_fwd"] = dps_fwd
    out["n_div3"] = n_div3
    px = out[price_col].replace(0, np.nan)
    out["dy_ttm"] = dps_ttm / px * 100.0          # %
    out["dy_fwd"] = dps_fwd / px * 100.0          # %
    return out


def _shift_days(dates: np.ndarray, days: int) -> np.ndarray:
    """把 YYYYMMDD 字符串数组平移若干**日历日**（近似交易日区间）。

    ⚠️ `pd.to_datetime(ndarray)` 返回 **DatetimeIndex**，没有 `.dt` 访问器 ——
    直接 `.strftime()` 即可。
    """
    s = pd.to_datetime(pd.Series(dates), format="%Y%m%d") + pd.Timedelta(days=days)
    return s.dt.strftime("%Y%m%d").values


def ic_table(df: pd.DataFrame, factors: list[str], horizons: list[int],
             start: str, end: str, min_price: float, min_amount: float,
             min_listed: int) -> pd.DataFrame:
    d = df[(df.date >= start) & (df.date <= end)].copy()
    d = d.sort_values(["code", "date"])
    g = d.groupby("code", sort=False)
    pool = ((d.listed >= min_listed) & (d.amt_ma20 >= min_amount)
            & (d.close >= min_price) & (~d.suspended))
    rows = []
    for h in horizons:
        d["_fwd"] = g.close.shift(-h) / d.close - 1.0
        sub = d[pool & d._fwd.notna()]
        for f in factors:
            s = sub[[f, "_fwd"]].dropna()
            if len(s) < 1000:
                rows.append({"因子": f, "持有期": h, "IC": np.nan, "t值": np.nan,
                             "样本": len(s)})
                continue
            ic = (sub.groupby("date")[[f, "_fwd"]]
                  .corr(method="spearman").unstack().iloc[:, 1].dropna())
            rows.append({"因子": f, "持有期": h, "IC": float(ic.mean()),
                         "t值": float(ic.mean() / ic.std() * np.sqrt(len(ic)))
                         if ic.std() > 0 else np.nan,
                         "样本": len(s), "天数": len(ic)})
    return pd.DataFrame(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="股息率因子 IC 检验")
    ap.add_argument("--bars", default="data/stockbars/bars_total.parquet")
    ap.add_argument("--bfq", default="data/stockbars/bars_bfq.parquet",
                    help="不复权真实价 —— 股息率的分母（**必须提供**）")
    ap.add_argument("--dividends", default="data/dividends/bonus_all.parquet")
    ap.add_argument("--universe", default="data/stockbars/universe_all.csv")
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260911")
    ap.add_argument("--horizons", type=int, nargs="+", default=[20, 60, 120])
    ap.add_argument("--min-price", type=float, default=2.0)
    ap.add_argument("--min-amount", type=float, default=3e7)
    ap.add_argument("--min-listed", type=int, default=120)
    ap.add_argument("--out", default="results/dividend_factor_ic.csv")
    args = ap.parse_args(argv)

    print("=" * 88)
    print("  股息率因子有效性检验")
    print("=" * 88)

    b = pd.read_parquet(args.bars)
    b["date"] = b.date.astype(str).str.replace("-", "", regex=False)
    if args.universe and os.path.exists(args.universe):
        u = pd.read_csv(args.universe, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
        b = b[~b.code.isin(st)]
    print(f"  日线 {len(b):,} 行 / {b.code.nunique():,} 只")

    div = pd.read_parquet(args.dividends)
    print(f"  分红 {len(div):,} 条 / {div.code.nunique():,} 只")

    # ⚠️ 股息率分母必须用**真实市价**。用 bars_total 的 close（总收益指数）
    # 会把历史股息率系统性高估 —— 实测曾因此跑出「N=20 年化 25.71%」的假结论，
    # 换成真实价后只有 6.82%。
    price_col = "close"
    if args.bfq and os.path.exists(args.bfq):
        px = pd.read_parquet(args.bfq, columns=["code", "date", "close"])
        px["date"] = px.date.astype(str).str.replace("-", "", regex=False)
        px = px.rename(columns={"close": "px_real"})
        b = b.merge(px, on=["code", "date"], how="left")
        cov = b.px_real.notna().mean()
        print(f"  并入真实价覆盖率 {cov*100:.2f}%")
        if cov < 0.99:
            print("  ❌ 真实价覆盖率不足 99%，股息率分母不可靠，终止")
            return 2
        price_col = "px_real"
    else:
        print("  ⚠️ 未提供 --bfq，分母退回总收益指数（会高估股息率）")

    print("  构建 point-in-time 股息率面板…", flush=True)
    df = build_yield_panel(b, div, price_col=price_col)

    # 池子掩码所需列（与引擎一致）
    g = df.groupby("code", sort=False)
    df["amt"] = df.amount.astype(float).fillna(df.close * df.volume * 100.0) \
        if "amount" in df.columns else df.close * df.volume * 100.0
    df["amt_ma20"] = g.amt.transform(lambda s: s.rolling(20).mean())
    df["listed"] = g.cumcount() + 1
    df["suspended"] = df.volume.fillna(0) <= 0

    d = df[(df.date >= args.start) & (df.date <= args.end)]
    pool = ((d.listed >= args.min_listed) & (d.amt_ma20 >= args.min_amount)
            & (d.close >= args.min_price) & (~d.suspended))
    p = d[pool]
    print(f"\n  池内 {len(p):,} 行 / {p.code.nunique():,} 只")
    print(f"  dy_ttm  >0 的行: {int((p.dy_ttm > 0).sum()):,} "
          f"({(p.dy_ttm > 0).mean()*100:.1f}%)   中位（>0 部分）"
          f" {p.loc[p.dy_ttm > 0, 'dy_ttm'].median():.2f}%")
    print(f"  dy_ttm > 3%: {int((p.dy_ttm > 3).sum()):,} 行   "
          f"> 4%: {int((p.dy_ttm > 4).sum()):,} 行   "
          f"> 5%: {int((p.dy_ttm > 5).sum()):,} 行")
    print(f"  dy_fwd > 0 的行: {int((p.dy_fwd > 0).sum()):,} "
          f"({(p.dy_fwd > 0).mean()*100:.1f}%)")

    print("\n" + "=" * 88)
    print("  一、IC（Spearman，全区间）")
    print("=" * 88)
    t = ic_table(df, ["dy_ttm", "dy_fwd"], args.horizons, args.start, args.end,
                 args.min_price, args.min_amount, args.min_listed)
    print(t.to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print("\n" + "=" * 88)
    print("  二、分年度 IC（持有期 60 日）")
    print("=" * 88)
    yrs = sorted(df.date.str[:4].unique())
    rows = []
    for y in yrs:
        s, e = f"{y}0101", f"{y}1231"
        if e < args.start or s > args.end:
            continue
        tt = ic_table(df, ["dy_ttm", "dy_fwd"], [60], max(s, args.start),
                      min(e, args.end), args.min_price, args.min_amount,
                      args.min_listed)
        tt["年度"] = y
        rows.append(tt)
    if rows:
        yy = pd.concat(rows, ignore_index=True)
        piv = yy.pivot_table(index="年度", columns="因子", values="IC")
        print(piv.to_string(float_format=lambda x: f"{x:.4f}"))
        pos = {c: int((piv[c] > 0).sum()) for c in piv.columns}
        print(f"\n  符号为正的年份数：{pos}  （共 {len(piv)} 年）")

    print("\n" + "=" * 88)
    print("  三、样本内外切分（持有期 60 日）")
    print("=" * 88)
    for tag, s, e in (("样本内 2019~2022", "20190101", "20221231"),
                      ("样本外 2023~2026", "20230101", "20260911")):
        tt = ic_table(df, ["dy_ttm", "dy_fwd"], [60], s, e, args.min_price,
                      args.min_amount, args.min_listed)
        tt["区间"] = tag
        print(tt.to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print("\n" + "=" * 88)
    print("  四、分档前向收益（dy_ttm，按日横截面十分位，持有期 60 日）")
    print("=" * 88)
    dd = df[(df.date >= args.start) & (df.date <= args.end)].copy()
    dd = dd.sort_values(["code", "date"])
    dd["_fwd"] = dd.groupby("code", sort=False).close.shift(-60) / dd.close - 1.0
    po = ((dd.listed >= args.min_listed) & (dd.amt_ma20 >= args.min_amount)
          & (dd.close >= args.min_price) & (~dd.suspended)
          & dd.dy_ttm.notna() & dd._fwd.notna())
    dd = dd[po]
    # 只在「有分红」的股票里分档，避免 0 分红把底档稀释
    dd = dd[dd.dy_ttm > 0]
    dd["_dec"] = dd.groupby("date").dy_ttm.transform(
        lambda s: pd.qcut(s, 10, labels=False, duplicates="drop"))
    gb = dd.groupby("_dec")._fwd.agg(["mean", "count"])
    gb.index = [f"D{int(i)+1}" for i in gb.index]
    gb["年化%"] = ((1 + gb["mean"]) ** (244 / 60) - 1) * 100
    print(gb.to_string(float_format=lambda x: f"{x:.4f}"))
    top, bot = gb["年化%"].iloc[-1], gb["年化%"].iloc[0]
    print(f"\n  顶档(D10) − 底档(D1) = {top - bot:+.2f}pp/年")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    t.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"\n  结果已写出 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
