"""短线策略：一次算因子，批量对比多种选股逻辑。

动机
----
首版 baseline 用 `vol_ratio` **降序**选股 → 专挑当日放量最猛的（= 涨停/巨量股）
→ 次日开盘买入即**追高接盘**，7.7 年净值从 10 万衰减到 0。

本脚本在同一份因子上并行对比多组「选股逻辑」，找出真正有正超额的那一种。
因子只构建一次（约 40s），每组回测只要几秒。

用法
----
    $PY tools/sweep_shortterm.py --bars data/stockbars/bars_all.parquet \
        --universe data/stockbars/universe_all.csv --start 20190101 --end 20260911
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import metrics  # noqa: E402
from tools.backtest_shortterm import build_short_features, run_short, load_index_ma  # noqa: E402


def build_configs(index_above):
    """(名称, kwargs)。共同前提：成交额 1~30 亿 / 20日有涨停 / 均线多头。"""
    base = dict(min_amt=1e8, max_amt=3e9, min_lu=1, require_ma_bull=True)
    zero = dict(commission=0.0, stamp=0.0, slippage=0.0, min_comm=0.0)
    return [
        ("A 基线 vol_ratio降序(追高)", dict(base, score_col="vol_ratio")),
        ("E 贴近MA20(不追高)", dict(base, score_col="bias20", score_asc=True)),
        ("J 温和上涨+大盘门", dict(base, score_col="close_strength",
                                 min_ret1=0.03, max_ret1=0.07, index_above=index_above)),
        # ---- 对照实验：隔离「成本」与「选股 alpha」----
        ("Z1 零成本·贴近MA20", dict(base, score_col="bias20", score_asc=True, **zero)),
        ("Z2 零成本·基线", dict(base, score_col="vol_ratio", **zero)),
        ("Z3 随机选股(对照)", dict(base, score_col="rand")),
        ("Z4 零成本·随机选股", dict(base, score_col="rand", **zero)),
        # ---- 降换手：持有期拉长 ----
        ("Y1 贴近MA20·持20日", dict(base, score_col="bias20", score_asc=True, hold=20)),
        ("Y2 贴近MA20·持20日·零成本", dict(base, score_col="bias20", score_asc=True,
                                        hold=20, **zero)),
        ("Y3 随机·持20日·零成本", dict(base, score_col="rand", hold=20, **zero)),
    ]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--index", default="data/indexes/000300.SH.csv")
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260911")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--hold", type=int, default=5)
    ap.add_argument("--out", default="results/shortterm_sweep.csv")
    args = ap.parse_args(argv)

    bars = pd.read_parquet(args.bars)
    bars["date"] = bars.date.astype(str).str.replace("-", "", regex=False)
    if args.universe and os.path.exists(args.universe):
        u = pd.read_csv(args.universe, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
        bars = bars[~bars.code.isin(st)]
    print(f"日线 {len(bars):,} 行 / {bars.code.nunique()} 只", flush=True)

    print("计算因子…", flush=True)
    df = build_short_features(bars)
    rng = np.random.default_rng(42)
    df["rand"] = rng.random(len(df))          # 随机选股对照，用于隔离引擎/成本影响

    index_above = None
    if os.path.exists(args.index):
        index_above = load_index_ma(args.index)["above"].to_dict()

    print(f"\n区间 {args.start}~{args.end}  topk={args.topk}  持有{args.hold}日")
    print("=" * 122)
    print("  {:<30} {:>8} {:>8} {:>7} {:>9} {:>7} {:>7} {:>7}".format(
        "配置", "年化%", "波动%", "夏普", "回撤%", "持仓", "交易数", "末净值"))
    print("=" * 122)

    rows = []
    for name, kw in build_configs(index_above):
        try:
            call = dict(hold=args.hold)
            call.update(kw)
            eq, tr, meta = run_short(df, start=args.start, end=args.end,
                                     topk=args.topk, cash0=100_000.0, **call)
        except Exception as e:
            print("  {:<30} 失败: {}: {}".format(name, type(e).__name__, e))
            continue
        m = metrics(eq.equity)
        last = float(eq.equity.iloc[-1])
        print("  {:<30} {:>8.2f} {:>8.2f} {:>7.2f} {:>9.2f} {:>7.1f} {:>7,d} {:>9,.0f}".format(
            name, m.get("年化收益", 0), m.get("年化波动", 0), m.get("夏普比率", 0),
            m.get("最大回撤", 0), meta["avg_hold"], len(tr), last), flush=True)
        rows.append({"配置": name, "年化收益": m.get("年化收益", 0),
                     "年化波动": m.get("年化波动", 0), "夏普比率": m.get("夏普比率", 0),
                     "最大回撤": m.get("最大回撤", 0), "卡玛比率": m.get("卡玛比率", 0),
                     "平均持仓": meta["avg_hold"], "交易笔数": len(tr),
                     "末净值": last})

    print("=" * 122)
    if rows:
        pd.DataFrame(rows).to_csv(args.out, index=False, encoding="utf-8-sig")
        print(f"结果已写出 {args.out}")
        best = max(rows, key=lambda r: r["夏普比率"])
        print(f"\n夏普最高: {best['配置']}  年化 {best['年化收益']:.2f}%  夏普 {best['夏普比率']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
