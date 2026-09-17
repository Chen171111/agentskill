"""股息率策略：行业中性化检验 —— 超额到底是「行业 beta」还是「选股 alpha」？

为什么做这个
------------
`docs/个股线_股息率策略.md` §六 局限 7：**未做行业中性化**。
高股息股票天然集中在**银行 / 煤炭（化石能源）/ 公用事业 / 钢铁**，
而 2023-2024 恰是「中特估」行情 —— 这些行业整体大涨。
所以「股息率组合跑赢中证1000 4.3pp」完全可能只是**押对了行业**，不是选股能力。

本文用三种手段回答它：

| 手段 | 问题 |
|---|---|
| **A. 行业暴露表** | 组合的行业权重 vs 全市场行业权重，偏离多大？ |
| **B. 行业中性化回测** | 把行业权重摁回市场水平后，超额还剩多少？ |
| **C. Brinson 归因** | 超额里「行业配置」与「行业内选股」各占多少？ |

三种中性化构造
--------------
| 模式 | 做法 | 行业权重 |
|---|---|---|
| `topn` | 全局按 dy 取前 N（**基线**） | 由信号决定 → 天然偏向高股息行业 |
| `indpct` | **行业内** dy 百分位 → 全局取前 N | ≈ **行业等权**（每个行业机会均等） |
| `indquota` | 行业配额 = **市场行业权重 × N**，行业内按 dy 取 | ≈ **市场权重**（严格的行业中性） |

`indpct` 是最强的中性化（把「哪个行业」这个信息完全抹掉，只剩「行业里谁股息率高」）；
`indquota` 是标准的 benchmark-relative 中性化。两个都看，结论才稳。

判据
----
- 中性化后超额**基本保持** → 是真选股 alpha，行业 beta 不是主因；
- 中性化后超额**大幅缩水/转负** → 之前的超额主要是行业 beta，**报告必须改口径**。

⚠️ 行业分类的前视偏差
----------------------
东财 `RPT_F10_BASIC_ORGINFO` 给的是**当前**行业，不是 point-in-time。
行业分类变更频率低，但借壳/重组会污染历史。本文用途是**粗粒度归因**，
影响有限；但**不能**拿它构造需要精确 point-in-time 的因子。

用法
----
    PY=.../python.exe
    $PY tools/test_industry_neutral.py \
        --bars data/stockbars/bars_total.parquet \
        --bfq  data/stockbars/bars_bfq.parquet \
        --dividends data/dividends/bonus_all.parquet \
        --industry data/industry/industry_all.parquet \
        --universe data/stockbars/universe_all.csv \
        --topn 20 30 --hold 60 --max-dy 10 --min-div3 2
"""
from __future__ import annotations

import argparse
import os
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_dividend import (MIN_COMMISSION, MODELED_ROUND,  # noqa: E402
                                     NOMINAL_FEE, SLIP, STAMP,
                                     prepare as prepare_div)
from tools.backtest_stock import metrics, run  # noqa: E402

TRADING_DAYS = 244.0


