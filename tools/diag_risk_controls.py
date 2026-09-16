"""诊断：为什么「回撤熔断」与「波动率目标」在回测里从未触发？

背景
----
`tools/attribute_etf_return.py` 实测：关掉 `dd_circuit` 后结果**差异恰好为 0**；
关掉 `vol_target` 差异仅 0.04pp。而 `config.py` 把它们标为「默认甜点组合」。

`risk/portfolio.py` 的阈值：
- 回撤熔断：`_CB_TRIGGER = 0.15`（回撤 ≥15% 才启动）
- 波动率目标：`vol_target = 0.15`（年化波动 >15% 才降仓）

本脚本直接测组合净值的**实际回撤**与**实际滚动波动**，验证阈值是否够得着。

用法
----
    $PY tools/diag_risk_controls.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                            # noqa: E402
from pipeline import run_backtest                        # noqa: E402
from risk.portfolio import _CB_LEVELS, _CB_TRIGGER, _CB_RECOVER  # noqa: E402

POOL = "ETF全球"
TOP_K, REBALANCE = 5, 5
INIT_CASH = 100_000.0
REAL_COST = {"commission_rate": 0.0005, "min_commission": 5.0,
             "sell_tax_rate": 0.001, "slippage_rate": 0.0005}
TD = 244

CASES = [
    ("现状（Faber 开 / 趋势门 开）", {"trend_window": 60, "trend_gate": True}, "20150101"),
    ("关 Faber", {"trend_window": None, "trend_gate": True}, "20150101"),
    ("纯动量 H（关趋势+门+RP）", {"trend_window": None, "trend_gate": False,
                                  "risk_parity": False}, "20150101"),
]


def main() -> int:
    config.TRADING_COST.clear()
    config.TRADING_COST.update(REAL_COST)
    print(f"阈值（risk/portfolio.py）：")
    print(f"  回撤熔断  触发 {_CB_TRIGGER:.0%}  恢复 {_CB_RECOVER:.0%}  "
          f"分档 {[(f'{t:.0%}', l) for t, l in _CB_LEVELS]}")
    print(f"  波动率目标  {config.DEFAULT_VOL_TARGET:.0%}")
    print(f"  池 {POOL}  topk={TOP_K}  {REBALANCE}日调仓  区间 2015~2026\n")

    print("=" * 104)
    print("  {:<30}{:>12}{:>14}{:>14}{:>16}".format(
        "配置", "最大回撤", "最大滚动波动", "熔断是否触发", "波动目标是否触发"))
    for tag, sp, start in CASES:
        ep = {"dd_circuit": False, "vol_target": 0}      # 先关掉风控，测"裸"净值
        r = run_backtest(codes=config.RECOMMENDED_POOLS[POOL],
                         strategy="etf_rotation", start=start, end="20260911",
                         topk=TOP_K, rebalance=REBALANCE, init_cash=INIT_CASH,
                         strategy_params={"risk_parity": True, **sp}, **ep)
        eq = r["result"].equity
        eq = eq["equity"] if "equity" in eq.columns else eq
        nav = eq.values
        # 实际最大回撤
        peak = np.maximum.accumulate(nav)
        dd = 1 - nav / peak
        max_dd = float(dd.max())
        # 实际滚动 20 日年化波动
        rets = pd.Series(nav).pct_change().dropna()
        roll_vol = rets.rolling(20).std() * np.sqrt(TD)
        max_vol = float(roll_vol.max())
        cb_hit = max_dd >= _CB_TRIGGER
        vt_hit = max_vol > config.DEFAULT_VOL_TARGET
        print("  {:<30}{:>12.2%}{:>14.2%}{:>14}{:>16}".format(
            tag, max_dd, max_vol,
            "是" if cb_hit else "**否**", "是" if vt_hit else "**否**"))
        # 补充：波动率超过目标的天数占比
        pct_above = float((roll_vol.dropna() > config.DEFAULT_VOL_TARGET).mean())
        print("  {:<30}  → 滚动波动超 15% 的天数占比 {:.1%}；"
              "回撤超 15% 的天数占比 {:.1%}".format(
                  "", pct_above, float((dd >= _CB_TRIGGER).mean())))
        print()

    print("=" * 104)
    print("=== 结论 ===")
    print("  若各配置的「最大回撤 < 15%」且「最大滚动波动 < 15%」，则两个风控**永远不会触发**——")
    print("  它们在 config.py 里被标为「默认甜点组合」，但对该策略实际不起任何作用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
