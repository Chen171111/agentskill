"""短线强势股策略：日线版回测引擎。

为什么是这个设计
----------------
用户约束：**短线 / 资金 10 万 / 持仓少（5~10 只）**。

这决定了**不能**走高广度弱信号路线（那条路要 2000+ 只持仓、2000 万门槛，
见 docs/落地决策_资金10万.md）。必须走「**少而精 + 短持有**」的强信号路线。

策略原型参考聚宽「182.76% 策略」（尾盘打板）的**日线可复现部分**：
- **涨停基因**：20 日内有涨停 → 有资金关注（聚宽核心条件）
- **均线多头**：MA5 > MA10 > MA20 → 趋势向上
- **量能**：量比放大 / 3 日缩量整理
- **规模与流动性**：用 20 日均**成交额**替代市值（本项目无股本数据）
- **尾盘强度**：close/high 作为「分时收盘强势」的日线代理

无分钟数据 → 聚宽的 VWAP / 14:20 分时突破部分**不做**（已如实标注）。

成本口径（主人实际）
--------------------
买：佣金 max(万5, 5元) + 滑点 0.05%
卖：佣金 max(万5, 5元) + 滑点 0.05% + **印花税 0.1%**（仅股票）
→ 10 万 + 5 只 ≈ 每笔 2 万 → 不触发最低佣金，成本可控。

用法
----
    $PY tools/backtest_shortterm.py --bars data/stockbars/bars_all.parquet \
        --universe data/stockbars/universe_all.csv --start 20190101 --end 20260911 \
        --topk 5 --hold 5
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import build_features, metrics, TRADING_DAYS  # noqa: E402


# ============================================================ 因子构建
def build_short_features(bars: pd.DataFrame) -> pd.DataFrame:
    """在 build_features 基础上追加短线因子。所有因子统一「越大越看好」。"""
    df = build_features(bars)
    g = df.groupby("code", sort=False)

    # ---- 均线 ----
    for n in (5, 10, 20):
        df[f"ma{n}"] = g.close.transform(lambda s, n=n: s.rolling(n).mean())

    # ---- 涨停判定：最高价触及涨停价即视为「当日涨停过」----
    up_limit = (df.prev_close * (1 + df.limit)).round(2)
    df["touched_limit"] = df.high >= (up_limit - 0.005)
    df["lu_20"] = g.touched_limit.transform(lambda s: s.rolling(20).sum())
    df["lu_60"] = g.touched_limit.transform(lambda s: s.rolling(60).sum())

    # ---- 短期动量 ----
    df["ret5"] = g.close.transform(lambda s: s / s.shift(5) - 1)
    df["ret10"] = g.close.transform(lambda s: s / s.shift(10) - 1)

    # ---- 量能 ----
    df["vol_ma5"] = g.volume.transform(lambda s: s.rolling(5).mean())
    df["vol_ma20"] = g.volume.transform(lambda s: s.rolling(20).mean())
    df["vol_ratio"] = df.volume / df.vol_ma5.replace(0, np.nan)      # 量比(相对5日)

    # 3 日缩量：vol[-3] > vol[-2] > vol[-1]（聚宽条件，洗盘特征）
    v1, v2, v3 = g.volume.shift(1), g.volume.shift(2), g.volume.shift(3)
    df["vol_shrink3"] = (v3 > v2) & (v2 > v1)

    # ---- 尾盘强度（日线代理）----
    df["close_strength"] = (df.close / df.high.replace(0, np.nan)).clip(0, 1)

    # ---- 均线多头 ----
    df["ma_bull"] = (df.ma5 > df.ma10) & (df.ma10 > df.ma20)

    # ---- 偏离 MA20（追高风险度量）----
    df["bias20"] = df.close / df.ma20 - 1

    return df


# ============================================================ 指数择时
def load_index_ma(path: str, window: int = 20) -> pd.DataFrame:
    """读指数日线，返回 {date: 是否站上 MA}。用于大盘门。"""
    d = pd.read_csv(path, dtype={"date": str})
    d["date"] = d.date.str.replace("-", "", regex=False)
    d = d.sort_values("date")
    col = "close" if "close" in d.columns else d.columns[-1]
    d["ma"] = d[col].astype(float).rolling(window).mean()
    d["above"] = d[col].astype(float) > d["ma"]
    return d.set_index("date")[["above"]]


# ============================================================ 回测
def run_short(df, *, start, end, topk=5, hold=5, score_col="vol_ratio",
              score_asc=False,
              min_listed=120, min_price=2.0, min_amt=1e8, max_amt=3e9,
              min_lu=1, max_lu=None, require_ma_bull=True,
              require_vol_shrink=None, min_close_strength=None,
              max_bias20=None, min_ret5=None, max_ret5=None,
              min_ret1=None, max_ret1=None,
              index_above=None,
              cash0=100_000.0, commission=0.0005, min_comm=5.0,
              stamp=0.001, slippage=0.0005,
              min_equity=3000.0,
              weight_mode="equal", writeoff_factor=1.0):
    """日频调仓：T 日收盘出信号 → T+1 开盘成交。

    成本含**单笔最低佣金**，因此当净值缩到很小时，费用占比会急剧放大。
    `min_equity`：净值低于该值即视为「已失效」，清仓后停止交易
    （否则会出现「卖出金额 < 手续费 → 现金变负」的非物理结果）。

    返回 (净值 DataFrame, 成交流水, 元信息)。
    """
    d = df[(df.date >= start) & (df.date <= end)]
    dates = sorted(d.date.unique())
    if len(dates) < hold * 4:
        raise ValueError("区间过短")

    need = ["date", "code", "open", "close", "high", "prev_close", "suspended",
            "buy_blocked", "sell_blocked", "listed", "amt_ma20", "volume",
            score_col]
    for extra in ("ma5", "ma10", "ma20", "lu_20", "vol_shrink3", "close_strength",
                  "bias20", "ret5", "vol_ratio", "ret1", "ma_bull"):
        if extra in df.columns and extra not in need:
            need.append(extra)
    need = [c for c in need if c in d.columns]
    d = d[need].copy()

    by_date = {dt: g for dt, g in d.groupby("date", sort=True)}
    cache = {}

    def view(dt):
        if dt not in cache:
            cur = by_date[dt]
            idx = cur.set_index("code")
            cache[dt] = {
                "open": idx["open"].to_dict(), "close": idx["close"].to_dict(),
                "susp": idx["suspended"].to_dict(), "bb": idx["buy_blocked"].to_dict(),
                "sb": idx["sell_blocked"].to_dict(),
            }
            if len(cache) > 8:
                for k in list(cache)[:-4]:
                    cache.pop(k, None)
        return cache[dt]

    cash, units = cash0, {}
    nav_hist, nav_dates, hold_sizes, trades = [], [], [], []
    pending = None
    last_close, last_seen = {}, {}
    n_writeoff = 0
    MAX_GAP = 60
    rebal_pos = set(range(0, len(dates) - 1, hold))

    for i, dt in enumerate(dates):
        cur = by_date[dt]
        v = view(dt)

        # ---- 0) 退市了结 ----
        for c in list(units):
            if c in v["close"]:
                last_seen[c] = i
            elif i - last_seen.get(c, i) > MAX_GAP:
                px = last_close.get(c, 0.0) * writeoff_factor
                amt = units.pop(c) * px
                cash += amt - _fee(amt, sell=True, commission=commission,
                                   min_comm=min_comm, stamp=stamp, slippage=slippage)
                trades.append((dt, c, "writeoff"))
                n_writeoff += 1

        # ---- 1) 执行上一日信号 ----
        if pending is not None:
            sel_w = pending
            sel = {c for c, _ in sel_w}
            pending = None
            tot_open = cash
            for c, u in units.items():
                px = v["open"].get(c) or last_close.get(c)
                tot_open += u * px if px else 0.0
            # 先卖
            for c in list(units):
                if c in sel:
                    continue
                if v["susp"].get(c, True) or v["sb"].get(c, True):
                    continue
                op = v["open"].get(c)
                if not op:
                    continue
                amt = units.pop(c) * op
                cash += amt - _fee(amt, sell=True, commission=commission,
                                   min_comm=min_comm, stamp=stamp, slippage=slippage)
                trades.append((dt, c, "sell"))
            # 再买
            for c, w in sel_w:
                if c in units:
                    continue
                if v["susp"].get(c, True) or v["bb"].get(c, True):
                    continue
                op = v["open"].get(c)
                if not op or op <= 0:
                    continue
                alloc = min(tot_open * w, cash)
                if alloc <= 1e-6:
                    continue
                fee = _fee(alloc, sell=False, commission=commission,
                           min_comm=min_comm, stamp=stamp, slippage=slippage)
                units[c] = (alloc - fee) / op
                cash -= alloc
                trades.append((dt, c, "buy"))

        # ---- 2) 记账 ----
        nav = cash
        for c, u in units.items():
            px = v["close"].get(c)
            if px:
                last_close[c] = px
            nav += u * last_close.get(c, 0.0)
        nav_hist.append(nav)
        nav_dates.append(dt)
        hold_sizes.append(len(units))

        # ---- 3) 收盘出信号 ----
        if i in rebal_pos:
            gate_ok = True
            if index_above is not None:
                gate_ok = bool(index_above.get(dt, True))
            if nav_hist and nav_hist[-1] < min_equity:
                pending = []                      # 资金已被手续费吃穿 → 清仓停手
            elif not gate_ok:
                pending = []                      # 大盘门关闭 → 空仓
            else:
                cs = cur.copy()
                m = (cs.listed >= min_listed) & (cs.close >= min_price)
                m &= (~cs.suspended) & (cs.amt_ma20 >= min_amt) & (cs.amt_ma20 <= max_amt)
                if "lu_20" in cs.columns:
                    m &= cs.lu_20 >= min_lu
                    if max_lu is not None:
                        m &= cs.lu_20 <= max_lu
                if require_ma_bull and "ma_bull" in cs.columns:
                    m &= cs.ma_bull.fillna(False)
                if min_ret1 is not None and "ret1" in cs.columns:
                    m &= cs.ret1 >= min_ret1
                if max_ret1 is not None and "ret1" in cs.columns:
                    m &= cs.ret1 <= max_ret1
                if require_vol_shrink is True and "vol_shrink3" in cs.columns:
                    m &= cs.vol_shrink3.fillna(False)
                if min_close_strength is not None and "close_strength" in cs.columns:
                    m &= cs.close_strength >= min_close_strength
                if max_bias20 is not None and "bias20" in cs.columns:
                    m &= cs.bias20 <= max_bias20
                if min_ret5 is not None and "ret5" in cs.columns:
                    m &= cs.ret5 >= min_ret5
                if max_ret5 is not None and "ret5" in cs.columns:
                    m &= cs.ret5 <= max_ret5
                cs = cs[m].dropna(subset=[score_col])

                if len(cs) >= 1:
                    sel = cs.sort_values(score_col, ascending=score_asc).head(topk)
                    if weight_mode == "equal":
                        wv = np.ones(len(sel))
                    else:                          # rank: 分数越高权重越大
                        sc = sel[score_col].values.astype(float)
                        if score_asc:
                            sc = -sc
                        sc = sc - sc.min() + 1e-9
                        wv = sc
                    wv = wv / wv.sum()
                    pending = list(zip(sel.code.tolist(), wv.tolist()))

    eq = pd.DataFrame({"date": nav_dates, "equity": nav_hist}).set_index("date")
    eq["rate"] = eq.equity.pct_change().fillna(0.0)
    meta = {"n_writeoff": n_writeoff, "n_trades": len(trades),
            "avg_hold": float(np.mean(hold_sizes)) if hold_sizes else 0.0}
    return eq, pd.DataFrame(trades, columns=["date", "code", "side"]), meta


def _fee(amount: float, *, sell: bool, commission: float, min_comm: float,
         stamp: float, slippage: float) -> float:
    """A 股单笔费用：佣金 max(费率, 最低) + 滑点 (+印花税，仅卖出)。"""
    comm = max(amount * commission, min_comm)
    fee = comm + amount * slippage
    if sell:
        fee += amount * stamp
    return fee


# ============================================================ CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="短线强势股策略回测")
    ap.add_argument("--bars", required=True)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--index", default="data/indexes/000300.SH.csv")
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260911")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--hold", type=int, default=5)
    ap.add_argument("--score", default="vol_ratio")
    ap.add_argument("--score-asc", action="store_true")
    ap.add_argument("--min-amt", type=float, default=1e8)
    ap.add_argument("--max-amt", type=float, default=3e9)
    ap.add_argument("--min-lu", type=int, default=1)
    ap.add_argument("--max-lu", type=int, default=None)
    ap.add_argument("--no-ma-bull", action="store_true")
    ap.add_argument("--vol-shrink", action="store_true")
    ap.add_argument("--min-close-strength", type=float, default=None)
    ap.add_argument("--max-bias20", type=float, default=None)
    ap.add_argument("--min-ret5", type=float, default=None)
    ap.add_argument("--max-ret5", type=float, default=None)
    ap.add_argument("--min-ret1", type=float, default=None, help="当日涨幅下限(小数)")
    ap.add_argument("--max-ret1", type=float, default=None, help="当日涨幅上限(小数)")
    ap.add_argument("--index-gate", action="store_true", help="沪深300 站上MA20 才持仓")
    ap.add_argument("--cash", type=float, default=100_000.0)
    ap.add_argument("--dump", default=None)
    args = ap.parse_args(argv)

    bars = pd.read_parquet(args.bars)
    bars["date"] = bars.date.astype(str).str.replace("-", "", regex=False)
    if args.universe and os.path.exists(args.universe):
        u = pd.read_csv(args.universe, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
        n0 = bars.code.nunique()
        bars = bars[~bars.code.isin(st)]
        print(f"剔除 ST {len(st)} 只：{n0} -> {bars.code.nunique()} 只")

    print(f"日线 {len(bars):,} 行 / {bars.code.nunique()} 只 / "
          f"{bars.date.nunique()} 交易日 ({bars.date.min()}~{bars.date.max()})")
    print("计算因子…", flush=True)
    df = build_short_features(bars)

    index_above = None
    if args.index_gate and os.path.exists(args.index):
        idx = load_index_ma(args.index)
        index_above = idx["above"].to_dict()
        print(f"大盘门：沪深300 MA20（{len(index_above)} 日）")

    print(f"\n回测 {args.start}~{args.end}  topk={args.topk}  持有{args.hold}日  "
          f"打分={args.score}{'(升序)' if args.score_asc else '(降序)'}")
    print(f"过滤：成交额 {args.min_amt/1e8:.1f}~{args.max_amt/1e8:.0f}亿  "
          f"20日涨停≥{args.min_lu}  MA多头={not args.no_ma_bull}  "
          f"大盘门={args.index_gate}  本金={args.cash:,.0f}")
    print("=" * 106)

    eq, tr, meta = run_short(
        df, start=args.start, end=args.end, topk=args.topk, hold=args.hold,
        score_col=args.score, score_asc=args.score_asc,
        min_amt=args.min_amt, max_amt=args.max_amt, min_lu=args.min_lu,
        max_lu=args.max_lu, require_ma_bull=not args.no_ma_bull,
        require_vol_shrink=True if args.vol_shrink else None,
        min_close_strength=args.min_close_strength, max_bias20=args.max_bias20,
        min_ret5=args.min_ret5, max_ret5=args.max_ret5,
        min_ret1=args.min_ret1, max_ret1=args.max_ret1,
        index_above=index_above, cash0=args.cash)
    m = metrics(eq.equity)
    _print_metrics("短线策略", m, eq.equity)

    # 基准：等权全池
    bench = []
    for dt, g in df[(df.date >= args.start) & (df.date <= args.end)].groupby("date"):
        u = ((g.listed >= 120) & (g.amt_ma20 >= args.min_amt)
             & (g.close >= 2.0) & (~g.suspended))
        bench.append((dt, float(g.loc[u, "ret1"].mean())))
    b = pd.DataFrame(bench, columns=["date", "r"]).set_index("date").dropna()
    beq = (1 + b.r).cumprod() * args.cash
    bm = metrics(beq)
    _print_metrics("等权全池基准", bm, beq)

    print("\n  超额: 年化 {:+.2f}pp   夏普 {:+.2f}   回撤 {:+.2f}pp".format(
        m.get("年化收益", 0) - bm.get("年化收益", 0),
        m.get("夏普比率", 0) - bm.get("夏普比率", 0),
        m.get("最大回撤", 0) - bm.get("最大回撤", 0)))

    if len(tr):
        nb = int((tr.side == "buy").sum())
        ns = int((tr.side == "sell").sum())
        print(f"\n  交易 {len(tr)} 笔（买 {nb} / 卖 {ns} / 退市了结 {meta['n_writeoff']}）")
        print(f"  平均持仓 {meta['avg_hold']:.1f} 只")

    # 分年度
    print("\n=== 分年度 ===")
    eq2 = eq.copy()
    eq2["yr"] = eq2.index.str[:4]
    b2 = beq.to_frame("equity")
    b2["yr"] = b2.index.str[:4]
    for yr in sorted(eq2.yr.unique()):
        s = eq2[eq2.yr == yr].equity
        sb = b2[b2.yr == yr].equity
        if len(s) < 20:
            continue
        ms, mb = metrics(s), metrics(sb)
        print("  {}: 策略年化 {:>+8.2f}%  基准 {:>+8.2f}%  超额 {:>+8.2f}pp  "
              "(回撤 {:.1f}% vs {:.1f}%)".format(
                  yr, ms.get("年化收益", 0), mb.get("年化收益", 0),
                  ms.get("年化收益", 0) - mb.get("年化收益", 0),
                  ms.get("最大回撤", 0), mb.get("最大回撤", 0)))

    if args.dump:
        out = eq.copy()
        out["bench"] = beq.reindex(out.index)
        out.to_parquet(args.dump)
        print(f"\n净值已写出 {args.dump}")
    return 0


def _print_metrics(tag, m, eq):
    yrs = len(eq) / TRADING_DAYS
    total = eq.iloc[-1] / eq.iloc[0] - 1
    print("  {:<16} 年化 {:>7.2f}%  累计 {:>9.2f}%  波动 {:>6.2f}%  "
          "夏普 {:>5.2f}  回撤 {:>7.2f}%  卡玛 {:>5.2f}".format(
              tag, m.get("年化收益", 0), total * 100, m.get("年化波动", 0),
              m.get("夏普比率", 0), m.get("最大回撤", 0), m.get("卡玛比率", 0)))


if __name__ == "__main__":
    sys.exit(main())