# ================================================================ 选股计划
def plan_selections(df: pd.DataFrame, dates: list[str], by_date: dict, *,
                    mode: str, topn: int, hold: int, dy_col: str,
                    min_dy: float, max_dy: float | None,
                    min_div3: int,
                    hyst_entry: float | None = None,
                    hyst_exit: float | None = None) -> dict:
    """返回 `{调仓日: [code, ...]}`。

    把「选股」与「建掩码」拆开，是为了让**行业暴露分析**与**Brinson 归因**
    能复用同一份选股结果 —— 否则三处各写一遍逻辑，早晚对不上。

    ⚠️ 只在**调仓日**选股（与 `backtest_dividend.build_mask` 同一约定）。
    引擎在两次调仓之间保持持仓，不能每天重选。

    `hyst_entry` / `hyst_exit`（滞回，可与任意 `mode` 叠加）
    ------------------------------------------------------
    给出这两个参数时，**先按「四进三出」缩小候选集**，再按 `mode` 选：

        候选 = {dy ≥ hyst_entry} ∪ {已持有 且 dy ≥ hyst_exit}

    这一步是**正交**的：它只改变"候选池"，不改变"怎么从候选里挑"。
    于是可以组合出 `indpct + hyst`、`indquota + hyst` 等形态。

    ⚠️ **`indquota` 的行业配额必须用「全体合格池」算，不能用候选集算** ——
    用候选集算的话，`hyst` 门槛会同时改变行业权重，行业中性就失效了。
    """
    code_arr = df.code.values
    in_uni = df._in_uni.values
    dy_all = df[dy_col].values
    nd3_all = df["n_div3"].values
    ind_all = df["ind_l1"].values

    plan: dict = {}
    held: list[str] = []
    for i in range(0, len(dates) - 1, hold):
        t = dates[i]
        rows = by_date.get(t)
        if rows is None:
            continue
        rows = rows[in_uni[rows]]
        if len(rows) == 0:
            continue
        dy = pd.Series(dy_all[rows], index=code_arr[rows])
        nd3 = pd.Series(nd3_all[rows], index=code_arr[rows])
        ind = pd.Series(ind_all[rows], index=code_arr[rows])
        ok = dy.notna() & (dy >= min_dy)
        if max_dy is not None:
            ok &= (dy <= max_dy)
        if min_div3:
            ok &= (nd3.fillna(0) >= min_div3)
        if mode in ("indpct", "indquota"):
            ok &= (ind != "未分类")          # 无行业标签无法中性化
        dy, ind = dy[ok], ind[ok]
        if dy.empty:
            plan[t] = []
            held = []
            continue
        # `dy` / `ind` 此时 = **全体合格池**（行业配额要用它算）
        ind_pool = ind

        # ── 滞回：只缩小候选集，不改变后续怎么挑 ──
        if hyst_entry is not None:
            is_held = dy.index.isin(set(held))
            cand = (dy >= hyst_entry) | (is_held & (dy >= (hyst_exit or hyst_entry)))
            dy, ind = dy[cand], ind[cand]
            if dy.empty:
                plan[t] = []
                held = []
                continue

        if mode == "topn":
            sel = dy.nlargest(min(topn, len(dy))).index.tolist()
        elif mode == "indpct":
            # 行业内百分位（0~1）→ 全局取前 N。
            # 每个行业的百分位都是均匀分布，故各行业入选数大致相等 → 行业等权。
            pct = dy.groupby(ind).rank(pct=True)
            sel = pct.nlargest(min(topn, len(pct))).index.tolist()
        elif mode == "indquota":
            # 行业配额 = 该行业在**全体合格池**里的股票数占比 × N
            # （最大余数法补足到 N）。⚠️ 用 `ind_pool`，不能用 `ind`。
            cnt = ind_pool.value_counts()
            quota = cnt / cnt.sum() * topn
            base = np.floor(quota).astype(int)
            rest = topn - int(base.sum())
            if rest > 0:
                for k in (quota - base).sort_values(ascending=False).index[:rest]:
                    base[k] += 1
            picks = []
            for k, n_k in base.items():
                if n_k <= 0:
                    continue
                g = dy[ind == k]
                if g.empty:
                    continue
                picks += g.nlargest(int(n_k)).index.tolist()
            sel = picks
        else:
            raise ValueError(f"未知模式 {mode}")
        plan[t] = sel
        held = sel
    return plan


def mask_from_plan(df: pd.DataFrame, plan: dict, by_date: dict) -> np.ndarray:
    """把选股计划转成 `run()` 能吃的布尔列。

    ⚠️ 用 `by_date` 的行号索引，**不要**对整个 900 万行表做 `np.isin` ——
    第一版那样写是「每个调仓日 × 全表」，24 次回测要跑几百次全表扫描。
    """
    mask = np.zeros(len(df), dtype=bool)
    code_arr = df.code.values
    for t, sel in plan.items():
        if not sel:
            continue
        rows = by_date.get(t)
        if rows is None:
            continue
        m = np.isin(code_arr[rows], sel)
        mask[rows[m]] = True
    return mask


