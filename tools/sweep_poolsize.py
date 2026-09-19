"""个股多因子：持仓数压缩扫描（tilt_min × 流动性门槛）。

为什么需要它
------------
tilt(>0.45) 实测持仓 **2195 只**，资金门槛 3,658 万元 —— 这是它落不了地的
根本原因（券商单笔最低佣金 5 元，持仓越分散每笔越小，费率被抬得越高）。

但直接换成 topk300 会把夏普从 0.86 打到 0.66、回撤从 −22% 打到 −39%
（见 `results/feasibility.csv`），防御性基本丢光。

所以要在**保住分散度**的前提下把持仓数压下来。两条路：
1. 抬 `tilt_min`  —— 只留高分股，池子自然变小
2. 抬 `min_amount` —— 砍掉流动性差的小票（它们也正是最低佣金伤害最大的那批）

判据：找「持仓 500~1000 只、夏普尽量接近 0.86、门槛尽量低」的配置。

用法
----
    $PY tools/sweep_poolsize.py --bars data/stockbars/bars.parquet \
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

TILT_MINS = (0.45, 0.50, 0.55, 0.60, 0.65, 0.70)
MIN_AMOUNTS = (3e7, 1e8)

# 成本模型统一到 tools/costs.py（**单一来源**，勿在此重定义 —— 铁律 14）。
# ⚠️ 本文件曾是**唯一的分叉点**：`NOMINAL_FEE` 停在 0.0003（万3），
# 导致 `BREAK_EVEN_TICKET` 被算成 16,667 元（正确 10,000 元）。收敛后不会再复发。
from tools.costs import (BREAK_EVEN_TICKET, MIN_COMMISSION,  # noqa: E402
                         NOMINAL_FEE)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260911")
    ap.add_argument("--out", default="results/poolsize.csv")
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
    print("=" * 122)
    print("  {:<9} {:<9} {:>7} {:>7} {:>6} {:>9} {:>6} {:>7} {:>8} {:>10} {:>11}".format(
        "tilt_min", "流动性", "年化%", "波动%", "夏普", "回撤%", "卡玛",
        "持仓数", "年换手", "交易笔数", "门槛(万)"))
    print("=" * 122)

    for ma in MIN_AMOUNTS:
        for tm in TILT_MINS:
            try:
                eq, tr, meta = run(df, ALL8, start=args.start, end=args.end,
                                   topk=50, hold=5, weight_mode="tilt",
                                   tilt_min=tm, min_amount=ma)
            except Exception as e:
                print(f"  tilt_min={tm} amt={ma:.0e}  失败: {type(e).__name__}: {e}",
                      flush=True)
                continue
            m = metrics(eq.equity)
            n_hold = meta.get("avg_hold", 0.0) or 1.0
            n_buy = int((tr.side == "buy").sum()) if len(tr) else 0
            turnover = (n_buy / years) / n_hold
            w_min = BREAK_EVEN_TICKET * n_hold / 1e4
            print("  {:<9} {:<9} {:>7.2f} {:>7.2f} {:>6.2f} {:>9.2f} {:>6.2f} "
                  "{:>7.0f} {:>7.1f}x {:>10,} {:>11,.0f}".format(
                      f"{tm:.2f}", f"{ma/1e7:.0f}千万" if ma < 1e8 else f"{ma/1e8:.0f}亿",
                      m.get("年化收益", 0), m.get("年化波动", 0),
                      m.get("夏普比率", 0), m.get("最大回撤", 0),
                      m.get("卡玛比率", 0), n_hold, turnover,
                      len(tr), w_min), flush=True)
            rows.append({"tilt_min": tm, "min_amount": ma,
                         "年化收益": m.get("年化收益", 0),
                         "年化波动": m.get("年化波动", 0),
                         "夏普比率": m.get("夏普比率", 0),
                         "最大回撤": m.get("最大回撤", 0),
                         "卡玛比率": m.get("卡玛比率", 0),
                         "平均持仓": n_hold, "年单边换手": turnover,
                         "交易笔数": len(tr), "资金门槛万元": w_min})
            pd.DataFrame(rows).to_csv(args.out, index=False, encoding="utf-8-sig")

    print("=" * 122)
    if rows:
        r = pd.DataFrame(rows).sort_values("平均持仓")
        print("\n按持仓数升序 —— 找「持仓少但夏普没塌」的拐点：")
        print("  {:>8} {:>9} {:>7} {:>7} {:>11}".format(
            "持仓数", "tilt_min", "夏普", "回撤%", "门槛(万)"))
        for _, x in r.iterrows():
            print("  {:>8.0f} {:>9.2f} {:>7.2f} {:>7.2f} {:>11,.0f}".format(
                x.平均持仓, x.tilt_min, x.夏普比率, x.最大回撤, x.资金门槛万元))
        print(f"\n门槛 = 16,667 元 × 平均持仓数")
        print(f"结果已写出 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
