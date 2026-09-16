"""把「市场宽度」信号接进 ETF 轮动线，做样本内外对照回测。

前置结论（tools/test_stock_timing.py）
--------------------------------------
个股因子模型的**自身输出**（分数广度 / 离散度 / 分数中位）对 ETF 池的预测力
**样本内外符号翻转，判为噪声**。
唯一通过检验的是 **`above_ma`（站上自身 MA60 的个股占比）**——一个**经典市场宽度
指标**，与个股因子模型无关，用任何价格数据都能算。方向为**反转**：
宽度越高 → 后市越弱（过热特征）。IC：h=5 样本内 −0.127 / 样本外 −0.062；
h=20 样本内 −0.195 / 样本外 −0.099。

本脚本回答：**把它接进已有的 ETF 轮动线，能不能真的提升？**

做法
----
不修改任何现有策略/引擎文件，在**本脚本内**动态注册一个包装策略
`BreadthGatedEtf`：继承 `EtfRotationStrategy`，在其产出权重后按宽度分位降仓。

严格性
------
- 分位阈值 q 与降仓系数只在**样本内**挑，样本外不得回头调
- 样本内 `2019-01-01~2022-12-31`，样本外 `2023-01-01~2026-09-11`
- 同时报全区间，但**以样本外为准**

用法
----
    $PY tools/test_etf_breadth_gate.py --bars data/stockbars/bars_all.parquet
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                            # noqa: E402
from pipeline import run_backtest                        # noqa: E402
from strategies.builtin import EtfRotationStrategy       # noqa: E402
from strategies.registry import register_strategy        # noqa: E402
from tools.backtest_stock import build_features          # noqa: E402

POOL_NAME = "ETF稳健"
IS_START, IS_END = "20190101", "20221231"
OOS_START, OOS_END = "20230101", "20260911"
BREADTH_CACHE = "results/breadth_series.csv"


class BreadthGatedEtf(EtfRotationStrategy):
    """ETF 轮动 + 市场宽度降仓门。

    宽度 = 全市场「收盘价 > 自身 MA60」的个股占比。
    取**历史滚动分位**（只用当日及之前的数据，无前视）：
    宽度处于历史高分位（过热）时，把目标权重乘 `scale`。
    """
    name = "etf_breadth"

    def __init__(self, breadth=None, q=0.80, win=500, scale=0.5, **kw):
        super().__init__(**kw)
        self._b = breadth
        self.q = float(q)
        self.win = int(win)
        self.scale = float(scale)

    def _gate(self, date) -> float:
        s = self._b
        if s is None or date not in s.index:
            return 1.0
        hist = s.loc[:date].dropna().tail(self.win)
        if len(hist) < 60:
            return 1.0
        return self.scale if float(s.loc[date]) >= float(hist.quantile(self.q)) else 1.0

    def generate_weights(self, date, factors, panel):
        w = super().generate_weights(date, factors, panel)
        if w is None or not w:
            return w
        k = self._gate(date)
        return w if k >= 1.0 else {c: v * k for c, v in w.items()}


def get_breadth(bars_path: str, universe: str) -> pd.Series:
    if os.path.exists(BREADTH_CACHE):
        s = pd.read_csv(BREADTH_CACHE, dtype={"date": str}).set_index("date").breadth
        print(f"宽度序列（缓存）: {len(s)} 日  {s.index.min()} ~ {s.index.max()}")
        return s
    bars = pd.read_parquet(bars_path)
    bars["date"] = bars.date.astype(str).str.replace("-", "", regex=False)
    if universe and os.path.exists(universe):
        u = pd.read_csv(universe, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
        bars = bars[~bars.code.isin(st)]
    print("计算因子…", flush=True)
    df = build_features(bars)
    d = df[(df.listed >= 120) & (df.amt_ma20 >= 3e7) & (df.close >= 2.0)
           & (~df.suspended)].copy()
    d["_above"] = (d.close > d.ma60).astype(float)
    s = d.groupby("date")._above.mean()
    os.makedirs("results", exist_ok=True)
    s.rename("breadth").to_frame().to_csv(BREADTH_CACHE, encoding="utf-8-sig")
    print(f"宽度序列: {len(s)} 日  {s.index.min()} ~ {s.index.max()}")
    return s


def show(tag, m):
    return ("  {:<26} 年化 {:>7.2f}%  波动 {:>6.2f}%  夏普 {:>5.2f}  "
            "回撤 {:>7.2f}%  卡玛 {:>5.2f}").format(
        tag, m.get("年化收益", 0), m.get("年化波动", 0), m.get("夏普比率", 0),
        m.get("最大回撤", 0), m.get("卡玛比率", 0))


def run_one(codes, strategy, params, s, e):
    r = run_backtest(codes=codes, strategy=strategy, start=s, end=e,
                     topk=5, rebalance=5, strategy_params=params)
    return r["metrics"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True)
    ap.add_argument("--universe", default=None)
    args = ap.parse_args(argv)

    register_strategy("etf_breadth", BreadthGatedEtf)
    breadth = get_breadth(args.bars, args.universe)
    codes = config.RECOMMENDED_POOLS[POOL_NAME]
    print(f"池子 {POOL_NAME}: {len(codes)} 只\n")

    base = {"risk_parity": True}

    print("=" * 104)
    print("=== 样本内 2019~2022（用这段挑参数）===")
    print(show("基线 etf_rotation", run_one(codes, "etf_rotation", base, IS_START, IS_END)))
    grid = []
    for q in (0.70, 0.80, 0.90):
        for sc in (0.0, 0.3, 0.5):
            m = run_one(codes, "etf_breadth",
                        {**base, "breadth": breadth, "q": q, "scale": sc},
                        IS_START, IS_END)
            grid.append((q, sc, m))
            print(show(f"宽度门 q={q} scale={sc}", m))
    best = max(grid, key=lambda t: t[2].get("夏普比率", -9))
    print(f"\n  → 样本内最优: q={best[0]}  scale={best[1]}  "
          f"夏普 {best[2].get('夏普比率', 0):.2f}")

    print("\n" + "=" * 104)
    print("=== 样本外 2023~2026（用样本内选出的参数，不得回头调）===")
    b_oos = run_one(codes, "etf_rotation", base, OOS_START, OOS_END)
    g_oos = run_one(codes, "etf_breadth",
                    {**base, "breadth": breadth, "q": best[0], "scale": best[1]},
                    OOS_START, OOS_END)
    print(show("基线 etf_rotation", b_oos))
    print(show(f"宽度门 q={best[0]} scale={best[1]}", g_oos))
    print("\n  样本外差异: 年化 {:+.2f}pp  夏普 {:+.2f}  回撤 {:+.2f}pp".format(
        g_oos.get("年化收益", 0) - b_oos.get("年化收益", 0),
        g_oos.get("夏普比率", 0) - b_oos.get("夏普比率", 0),
        g_oos.get("最大回撤", 0) - b_oos.get("最大回撤", 0)))

    print("\n" + "=" * 104)
    print("=== 全区间 2019~2026（参考）===")
    b_all = run_one(codes, "etf_rotation", base, IS_START, OOS_END)
    g_all = run_one(codes, "etf_breadth",
                    {**base, "breadth": breadth, "q": best[0], "scale": best[1]},
                    IS_START, OOS_END)
    print(show("基线 etf_rotation", b_all))
    print(show(f"宽度门 q={best[0]} scale={best[1]}", g_all))

    print("\n=== 判据 ===")
    print("  只有【样本外】年化与夏普同时改善、且回撤不变差，才算可用。")
    print("  样本内最优不算数——那是挑出来的。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
