"""个股多因子：持仓数 → 实盘资金门槛 的可行性测算。

为什么需要它
------------
回测用**比例成本**（买 0.03%、卖 0.13%、滑点 0.05%）——这个假设在小资金下
是错的。A 股券商有**单笔最低佣金 5 元**：单笔金额低于 16,667 元时，
0.03% 佣金不足 5 元，按 5 元收，实际费率被抬高到 5/单笔金额。

tilt 模式实测持仓 **2195 只**、每周换掉约 36%，年单边换手约 9.3 倍。
资金 100 万时每笔只有 455 元，佣金 5 元 = **1.1% 单边**，一年能吃掉 20%+。

本脚本把「持仓数 / 换手率 / 资金量 / 最低佣金」串起来，给出每种配置的
**资金门槛**：低于这个规模，回测里的成本假设就不成立，年化要额外打折。

用法
----
    $PY tools/sweep_feasibility.py --bars data/stockbars/bars.parquet \
        --universe data/stockbars/universe.csv
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

# 成本模型统一到 tools/costs.py（**单一来源**，勿在此重定义 —— 铁律 14）。
# `BREAK_EVEN_TICKET` = 最低佣金 / 名义费率 = 5 / 0.0005 = 10,000 元
from tools.costs import (BREAK_EVEN_TICKET, MIN_COMMISSION,  # noqa: E402
                         NOMINAL_FEE)

VARIANTS = [
    ("tilt(>0.45)", "tilt", 50, 0.45),
    ("topk1000 排名加权", "rank", 1000, 0.45),
    ("topk500 排名加权", "rank", 500, 0.45),
    ("topk300 排名加权", "rank", 300, 0.45),
    ("topk100 排名加权", "rank", 100, 0.45),
    ("topk50 排名加权", "rank", 50, 0.45),
    ("topk20 排名加权", "rank", 20, 0.45),
    ("topk10 排名加权", "rank", 10, 0.45),
]

# 用这些资金规模演示实盘成本（万元）—— 主人实际资金 10 万
CAPITALS = (10, 30, 100, 300, 1000)


def real_cost_rates(ticket: float, *, slippage=0.0005, stamp=0.001):
    """给定单笔金额，返回真实的（买费率, 卖费率），已计入最低佣金。"""
    comm = max(ticket * NOMINAL_FEE, MIN_COMMISSION) / ticket
    buy = comm + slippage
    sell = comm + stamp + slippage
    return buy, sell


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260911")
    ap.add_argument("--out", default="results/feasibility.csv")
    args = ap.parse_args(argv)

    bars = pd.read_parquet(args.bars)
    bars["date"] = bars.date.astype(str).str.replace("-", "", regex=False)
    if args.universe and os.path.exists(args.universe):
        u = pd.read_csv(args.universe, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
        bars = bars[~bars.code.isin(st)]
    print(f"日线: {len(bars):,} 行, {bars.code.nunique()} 只", flush=True)
    print("计算因子…", flush=True)
    df = build_features(bars)

    ndays = df[(df.date >= args.start) & (df.date <= args.end)].date.nunique()
    years = ndays / 244.0
    print(f"区间 {args.start}~{args.end}  {ndays} 交易日 ({years:.1f} 年)\n")

    rows = []
    print("=" * 112)
    print("  {:<18} {:>7} {:>7} {:>6} {:>9} {:>8} {:>10} {:>11}".format(
        "配置", "年化%", "波动%", "夏普", "回撤%", "持仓数", "年单边换手", "资金门槛(万)"))
    print("=" * 112)

    for tag, wmode, topk, tm in VARIANTS:
        eq, tr, meta = run(df, ALL8, start=args.start, end=args.end, topk=topk,
                           hold=5, weight_mode=wmode, tilt_min=tm)
        m = metrics(eq.equity)
        n_hold = meta.get("avg_hold", 0.0) or 1.0
        n_buy = int((tr.side == "buy").sum()) if len(tr) else 0
        # 年单边换手 = 每年买入金额 / 总资金。每笔买入 ≈ 1/持仓数 的资金
        turnover = (n_buy / years) / n_hold if n_hold else 0.0
        # 资金门槛：让每笔金额 >= BREAK_EVEN_TICKET，名义佣金才不被最低收费抬高
        w_min = BREAK_EVEN_TICKET * n_hold / 1e4
        print("  {:<18} {:>7.2f} {:>7.2f} {:>6.2f} {:>9.2f} {:>8.0f} {:>10.1f}x "
              "{:>11,.0f}".format(
                  tag, m.get("年化收益", 0), m.get("年化波动", 0),
                  m.get("夏普比率", 0), m.get("最大回撤", 0), n_hold,
                  turnover, w_min), flush=True)
        rows.append({"配置": tag, "weight_mode": wmode, "topk": topk,
                     "年化收益": m.get("年化收益", 0),
                     "年化波动": m.get("年化波动", 0),
                     "夏普比率": m.get("夏普比率", 0),
                     "最大回撤": m.get("最大回撤", 0),
                     "卡玛比率": m.get("卡玛比率", 0),
                     "平均持仓": n_hold, "买入笔数": n_buy,
                     "年单边换手": turnover, "资金门槛万元": w_min})
        pd.DataFrame(rows).to_csv(args.out, index=False, encoding="utf-8-sig")

    print("=" * 112)
    print(f"\n名义佣金 {NOMINAL_FEE:.2%} 与最低收费 {MIN_COMMISSION:.0f} 元的临界单笔金额 "
          f"= {BREAK_EVEN_TICKET:,.0f} 元")
    print("资金门槛 = 临界单笔金额 × 平均持仓数（低于此规模，实际费率高于回测假设）\n")

    # 各资金规模下的实盘成本侵蚀（年化百分点）
    print("实盘成本侵蚀（年化百分点，已扣除回测里本来就计过的比例成本）")
    print("  {:<18}".format("配置") + "".join("{:>12}".format(f"{c}万")
                                              for c in CAPITALS))
    for r in rows:
        line = "  {:<18}".format(r["配置"])
        for c in CAPITALS:
            w = c * 1e4
            ticket = w / max(r["平均持仓"], 1.0)
            buy, sell = real_cost_rates(ticket)
            real = r["年单边换手"] * (buy + sell) * 100
            # 回测里已扣掉的：年单边换手 × (0.03% + 0.13% + 0.05%×2)
            modeled = r["年单边换手"] * (0.0003 + 0.0013 + 0.0005 * 2) * 100
            line += "{:>12}".format(f"−{real - modeled:.1f}pp")
        print(line)
    print("\n  读法：−5pp 表示该资金规模下，真实成本比回测假设多吞掉 5 个点的年化。")

    # 给定资金下，最多能持有多少只（每只至少 BREAK_EVEN_TICKET 元，否则最低佣金吃掉收益）
    print("\n各资金规模下的「经济持仓上限」（每只 ≥ {:,} 元，否则被最低佣金反噬）"
          .format(int(BREAK_EVEN_TICKET)))
    for c in CAPITALS:
        n_max = int(c * 1e4 / BREAK_EVEN_TICKET)
        print("  {:>6}万 → 最多 {:>5} 只".format(c, n_max))

    print(f"\n结果已写出 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
