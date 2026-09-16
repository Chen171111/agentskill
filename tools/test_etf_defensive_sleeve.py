"""ETF 线：**战略防御仓位**的样本外检验。

动机（不是数据挖掘）
--------------------
`docs/ETF线_真实成本复核.md` 发现 `ETF防御`（黄金+红利+国债）在同区间夏普 1.07，
远高于实盘的 0.60。但那是**黄金牛市的事后结果**，不能据此换池。

本脚本检验的是一个**结构性假设**，而非历史排名：

> 在动量轮动组合里**永久保留一部分低相关资产**（黄金/红利/国债），
> 能否在不牺牲太多收益的前提下**系统性降低回撤**？

**为什么这是先验而非拟合**：黄金、红利、国债与股票类 ETF 的低相关性是
资产类别的**结构性属性**（宏观驱动不同），不是从这段样本里挑出来的。
若它成立，应当**在样本内外同时**表现为夏普/卡玛改善。

设计
----
    组合 = w × ETF轮动（动量选股）  +  (1−w) × 防御篮子（等权，永久持有）

`w` 从 0（现状）扫到 0.5。防御篮子 = `518880`(黄金) + `510880`(红利) + `511010`(国债)。

判据
----
- **样本内 2019~2022 与样本外 2023~2026 都要改善**才算成立
- 只看单段（尤其只看含黄金牛市的那段）不算数
- 若两段都改善且回撤显著下降 → 值得考虑；否则维持现状

用法
----
    $PY tools/test_etf_defensive_sleeve.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                            # noqa: E402
from pipeline import run_backtest                        # noqa: E402
from strategies.builtin import EtfRotationStrategy       # noqa: E402
from strategies.registry import register_strategy        # noqa: E402

POOL = "ETF全球"                    # 实盘在用
DEFENSIVE = ["518880.SH", "510880.SH", "511010.SH"]   # 黄金 / 红利 / 国债
STRATEGY_BASE = "etf_rotation"
TOP_K, REBALANCE = 5, 5
INIT_CASH = 100_000.0
REAL_COST = {"commission_rate": 0.0005, "min_commission": 5.0,
             "sell_tax_rate": 0.001, "slippage_rate": 0.0005}
TD = 244

PERIODS = [("样本内 2019~2022", "20190101", "20221231"),
           ("样本外 2023~2026", "20230101", "20260911"),
           ("全区间 2019~2026", "20190101", "20260911")]


class DefensiveSleeveEtf(EtfRotationStrategy):
    """动量轮动 + 永久防御仓位。

    动量部分权重乘 `(1−sleeve)`，防御篮子固定占 `sleeve`（等权）。
    `sleeve=0` 时退化为原策略。
    """
    name = "etf_defensive"

    def __init__(self, sleeve=0.3, defensive=None, **kw):
        super().__init__(**kw)
        self.sleeve = float(sleeve)
        self.defensive = list(defensive or DEFENSIVE)

    def generate_weights(self, date, factors, panel):
        w = super().generate_weights(date, factors, panel)
        if w is None:
            return None
        if self.sleeve <= 0:
            return w
        out = {c: v * (1.0 - self.sleeve) for c, v in (w or {}).items()}
        per = self.sleeve / len(self.defensive)
        for d in self.defensive:
            out[d] = out.get(d, 0.0) + per
        return out


def use_real_cost():
    config.TRADING_COST.clear()
    config.TRADING_COST.update(REAL_COST)


def metrics(eq: pd.Series):
    r = eq.pct_change().dropna()
    years = len(eq) / TD
    cum = eq.iloc[-1] / eq.iloc[0] - 1
    ann = (1 + cum) ** (1 / years) - 1 if years > 0 else 0.0
    vol = r.std() * np.sqrt(TD)
    sh = r.mean() / r.std() * np.sqrt(TD) if r.std() > 0 else 0.0
    mdd = float((eq / eq.cummax() - 1).min())
    return {"年化": ann * 100, "波动": vol * 100, "夏普": sh,
            "最大回撤": mdd * 100, "卡玛": (ann / abs(mdd)) if mdd else 0.0}


def line(tag, m):
    return ("  {:<22} 年化 {:>7.2f}%  波动 {:>6.2f}%  夏普 {:>5.2f}  "
            "回撤 {:>7.2f}%  卡玛 {:>5.2f}").format(
        tag, m["年化"], m["波动"], m["夏普"], m["最大回撤"], m["卡玛"])


def run(pool_codes, sleeve, s, e):
    r = run_backtest(codes=pool_codes,
                     strategy=STRATEGY_BASE if sleeve <= 0 else "etf_defensive",
                     start=s, end=e, topk=TOP_K, rebalance=REBALANCE,
                     init_cash=INIT_CASH,
                     strategy_params={"risk_parity": True, "sleeve": sleeve,
                                      "defensive": DEFENSIVE})
    eq = r["result"].equity
    return eq["equity"] if "equity" in eq.columns else eq


def main() -> int:
    use_real_cost()
    register_strategy("etf_defensive", DefensiveSleeveEtf)
    codes = list(dict.fromkeys(config.RECOMMENDED_POOLS[POOL] + DEFENSIVE))
    print(f"池子: {POOL} + 防御篮子 {DEFENSIVE}")
    print(f"口径: 真实成本万5 / 资金 {INIT_CASH:,.0f} / topk={TOP_K} / {REBALANCE}日\n")

    sleeves = [0.0, 0.2, 0.3, 0.4, 0.5, 0.8, 1.0]
    res = {}
    print("=" * 104)
    for ptag, s, e in PERIODS:
        print(f"=== {ptag} ===")
        for sl in sleeves:
            try:
                eq = run(codes, sl, s, e)
                m = metrics(eq)
                res[(ptag, sl)] = m
                tag = f"sleeve={sl:.0%}" + ("（现状）" if sl == 0 else "")
                print(line(tag, m))
            except Exception as ex:
                print(f"  sleeve={sl:.0%} 失败: {type(ex).__name__}: {ex}")
        print()

    print("=" * 104)
    print("=== 判据：两段都要改善 ===")
    print("  {:<10}{:>14}{:>14}{:>14}{:>14}".format(
        "sleeve", "IS夏普", "OOS夏普", "IS回撤%", "OOS回撤%"))
    for sl in sleeves:
        a = res.get((PERIODS[0][0], sl), {})
        b = res.get((PERIODS[1][0], sl), {})
        print("  {:<10}{:>14}{:>14}{:>14}{:>14}".format(
            f"{sl:.0%}", f"{a.get('夏普', float('nan')):.2f}",
            f"{b.get('夏普', float('nan')):.2f}",
            f"{a.get('最大回撤', float('nan')):.2f}",
            f"{b.get('最大回撤', float('nan')):.2f}"))

    base_is = res.get((PERIODS[0][0], 0.0), {})
    base_oos = res.get((PERIODS[1][0], 0.0), {})
    print("\n  相对现状（sleeve=0）的改善：")
    for sl in sleeves[1:]:
        a = res.get((PERIODS[0][0], sl), {})
        b = res.get((PERIODS[1][0], sl), {})
        d_sh_is = a.get("夏普", 0) - base_is.get("夏普", 0)
        d_sh_oos = b.get("夏普", 0) - base_oos.get("夏普", 0)
        d_dd_is = a.get("最大回撤", 0) - base_is.get("最大回撤", 0)
        d_dd_oos = b.get("最大回撤", 0) - base_oos.get("最大回撤", 0)
        ok = (d_sh_is > 0 and d_sh_oos > 0)
        print("  sleeve={:<5} IS夏普 {:+.2f} / OOS夏普 {:+.2f} | "
              "IS回撤 {:+.2f}pp / OOS回撤 {:+.2f}pp  {}".format(
                  f"{sl:.0%}", d_sh_is, d_sh_oos, d_dd_is, d_dd_oos,
                  "✅ 两段同向改善" if ok else "✗ 不一致"))

    rows = [{"period": k[0], "sleeve": k[1], **v} for k, v in res.items()]
    pd.DataFrame(rows).to_csv("results/etf_defensive_sleeve.csv",
                              index=False, encoding="utf-8-sig")
    print("\n结果已写出 results/etf_defensive_sleeve.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