# ================================================================ A. 行业暴露
def industry_exposure(df: pd.DataFrame, plan: dict, *, topn: int) -> pd.DataFrame:
    """组合的行业权重 vs 当日合格池的行业权重（按调仓日平均）。"""
    code_arr = df.code.values
    in_uni = df._in_uni.values
    ind_all = df["ind_l1"].values
    date_arr = df.date.values
    by_date = df.groupby("date", sort=False).indices

    recs = []
    for t, sel in plan.items():
        if not sel:
            continue
        rows = by_date.get(t)
        if rows is None:
            continue
        rows = rows[in_uni[rows]]
        mkt = pd.Series(ind_all[rows]).value_counts(normalize=True)
        held = set(sel)
        hrows = [r for r in rows if code_arr[r] in held]
        if not hrows:
            continue
        port = pd.Series(ind_all[hrows]).value_counts(normalize=True)
        for k in set(mkt.index) | set(port.index):
            recs.append({"date": t, "行业": k,
                         "组合权重": float(port.get(k, 0.0)),
                         "市场权重": float(mkt.get(k, 0.0))})
    e = pd.DataFrame(recs)
    if e.empty:
        return e
    g = e.groupby("行业").agg(组合权重=("组合权重", "mean"),
                              市场权重=("市场权重", "mean"),
                              天数=("date", "nunique")).reset_index()
    g["超配pp"] = (g.组合权重 - g.市场权重) * 100
    g["组合持仓数"] = g.组合权重 * topn
    return g.sort_values("组合权重", ascending=False).reset_index(drop=True)


def concentration(e: pd.DataFrame) -> dict:
    """集中度指标：HHI、前 3 大行业权重合计。"""
    if e.empty:
        return {}
    w = e.组合权重.values
    return {"组合HHI": float((w ** 2).sum()),
            "市场HHI": float((e.市场权重.values ** 2).sum()),
            "组合前3行业占比": float(np.sort(w)[::-1][:3].sum()) * 100,
            "市场前3行业占比": float(np.sort(e.市场权重.values)[::-1][:3].sum()) * 100}


# ================================================================ C. Brinson 归因
def brinson(df: pd.DataFrame, plan: dict, period_dates: list[str], *,
            hold: int, min_listed: int, min_amount: float,
            min_price: float) -> pd.DataFrame:
    """逐持有期 Brinson 分解：总收益 = 行业配置贡献 + 行业内选股贡献。

    定义（每个持有期 `[t0, t1)`）：
    - `w_i^p` 组合在行业 i 的权重（等权 → 入选数 / N）
    - `w_i^m` 合格池在行业 i 的权重（股票数占比）
    - `r_i`   行业 i 的**等权收益**（池内该行业全部股票）
    - `r_i^p` 组合在行业 i 内持仓的等权收益

    ```
    R_p            = Σ_i w_i^p · r_i^p
    行业配置贡献   = Σ_i w_i^p · r_i
    行业内选股贡献 = Σ_i w_i^p · (r_i^p − r_i)
    市场基准 R_m   = Σ_i w_i^m · r_i
    超额 = R_p − R_m = Σ_i (w_i^p − w_i^m)·r_i  +  Σ_i w_i^p·(r_i^p − r_i)
                       └── 行业配置超额 ──┘      └── 行业内选股超额 ──┘
    ```

    ⚠️ **调仓日必须从 `plan` 自己取，不能写 `dates[::hold]`**。
    第一版用 `dates[::hold]`（锚定在全区间第一天）去过滤 `plan` 的键，
    而样本外的 plan 是从 2023 年第一天起每 60 日一个相位 ——
    **两个相位不相交**，样本外归因直接算不出来（当时只输出了全区间与样本内）。

    ⚠️ 用**收盘价到收盘价**（`bars_total` 的总收益序列），不含交易成本与 T+1 ——
    本函数只回答「收益从哪来」，组合的**真实**表现以 `run()` 的回测为准。
    """
    reb = sorted(t for t in plan if t in period_dates)
    if not reb:
        return pd.DataFrame()
    px_dates = sorted(set(reb) | {period_dates[-1]})

    sub = df[df.date.isin(px_dates)][["code", "date", "close", "ind_l1",
                                      "listed", "amt_ma20", "suspended"]]
    piv = sub.pivot_table(index="code", columns="date", values="close")
    pool = sub.pivot_table(index="code", columns="date", values="listed",
                           aggfunc="max")
    amt = sub.pivot_table(index="code", columns="date", values="amt_ma20",
                          aggfunc="max")
    susp = sub.pivot_table(index="code", columns="date", values="suspended",
                           aggfunc="max")
    ind_map = df.drop_duplicates("code").set_index("code").ind_l1

    recs = []
    for k in range(len(px_dates) - 1):
        t0, t1 = px_dates[k], px_dates[k + 1]
        if t0 not in plan or t0 not in piv.columns or t1 not in piv.columns:
            continue
        ret = (piv[t1] / piv[t0] - 1.0).replace([np.inf, -np.inf], np.nan)
        ok = (pool[t0].fillna(0) >= min_listed) & (amt[t0].fillna(0) >= min_amount) \
            & (piv[t0].fillna(0) >= min_price) & (~susp[t0].fillna(True).astype(bool))
        r = ret[ok & ret.notna()]
        if len(r) < 50:
            continue
        ii = ind_map.reindex(r.index)
        sel = [c for c in plan[t0] if c in r.index]
        if not sel:
            continue
        w_p = pd.Series(ii.reindex(sel)).value_counts(normalize=True)
        w_m = ii.value_counts(normalize=True)
        r_i = r.groupby(ii).mean()
        r_i_p = r.reindex(sel).groupby(ii.reindex(sel)).mean()

        cfg = float((w_p * r_i.reindex(w_p.index).fillna(0.0)).sum())
        pick = float((w_p * (r_i_p.reindex(w_p.index) - r_i.reindex(w_p.index))
                      .fillna(0.0)).sum())
        Rp = cfg + pick
        Rm = float((w_m * r_i.reindex(w_m.index).fillna(0.0)).sum())
        allind = r_i.index
        ex_cfg = float(((w_p.reindex(allind).fillna(0.0)
                         - w_m.reindex(allind).fillna(0.0)) * r_i).sum())
        ex_pick = float((w_p.reindex(allind).fillna(0.0)
                         * (r_i_p.reindex(allind) - r_i).fillna(0.0)).sum())
        recs.append({"t0": t0, "t1": t1, "N": len(sel),
                     "组合收益": Rp, "基准收益": Rm,
                     "行业配置贡献": cfg, "选股贡献": pick,
                     "行业配置超额": ex_cfg, "选股超额": ex_pick,
                     "超额": Rp - Rm})
    return pd.DataFrame(recs)


