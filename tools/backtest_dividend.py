"""股息率组合回测 —— 「强信号 × 低频」方向能否在 10 万下落地的决定性检验。

背景
----
`docs/HANDOFF_项目总结与下一步.md` 第零节：现有因子集是「弱信号 × 高换手」
（IC 0.05~0.08、年单边换手 8 倍），靠广度赚钱 → 要 700+ 只持仓 → 10 万做不了。
主人举的高股息反例（聚宽「四进三出」：股息率 >4% 买、<3% 卖）指出另一条路：
**信号慢、换手低 → 少持仓也能做**。

`tools/test_dividend_factor.py` 已确认股息率**有真实且稳健的 alpha**
（60 日 IC 0.064、8/8 年为正、样本外 0.044、顶底档差 +8pp/年）。
本脚本回答最后一个问题：**做成 10~30 只持仓的组合，10 万能不能落地？**

两种组合构造
------------
| 模式 | 规则 | 对应 |
|---|---|---|
| `topn` | 每个调仓日取股息率最高的 N 只，等权 | 「高股息 top-N」 |
| `hyst` | 股息率 ≥ `entry` 买入；已持仓的跌到 < `exit` 才卖（滞回）| 「四进三出」 |

两种都通过引擎的 `cond_col`（条件选股）模式实现：
把「每个调仓日应持有的股票」预先编码成一个布尔列，引擎负责
T+1、涨跌停不可成交、停牌、退市了结、最低佣金、印花税。

⚠️ **必须用 `bars_total.parquet`**（不复权价 + 分红回放），
不能用 `bars_all.parquet`（腾讯 `qfq` 是仿射复权，不保持日收益，
见 `docs/个股线_复权口径缺陷.md`）。股息率策略尤其敏感 ——
它的分母就是价格，用被压小的 qfq 价会系统性高估股息率。

用法
----
    PY=.../python.exe
    $PY tools/backtest_dividend.py \
        --bars data/stockbars/bars_total.parquet \
        --dividends data/dividends/bonus_all.parquet \
        --universe data/stockbars/universe_all.csv \
        --topn 10 20 30 --hold 60 --out results/dividend_backtest.csv
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import build_features, metrics, run
from tools.test_dividend_factor import build_yield_panel, require_adj_mode
from tools.build_div_tax import load_div_tax, DEFAULT_DIV_TAX

# 成本模型统一到 tools/costs.py（**单一来源**，勿在此重定义 —— 铁律 14）
from tools.costs import (MIN_COMMISSION, MODELED_ROUND, NOMINAL_FEE,  # noqa: E402
                         SLIP, STAMP)


def prepare(args) -> pd.DataFrame:
    """返回**引擎可直接吃**的特征表 + 股息率列。

    ⚠️ 不能把原始 bars 直接喂给 `run()` —— 它要的是 `build_features` 产出的
    `prev_close / buy_blocked / sell_blocked / ma60 / listed / amt_ma20 / suspended`
    等列（本次踩过：只算股息率列就调 run()，报 `KeyError: 'buy_blocked'`）。
    """
    b = pd.read_parquet(args.bars)
    b["date"] = b.date.astype(str).str.replace("-", "", regex=False)
    if args.universe and os.path.exists(args.universe):
        u = pd.read_csv(args.universe, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
        b = b[~b.code.isin(st)]
    b = b.sort_values(["code", "date"]).reset_index(drop=True)
    # ⚠️ 股息率的分母必须是**真实市价**，不能用 bars_total 的 close
    # （那是总收益指数，历史区间比真实价低约 14% → 系统性高估股息率）
    if args.bfq and os.path.exists(args.bfq):
        px = pd.read_parquet(args.bfq, columns=["code", "date", "close"])
        px["date"] = px.date.astype(str).str.replace("-", "", regex=False)
        px = px.rename(columns={"close": "px_real"})
        b = b.merge(px, on=["code", "date"], how="left")
        n_ok = b.px_real.notna().mean()
        print(f"  并入真实价（{os.path.basename(args.bfq)}）覆盖率 {n_ok*100:.2f}%")
        if n_ok < 0.99:
            print("  ❌ 真实价覆盖率不足 99%，股息率分母不可靠，终止")
            raise SystemExit(2)
    else:
        print("  ⚠️ 未提供 --bfq，股息率分母退回总收益指数（会高估股息率）")
    div = pd.read_parquet(args.dividends)
    print(f"  日线 {len(b):,} 行 / {b.code.nunique():,} 只 ｜ 分红 {len(div):,} 条")

    print("  计算引擎特征…", flush=True)
    feat = build_features(b)
    print("  构建 point-in-time 股息率面板…（adj_mode = {}）".format(
        require_adj_mode(args)), flush=True)
    dy = build_yield_panel(b, div, price_col="px_real" if "px_real" in b else "close",
                           adj_mode=require_adj_mode(args))[
        ["code", "date", "dps_ttm", "dps_fwd", "dy_ttm", "dy_fwd", "n_div3"]]
    df = feat.merge(dy, on=["code", "date"], how="left")
    # 池子掩码（与 run() 内部口径一致），供 build_mask 用
    df["_in_uni"] = ((df.listed >= args.min_listed)
                     & (df.amt_ma20 >= args.min_amount)
                     & (df.close >= args.min_price) & (~df.suspended))
    return df


def build_mask(df: pd.DataFrame, dates: list[str], by_date: dict, *,
               mode: str, topn: int, hold: int, dy_col: str,
               entry: float, exit_: float | None,
               min_dy: float, max_dy: float | None = None,
               min_div3: int = 0) -> np.ndarray:
    """构造 cond_col 掩码：只在**调仓日**为「应持有」的股票置 True。

    ⚠️ 不能每天置 True —— 引擎在 `hold=1` 时每天把权重**拉回等权**，
    会产生大量零碎交易，把低换手优势吃掉。掩码只在调仓日生效，
    引擎在两次调仓之间保持持仓。

    ⚠️ `by_date`（日期 → 行号）**必须在外面算一次传进来**。
    第一版在函数内写 `{t: np.where(df.date.values == t)[0] for t in dates}`，
    是 1868 次 × 900 万行的全表比较；× 6 组配置 = 上千亿次比较，
    实测跑 10 分钟没出结果。

    `max_dy` / `min_div3`
    --------------------
    「按股息率取前 N 名」是很天真的构造：**极高股息率往往是价值陷阱**
    （股价崩了 / 一次性特别分红），且可能是「今年分了、明年不分」。
    故留两个过滤器：`max_dy` 砍掉极端值，`min_div3` 要求过去 3 年分红次数下限。
    """
    mask = np.zeros(len(df), dtype=bool)
    code_arr = df.code.values
    in_uni = df._in_uni.values
    dy_all = df[dy_col].values
    nd3_all = df["n_div3"].values

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
        ok = dy.notna() & (dy >= min_dy)
        if max_dy is not None:
            ok &= (dy <= max_dy)
        if min_div3:
            ok &= (nd3.fillna(0) >= min_div3)
        dy = dy[ok]
        if dy.empty:
            held = []
            continue
        if mode == "topn":
            sel = dy.nlargest(min(topn, len(dy))).index.tolist()
        else:                                   # hyst：「四进三出」
            buy = set(dy[dy >= entry].index)
            keep = set(held) & set(dy[dy >= (exit_ or entry)].index)
            cand = buy | keep
            if not cand:
                held = []
                continue
            cand_dy = dy[dy.index.isin(cand)]
            sel = cand_dy.nlargest(min(topn, len(cand_dy))).index.tolist()
        held = sel
        m = np.isin(code_arr[rows], sel)
        mask[rows[m]] = True
    return mask


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="股息率组合回测")
    ap.add_argument("--bars", default="data/stockbars/bars_total.parquet")
    ap.add_argument("--bfq", default="data/stockbars/bars_bfq.parquet",
                    help="不复权真实价 —— 股息率的分母（**必须提供**）")
    ap.add_argument("--dividends", default="data/dividends/bonus_all.parquet")
    ap.add_argument("--universe", default="data/stockbars/universe_all.csv")
    ap.add_argument("--indexes", default="data/indexes/indexes_all.parquet")
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260911")
    ap.add_argument("--topn", type=int, nargs="+", default=[10, 20, 30])
    ap.add_argument("--hold", type=int, default=60,
                    help="调仓周期（交易日）。60≈季度，20≈月度")
    ap.add_argument("--dy-col", default="dy_ttm", choices=["dy_ttm", "dy_fwd"])
    ap.add_argument("--entry", type=float, default=4.0,
                    help="hyst 模式的买入阈值（股息率 %%）")
    ap.add_argument("--exit", type=float, default=3.0,
                    help="hyst 模式的卖出阈值（股息率 %%）")
    ap.add_argument("--min-dy", type=float, default=0.5,
                    help="入选的最低股息率（%%）——避免 0 分红股混入")
    ap.add_argument("--max-dy", type=float, default=None,
                    help="股息率上限（%%）——砍掉价值陷阱/一次性特别分红")
    ap.add_argument("--min-div3", type=int, default=0,
                    help="过去 3 年现金分红次数下限（持续性过滤）")
    ap.add_argument("--modes", nargs="+", default=["topn", "hyst"])
    ap.add_argument("--min-price", type=float, default=2.0)
    ap.add_argument("--min-amount", type=float, default=3e7)
    ap.add_argument("--min-listed", type=int, default=120)
    ap.add_argument("--adj-mode", default=None, choices=["legacy", "correct"],
                    help="送转调整口径（透传给 build_yield_panel）："
                         "correct=价值中性口径（**默认**）；legacy=已证伪的旧实现")
    ap.add_argument("--tax-rate", type=float, default=0.0,
                    help="红利税率（0~0.2）。**0 = 不扣税 = 默认**。"
                         "引擎内在**除权日**按持仓扣税，价格路径不变 → "
                         "税后与无税**组合恒等**，Δ 才可读作税成本（D4）")
    ap.add_argument("--div-tax", default=DEFAULT_DIV_TAX,
                    help="引擎内扣税用的每股派现表（tools/build_div_tax.py 生成）")
    ap.add_argument("--capitals", type=float, nargs="+",
                    default=[10, 20, 50, 100, 200])
    ap.add_argument("--out", default="results/dividend_backtest.csv")
    args = ap.parse_args(argv)

    print("=" * 96)
    print("  股息率组合回测（高股息 × 低换手 × 少持仓）")
    print("=" * 96)
    df = prepare(args)

    dates = sorted(df[(df.date >= args.start) & (df.date <= args.end)]
                   .date.unique())
    print(f"  区间 {args.start}~{args.end}  {len(dates)} 交易日 "
          f"({len(dates)/244:.1f} 年) ｜ 调仓周期 {args.hold} 日\n")
    # 日期 → 行号索引，**只建一次**（6 组配置共用）
    by_date = {t: v for t, v in df.groupby("date", sort=False).indices.items()}

    rows = []
    df["_div_sig"] = False        # 复用一个列，避免每轮 df.copy()（9M 行 × 6 次太浪费）
    # 过滤器变体：裸 top-N 是天真构造，逐步加约束看能否改善
    variants = [("裸", None, 0)]
    if args.max_dy is not None:
        variants.append((f"≤{args.max_dy:g}%", args.max_dy, 0))
    if args.min_div3:
        variants.append((f"≥{args.min_div3}次", args.max_dy, args.min_div3))
    # 引擎内扣税（D4）：**在嵌套循环外加载一次**，别在循环里重复读表
    div_tax = load_div_tax(args.div_tax) if args.tax_rate else None

    for vtag, max_dy, min_div3 in variants:
        for mode in args.modes:
            for n in args.topn:
                mask = build_mask(df, dates, by_date, mode=mode, topn=n,
                                  hold=args.hold, dy_col=args.dy_col,
                                  entry=args.entry, exit_=args.exit,
                                  min_dy=args.min_dy, max_dy=max_dy,
                                  min_div3=min_div3)
                col = "_div_sig"
                df[col] = mask
                eq, tr, meta = run(df, [], start=args.start, end=args.end,
                                   hold=args.hold, cond_col=col,
                                   min_price=args.min_price,
                                   min_amount=args.min_amount,
                                   min_listed=args.min_listed,
                                   div_tax=div_tax, tax_rate=args.tax_rate)
                m = metrics(eq.equity)
                years = len(eq) / 244.0
                n_hold = meta.get("avg_hold", 0) or 1
                turnover = ((int((tr.side == "buy").sum()) / years) / n_hold
                            if len(tr) else 0)
                rows.append({"过滤": vtag, "模式": mode, "持仓上限": n,
                             "年化%": m["年化收益"], "波动%": m["年化波动"],
                             "夏普": m["夏普比率"], "回撤%": m["最大回撤"],
                             "累计%": m["累计收益"], "平均持仓": n_hold,
                             "年单边换手": turnover, "交易笔数": len(tr)})
                print(f"  [{vtag:<7} {mode:<4} N={n:<3}] 年化 {m['年化收益']:>6.2f}%  "
                      f"夏普 {m['夏普比率']:>5.2f}  回撤 {m['最大回撤']:>7.2f}%  "
                      f"持仓 {n_hold:>5.1f}  换手 {turnover:>5.2f}x  "
                      f"交易 {len(tr):>5,} 笔", flush=True)

    r = pd.DataFrame(rows)

    print("\n" + "=" * 96)
    print("  计入「最低佣金 5 元」后的**真实年化**（%）")
    print("=" * 96)
    print("  " + "{:<20}".format("配置") + "".join(f"{c:>9.0f}万" for c in args.capitals))
    best_by_cap = {}
    for _, x in r.iterrows():
        tag = f"{x.过滤} {x.模式} N={int(x.持仓上限)}"
        line = "  {:<20}".format(tag)
        for cap in args.capitals:
            W = cap * 1e4
            ticket = W / max(x.平均持仓, 1.0)
            comm = max(NOMINAL_FEE, MIN_COMMISSION / ticket)
            real_round = (comm + SLIP) + (comm + STAMP + SLIP)
            extra = x.年单边换手 * (real_round - MODELED_ROUND) * 100
            real = x["年化%"] - extra
            line += f"{real:>10.2f}"
            if real > best_by_cap.get(cap, (-1e9, None))[0]:
                best_by_cap[cap] = (real, tag)
        print(line)

    # 基准
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
        if benches:
            print("\n  同期可投资指数年化："
                  + "  ".join(f"{k} {v:.2f}%" for k, v in benches.items()))

    print("\n" + "=" * 96)
    print("  各资金档最优配置")
    print("=" * 96)
    for cap in args.capitals:
        real, cfg = best_by_cap.get(cap, (np.nan, "—"))
        line = f"  {cap:>6.0f}万 → 最优 {cfg:<14} 真实年化 {real:>6.2f}%"
        for nm, v in benches.items():
            line += f"   超额 {real - v:>+6.2f}pp vs {nm}"
        print(line)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    r.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"\n  结果已写出 {args.out}")
    print("  说明：真实年化 = 回测年化 − 最低佣金带来的额外成本；"
          "换手越低，最低佣金惩罚越小 —— 这正是「低频」的价值所在。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
