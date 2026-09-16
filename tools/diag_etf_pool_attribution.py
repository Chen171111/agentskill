"""扩池归因：C/D 池的优势是「轮动策略」带来的，还是只是「池子 beta」？

问题
----
`tools/sweep_etf_pool_fair.py` 发现：2022-01~2026-09 同区间上
- 现有 ETF全球(11只)：年化 2.32% / 夏普 0.33 / 波动 7.79%
- C ≤2019(21只)：      年化 6.55% / 夏普 0.54 / 波动 13.29%
- D ≤2021(25只)：      年化 6.51% / 夏普 0.53 / 波动 13.59%

但扩池同时把**波动**抬了一倍（7.8% → 13.3%）。
所以必须回答：**收益提升是"轮动选得更好"，还是"池子本身涨得更多"？**

判据
----
对每个池子算**等权买入持有**（首日等权买入全池、持有到底、不调仓、不计成本）。
- 若「买入持有」在 C/D 上同样远好于现有池 → 优势来自**池子构成（beta 敞口）**，
  与 `etf_rotation` 策略无关；换池等于**加杠杆式提高风险敞口**
- 若「买入持有」差异不大、而「轮动」差异大 → 才是**策略真的更会选**

用法
----
    $PY tools/diag_etf_pool_attribution.py
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                              # noqa: E402
from pipeline import run_backtest                          # noqa: E402
from analysis.metrics import compute_metrics              # noqa: E402

STRATEGY = "etf_rotation"
TOP_K = 5
REBALANCE = 5
CASH = 100_000.0
S, E = "20220101", "20260911"


def load_close(code):
    p = f"data/stocks/{code}.csv"
    if not os.path.exists(p):
        return None
    d = pd.read_csv(p, dtype={"date": str})[["date", "close"]]
    d["date"] = d.date.str.replace("-", "", regex=False)
    return d.set_index("date").close.astype(float)


def buy_hold(codes):
    """等权买入持有：首日等权，末日估值。返回净值序列。"""
    series = {}
    for c in codes:
        s = load_close(c)
        if s is not None:
            series[c] = s
    if not series:
        return None
    df = pd.DataFrame(series).sort_index()
    df = df[(df.index >= S) & (df.index <= E)].dropna(how="all")
    df = df.ffill().dropna()
    if len(df) < 20:
        return None
    ret = df.pct_change().fillna(0.0).mean(axis=1)     # 等权日收益
    nav = (1 + ret).cumprod() * CASH
    return pd.DataFrame({"equity": nav, "value": nav})


def main() -> int:
    allc = []
    for f in sorted(os.listdir("data/stocks")):
        if f.endswith(".csv") and f[:-4].split(".")[0][:2] in ("51", "56", "58", "15", "16"):
            code = f[:-4]
            s = load_close(code)
            if s is not None:
                allc.append((code, s.index.min()))
    start = dict(allc)
    allcodes = sorted(start)

    pools = {
        "现有 ETF全球(11)": config.RECOMMENDED_POOLS["ETF全球"],
        "C ≤2019(21)": [c for c in allcodes if start[c] <= "20191231"],
        "D ≤2021(25)": [c for c in allcodes if start[c] <= "20211231"],
    }

    print(f"区间 {S} ~ {E}  （策略：{STRATEGY} topk={TOP_K} {REBALANCE}日调仓）\n")
    print("  {:<18}{:>7}{:>26}{:>26}{:>12}".format(
        "池子", "标的", "轮动策略 年化/夏普", "等权买入持有 年化/夏普", "轮动−持有"))
    print("  " + "-" * 90)

    for name, cs in pools.items():
        r = run_backtest(codes=cs, strategy=STRATEGY, start=S, end=E,
                         topk=TOP_K, rebalance=REBALANCE, init_cash=CASH)
        m = r["metrics"]
        bh = buy_hold(cs)
        bm = compute_metrics(bh) if bh is not None else {}
        d = m.get("年化收益", 0) - bm.get("年化收益", 0)
        print("  {:<18}{:>7}{:>26}{:>26}{:>+12.2f}".format(
            name, len(cs),
            f"{m.get('年化收益',0):.2f}% / {m.get('夏普比率',0):.2f}",
            f"{bm.get('年化收益',0):.2f}% / {bm.get('夏普比率',0):.2f}", d))
        print("  {:<18}{:>7}{:>26}{:>26}".format(
            "", "", f"波动 {m.get('年化波动',0):.2f}%  回撤 {m.get('最大回撤',0):.2f}%",
            f"波动 {bm.get('年化波动',0):.2f}%  回撤 {bm.get('最大回撤',0):.2f}%"))

    print()
    print("判读：")
    print("  · 若 C/D 的「买入持有」同样远好于现有池 → 优势来自**池子 beta**（扩池=加大风险敞口）")
    print("  · 若「轮动−持有」差值在 C/D 上更大 → 才是**策略更会选**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