def ann(x: float, hold: int) -> float:
    """持有期收益 → 复利年化（%）。用于净值序列。"""
    return ((1 + x) ** (TRADING_DAYS / hold) - 1) * 100


def ann_arith(x: float, hold: int) -> float:
    """持有期贡献 → 算术年化（%）。

    ⚠️ Brinson 的「配置贡献 / 选股贡献」是**逐期相加**的量，
    不能复利（`(1+x)^k` 对"贡献"没有意义，还会在负贡献上炸掉）。
    故统一用 `均值 × (244/hold)` 的算术年化。
    """
    return x * (TRADING_DAYS / hold) * 100


def real_ann(ann_pct: float, turnover: float, avg_hold: float,
             cap_wan: float) -> float:
    """计入「单笔最低佣金 5 元」后的真实年化（%）。

    与 `backtest_dividend.py` / `sweep_scaling.py` 同一套成本模型：
    单笔金额 = 资金 ÷ 平均持仓数；佣金率 = max(名义万5, 5 元 ÷ 单笔金额)。
    """
    ticket = cap_wan * 1e4 / max(avg_hold, 1.0)
    comm = max(NOMINAL_FEE, MIN_COMMISSION / ticket)
    real_round = (comm + SLIP) + (comm + STAMP + SLIP)
    return ann_pct - turnover * (real_round - MODELED_ROUND) * 100


# ================================================================ 主流程
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="股息率策略行业中性化检验")
    ap.add_argument("--bars", default="data/stockbars/bars_total.parquet")
    ap.add_argument("--bfq", default="data/stockbars/bars_bfq.parquet")
    ap.add_argument("--dividends", default="data/dividends/bonus_all.parquet")
    ap.add_argument("--industry", default="data/industry/industry_all.parquet")
    ap.add_argument("--universe", default="data/stockbars/universe_all.csv")
    ap.add_argument("--indexes", default="data/indexes/indexes_all.parquet")
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260911")
    ap.add_argument("--topn", type=int, nargs="+", default=[20, 30])
    ap.add_argument("--hold", type=int, default=60)
    ap.add_argument("--dy-col", default="dy_ttm", choices=["dy_ttm", "dy_fwd"])
    ap.add_argument("--min-dy", type=float, default=0.5)
    ap.add_argument("--max-dy", type=float, default=10.0)
    ap.add_argument("--min-div3", type=int, default=2)
    ap.add_argument("--min-price", type=float, default=2.0)
    ap.add_argument("--min-amount", type=float, default=3e7)
    ap.add_argument("--min-listed", type=int, default=120)
    ap.add_argument("--adj-mode", default="legacy", choices=["legacy", "correct"],
                    help="送转调整口径（透传给 build_yield_panel）："
                         "legacy=旧实现（默认）；correct=价值中性口径")
    ap.add_argument("--hyst-entry", type=float, default=None,
                    help="滞回买入阈值（%）。给出后额外跑「hyst × 行业中性化」叠加配置")
    ap.add_argument("--hyst-exit", type=float, default=None,
                    help="滞回卖出阈值（%）")
    ap.add_argument("--out-prefix", default="results/industry_neutral")
    args = ap.parse_args(argv)

    print("=" * 100)
    print("  股息率策略：行业中性化检验")
    print("=" * 100)

    # ---- 数据准备（复用 backtest_dividend.prepare，保证与股息率回测同一套口径）----
    ns = SimpleNamespace(bars=args.bars, bfq=args.bfq, dividends=args.dividends,
                         universe=args.universe, min_listed=args.min_listed,
                         min_amount=args.min_amount, min_price=args.min_price,
                         adj_mode=args.adj_mode)
    df = prepare_div(ns)

    ind = pd.read_parquet(args.industry)[["code_full", "ind_l1", "em2016"]]
    ind = ind.rename(columns={"code_full": "code"})
    n0 = df.code.nunique()
    df = df.merge(ind[["code", "ind_l1"]], on="code", how="left")
    df["ind_l1"] = df.ind_l1.fillna("未分类")
    miss = df.loc[df.ind_l1 == "未分类", "code"].nunique()
    print(f"  并入行业分类：{n0:,} 只中 {miss:,} 只未分类"
          f"（{miss/max(n0,1)*100:.2f}%，中性化变体会剔除）")

    dates = sorted(df[(df.date >= args.start) & (df.date <= args.end)].date.unique())
    print(f"  区间 {args.start}~{args.end}  {len(dates)} 交易日 "
          f"({len(dates)/TRADING_DAYS:.1f} 年) ｜ hold={args.hold} 日\n")
    by_date = {t: v for t, v in df.groupby("date", sort=False).indices.items()}

    # ---- 基准指数 ----
    benches = {}
    if os.path.exists(args.indexes):
        ix = pd.read_parquet(args.indexes)
        ix["date"] = ix.date.astype(str).str.replace("-", "", regex=False)
        for sym, nm in (("SH000852", "中证1000"), ("SZ399303", "国证2000"),
                        ("SH000300", "沪深300")):
            px = ix[ix.code == sym].set_index("date").close.sort_index()
            px = px.reindex(dates).ffill().dropna()
            if len(px) > 2:
                benches[nm] = metrics(px)["年化收益"]

    def run_cfg(tag: str, mode: str, n: int, he, hx, s: str, e: str):
        dts = [t for t in dates if s <= t <= e]
        pl = plan_selections(df, dts, by_date, mode=mode, topn=n, hold=args.hold,
                             dy_col=args.dy_col, min_dy=args.min_dy,
                             max_dy=args.max_dy, min_div3=args.min_div3,
                             hyst_entry=he, hyst_exit=hx)
        mask = mask_from_plan(df, pl, by_date)
        col = "_sig"
        df[col] = mask
        eq, tr, meta = run(df, [], start=s, end=e, hold=args.hold, cond_col=col,
                           min_price=args.min_price, min_amount=args.min_amount,
                           min_listed=args.min_listed)
        m = metrics(eq.equity)
        yrs = len(eq) / TRADING_DAYS
        nh = meta.get("avg_hold", 0) or 1
        turn = (int((tr.side == "buy").sum()) / yrs / nh) if len(tr) else 0.0
        return m, nh, turn, pl, eq, len(tr)

    # ================= A. 行业暴露 =================
    print("=" * 100)
    print("  A. 行业暴露：高股息组合 vs 全市场")
    print("=" * 100)
    exps = {}
    for mode, lbl in (("topn", "基线 topn"), ("indpct", "行业内百分位")):
        pl = plan_selections(df, dates, by_date, mode=mode, topn=args.topn[0],
                             hold=args.hold, dy_col=args.dy_col,
                             min_dy=args.min_dy, max_dy=args.max_dy,
                             min_div3=args.min_div3)
        exps[lbl] = industry_exposure(df, pl, topn=args.topn[0])
        if mode == "topn":
            pl_base = pl

    exp = exps["基线 topn"]
    if not exp.empty:
        print("\n  集中度对照（HHI 越大越集中；市场基线 ≈ 1/行业数）：")
        print("  " + "{:<16}{:>10}{:>12}{:>14}".format(
            "构造", "组合HHI", "前3行业占比", "最大超配行业"))
        print("  " + "-" * 54)
        for lbl, e_ in exps.items():
            if e_.empty:
                continue
            cc = concentration(e_)
            top = e_.iloc[0]
            print("  " + "{:<16}{:>10.3f}{:>11.1f}%{:>14}".format(
                lbl, cc["组合HHI"], cc["组合前3行业占比"],
                f"{top.行业} +{top.超配pp:.1f}pp"))
        cc = concentration(exp)
        print("  " + "{:<16}{:>10.3f}{:>11.1f}%{:>14}".format(
            "全市场", cc["市场HHI"], cc["市场前3行业占比"], "—"))

        print(f"\n  ── 基线 topn 的行业权重明细（按组合权重降序，前 12）")
        print("  " + "{:<12}{:>10}{:>10}{:>10}{:>10}".format(
            "行业", "组合权重", "市场权重", "超配pp", "组合持仓数"))
        print("  " + "-" * 52)
        for _, r_ in exp.head(12).iterrows():
            print("  " + "{:<12}{:>9.1f}%{:>9.1f}%{:>+10.1f}{:>10.1f}".format(
                r_.行业, r_.组合权重 * 100, r_.市场权重 * 100, r_.超配pp,
                r_.组合持仓数))
        print(f"\n  ── 基线 topn 的低配（超配pp 最小，前 6）")
        print("  " + "{:<12}{:>10}{:>10}{:>10}".format(
            "行业", "组合权重", "市场权重", "超配pp"))
        print("  " + "-" * 42)
        for _, r_ in exp.tail(6).iterrows():
            print("  " + "{:<12}{:>9.1f}%{:>9.1f}%{:>+10.1f}".format(
                r_.行业, r_.组合权重 * 100, r_.市场权重 * 100, r_.超配pp))
        exp.to_csv(f"{args.out_prefix}_exposure.csv", index=False,
                   encoding="utf-8-sig")
        for lbl, e_ in exps.items():
            if not e_.empty and lbl != "基线 topn":
                e_.to_csv(f"{args.out_prefix}_exposure_{lbl}.csv", index=False,
                          encoding="utf-8-sig")
    else:
        print("  ⚠️ 无暴露数据")

    # ================= B. 中性化回测 =================
    print("\n" + "=" * 100)
    print("  B. 行业中性化回测（全区间 / 样本内 / 样本外）")
    print("=" * 100)
    cfgs = []
    for n in args.topn:
        cfgs.append((f"原始 topn N={n}", "topn", n, None, None))
    for n in args.topn:
        cfgs.append((f"行业内百分位 N={n}", "indpct", n, None, None))
    for n in (30, 50):
        cfgs.append((f"行业配额中性 N={n}", "indquota", n, None, None))
    # ── 叠加：滞回（hyst）× 行业中性化 ──
    # 两个改进各自都改善样本外，但它们是否**可叠加**未知 —— 本节专门回答。
    if args.hyst_entry:
        he = args.hyst_entry
        hx = args.hyst_exit if args.hyst_exit is not None else args.hyst_entry
        for n in args.topn:
            cfgs.append((f"hyst {he:g}/{hx:g} N={n}", "topn", n, he, hx))
            cfgs.append((f"hyst+行业内 N={n}", "indpct", n, he, hx))
        cfgs.append((f"hyst+行业配额 N={args.topn[0]}", "indquota",
                     args.topn[0], he, hx))

    periods = [("全区间", args.start, args.end),
               ("样本内 2019~2022", "20190101", "20221231"),
               ("样本外 2023~2026", "20230101", args.end)]

    rows = []
    plans = {}
    for ptag, s, e in periods:
        print(f"\n  ── {ptag}（{s}~{e}）" + "─" * 40)
        print("  " + "{:<22}{:>9}{:>8}{:>9}{:>9}{:>8}{:>9}{:>10}".format(
            "配置", "年化%", "夏普", "回撤%", "持仓", "换手", "交易笔数", "10万%"))
        print("  " + "-" * 84)
        for tag, mode, n, he, hx in cfgs:
            m, nh, turn, pl, eq, ntr = run_cfg(tag, mode, n, he, hx, s, e)
            plans[(ptag, tag)] = (pl, eq, m)
            real = real_ann(m["年化收益"], turn, nh, 10.0)
            print("  " + "{:<22}{:>9.2f}{:>8.2f}{:>9.2f}{:>9.1f}{:>8.2f}{:>9,}{:>10.2f}".format(
                tag, m["年化收益"], m["夏普比率"], m["最大回撤"], nh, turn, ntr, real),
                flush=True)
            rows.append({"区间": ptag, "配置": tag, "模式": mode, "N": n,
                         "hyst_entry": he, "hyst_exit": hx,
                         "年化%": m["年化收益"], "夏普": m["夏普比率"],
                         "回撤%": m["最大回撤"], "平均持仓": nh,
                         "年单边换手": turn, "交易笔数": ntr, "10万真实年化%": real})
    r = pd.DataFrame(rows)
    r.to_csv(f"{args.out_prefix}_backtest.csv", index=False, encoding="utf-8-sig")

    if benches:
        print("\n  同期可投资指数年化："
              + "  ".join(f"{k} {v:.2f}%" for k, v in benches.items()))

    # ================= B2. 叠加效应小结 =================
    if args.hyst_entry:
        print("\n" + "=" * 100)
        print("  B2. 叠加效应：两个改进能否相加？（样本外为准）")
        print("=" * 100)
        for ptag in ("全区间", "样本外 2023~2026"):
            s_ = r[r.区间 == ptag].set_index("配置")
            if s_.empty:
                continue
            print(f"\n  ── {ptag} ──")
            base = None
            for tag, delta_from in (
                    (f"原始 topn N={args.topn[0]}", None),
                    (f"行业内百分位 N={args.topn[0]}", f"原始 topn N={args.topn[0]}"),
                    (f"hyst {args.hyst_entry:g}/"
                     f"{(args.hyst_exit if args.hyst_exit is not None else args.hyst_entry):g} "
                     f"N={args.topn[0]}", f"原始 topn N={args.topn[0]}"),
                    (f"hyst+行业内 N={args.topn[0]}",
                     f"行业内百分位 N={args.topn[0]}")):
                if tag not in s_.index:
                    continue
                v = float(s_.loc[tag, "年化%"])
                if base is None:
                    base = v
                d = f"{v - float(s_.loc[delta_from, '年化%']):+.2f}" \
                    if delta_from and delta_from in s_.index else "—"
                print(f"  {tag:<24} 年化 {v:>6.2f}%  "
                      f"夏普 {s_.loc[tag,'夏普']:>5.2f}  回撤 {s_.loc[tag,'回撤%']:>7.2f}%  "
                      f"10万 {s_.loc[tag,'10万真实年化%']:>6.2f}%   "
                      f"相对{delta_from or '基线'} {d}")
            # 叠加是否成立：两者相加 ≈ 叠加后的总增量？
            try:
                b = float(s_.loc[f"原始 topn N={args.topn[0]}", "年化%"])
                i = float(s_.loc[f"行业内百分位 N={args.topn[0]}", "年化%"])
                hy = float(s_.loc[f"hyst {args.hyst_entry:g}/"
                                  f"{(args.hyst_exit if args.hyst_exit is not None else args.hyst_entry):g} "
                                  f"N={args.topn[0]}", "年化%"])
                bo = float(s_.loc[f"hyst+行业内 N={args.topn[0]}", "年化%"])
                print(f"  → 单独增量：行业 {i-b:+.2f}pp、滞回 {hy-b:+.2f}pp，"
                      f"相加 = {i+hy-2*b:+.2f}pp")
                print(f"  → 叠加实测：{bo-b:+.2f}pp  "
                      f"（{'可叠加' if bo - b > (i + hy - 2 * b) * 0.6 else '不可叠加/重叠'}）")
            except (KeyError, ValueError):
                pass

    # ================= C. Brinson 归因 =================
    print("\n" + "=" * 100)
    print("  C. Brinson 归因：超额 = 行业配置超额 + 行业内选股超额（算术年化，毛收益）")
    print("=" * 100)
    att_rows = []
    detail = []
    for ptag, s, e in periods:
        pdts = [t for t in dates if s <= t <= e]
        for tag, mode, n, he, hx in [c for c in cfgs if c[2] == args.topn[0]][:2]:
            pl, eq, m = plans[(ptag, tag)]
            a = brinson(df, pl, pdts, hold=args.hold, min_listed=args.min_listed,
                        min_amount=args.min_amount, min_price=args.min_price)
            if a.empty:
                print(f"\n  [{ptag}] {tag}：⚠️ 无可归因期")
                continue
            a = a.copy()
            a.insert(0, "区间", ptag)
            a.insert(1, "配置", tag)
            detail.append(a)
            mean = a.drop(columns=["区间", "配置", "t0", "t1", "N"]).mean()
            line = {"区间": ptag, "配置": tag, "期数": len(a)}
            for k in mean.index:
                line[k + "(年化%)"] = ann_arith(float(mean[k]), args.hold)
            att_rows.append(line)
            print(f"\n  [{ptag}] {tag}  共 {len(a)} 期（每期 {args.hold} 交易日，"
                  f"算术年化）")
            print("  " + "{:<20}{:>12}".format("项", "年化%"))
            print("  " + "-" * 32)
            for k in mean.index:
                print("  " + "{:<20}{:>12.2f}".format(
                    k, ann_arith(float(mean[k]), args.hold)))
            cfgx = ann_arith(float(mean["行业配置超额"]), args.hold)
            pickx = ann_arith(float(mean["选股超额"]), args.hold)
            tot = ann_arith(float(mean["超额"]), args.hold)
            print(f"  → 超额 {tot:+.2f}pp/年 = 行业配置 {cfgx:+.2f}pp "
                  f"+ 选股 {pickx:+.2f}pp")

            # ---- 稳健性：行业配置拖累是否由少数期主导？ ----
            ec = a["行业配置超额"]
            npos = int((ec > 0).sum())
            i_w = ec.idxmin()
            print(f"  ⚠️ 行业配置超额分布：{npos}/{len(ec)} 期为正，"
                  f"中位 {ann_arith(float(ec.median()), args.hold):+.2f}pp/年；"
                  f"最差一期 {ec.loc[i_w]*100:+.2f}%（{a.loc[i_w,'t0']}）")
            if len(ec) > 2:
                print(f"     剔除最差一期后："
                      f"{ann_arith(float(ec.drop(i_w).mean()), args.hold):+.2f}pp/年"
                      f"   （原 {cfgx:+.2f}pp）→ 拖累"
                      f"{'集中' if abs(ec.drop(i_w).mean()) < abs(ec.mean()) * 0.6 else '分散'}"
                      f"在少数期")
            pk = a["选股超额"]
            print(f"     选股超额：{int((pk > 0).sum())}/{len(pk)} 期为正，"
                  f"中位 {ann_arith(float(pk.median()), args.hold):+.2f}pp/年")
    if att_rows:
        pd.DataFrame(att_rows).to_csv(f"{args.out_prefix}_brinson.csv",
                                      index=False, encoding="utf-8-sig")
    if detail:
        pd.concat(detail, ignore_index=True).to_csv(
            f"{args.out_prefix}_brinson_detail.csv", index=False,
            encoding="utf-8-sig")

    print("\n" + "=" * 100)
    print("  判据：中性化后超额基本保持 → 真选股 alpha；大幅缩水 → 主要是行业 beta")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
