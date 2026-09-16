"""ETF 轮动线：按主人**真实成本**（万5）与**真实资金**（10万）重跑对照。

背景
----
`config.TRADING_COST` 里写的是 **commission_rate=0.0003（万3）**、
`INIT_CASH=200_000`（20 万），而主人实际是**万5 / 10 万**。
回测引擎本身是对的（`backtest/account.py::_fee` 已正确实现
`max(名义佣金, 最低5元)`，且**只对股票收印花税、ETF 免税**），
只是费率参数与实际不符。

本脚本不改任何实盘配置，只在**研究脚本内**临时替换成本参数，回答：
1. 万3 → 万5 对 ETF 轮动线的影响有多大？
2. 20 万 → 10 万 有影响吗？（预期没有：topk=5 时单笔 2 万元，
   佣金 max(20000×0.0005, 5)=10 元=0.05%，不触发最低收费）
3. 现有几个 ETF 池，在真实成本下哪个更好？

用法
----
    $PY tools/test_etf_realcost.py
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                            # noqa: E402
from pipeline import run_backtest                        # noqa: E402

# 实盘跑的是 `main.py simulate --ths` → 池 ETF全球 / 策略 etf_rotation / topk 默认 / 5日
LIVE_POOL = "ETF全球"
LIVE_STRATEGY = "etf_rotation"
TOP_K = 5
REBALANCE = 5

PERIODS = [
    ("全区间 2019~2026", "20190101", "20260911"),
    ("近三年 2024~2026", "20240101", "20260911"),
]

REAL_COST = {"commission_rate": 0.0005, "min_commission": 5.0,
             "sell_tax_rate": 0.001, "slippage_rate": 0.0005}
OLD_COST = {"commission_rate": 0.0003, "min_commission": 5.0,
            "sell_tax_rate": 0.001, "slippage_rate": 0.0005}


def with_cost(cost: dict):
    """临时替换全局成本参数（脚本结束恢复）。"""
    old = dict(config.TRADING_COST)
    config.TRADING_COST.clear()
    config.TRADING_COST.update(cost)
    return old


def show(tag, m):
    return ("  {:<30} 年化 {:>7.2f}%  波动 {:>6.2f}%  夏普 {:>5.2f}  "
            "回撤 {:>7.2f}%  卡玛 {:>5.2f}").format(
        tag, m.get("年化收益", 0), m.get("年化波动", 0), m.get("夏普比率", 0),
        m.get("最大回撤", 0), m.get("卡玛比率", 0))


def run(codes, strategy, params, s, e, cash=None):
    kw = {"strategy_params": params} if params else {}
    r = run_backtest(codes=codes, strategy=strategy, start=s, end=e,
                     topk=TOP_K, rebalance=REBALANCE,
                     init_cash=cash, **kw)
    return r["metrics"], r["result"]


def main() -> int:
    codes = config.RECOMMENDED_POOLS[LIVE_POOL]
    print(f"实盘配置: 池 {LIVE_POOL} ({len(codes)} 只) / 策略 {LIVE_STRATEGY} / "
          f"topk={TOP_K} / {REBALANCE} 日调仓")
    print(f"池子: {','.join(codes)}\n")

    rows = []
    print("=" * 108)
    print("=== 1. 成本口径：配置里的万3 vs 主人实际的万5 ===")
    for tag, s, e in PERIODS:
        print(f"\n  【{tag}】")
        for ctag, cost in [("万3（配置现值）", OLD_COST), ("万5（实际）", REAL_COST)]:
            old = with_cost(cost)
            m, _ = run(codes, LIVE_STRATEGY, None, s, e)
            config.TRADING_COST.clear(); config.TRADING_COST.update(old)
            print(show(f"{ctag}", m))
            rows.append({"period": tag, "cost": ctag, **m})

    print("\n" + "=" * 108)
    print("=== 2. 资金口径：20万（配置现值）vs 10万（实际）—— 预期无差别 ===")
    print("  理由：topk=5 时单笔 2 万元，佣金 max(20000×0.0005, 5)=10 元 = 0.05%，不触发最低收费")
    for tag, s, e in PERIODS:
        print(f"\n  【{tag}】")
        for ctag, cash in [("20万（配置现值）", 200_000.0), ("10万（实际）", 100_000.0)]:
            old = with_cost(REAL_COST)
            m, _ = run(codes, LIVE_STRATEGY, None, s, e, cash=cash)
            config.TRADING_COST.clear(); config.TRADING_COST.update(old)
            print(show(ctag, m))

    print("\n" + "=" * 108)
    print("=== 3. ETF 池对比（真实成本万5 / 10万 / 同策略同参数）===")
    pools = ["ETF全球", "ETF稳健", "ETF宽基", "ETF行业", "ETF防御", "科技进攻"]
    print("  {:<14}{:>9}{:>26}{:>26}".format("池子", "标的数", "全区间 2019~2026", "近三年 2024~2026"))
    for p in pools:
        cs = config.RECOMMENDED_POOLS.get(p)
        if not cs:
            continue
        out = []
        for s, e in [("20190101", "20260911"), ("20240101", "20260911")]:
            old = with_cost(REAL_COST)
            try:
                m, _ = run(cs, LIVE_STRATEGY, None, s, e, cash=100_000.0)
                out.append(f"年化{m.get('年化收益',0):>6.2f}% 夏普{m.get('夏普比率',0):>5.2f}")
            except Exception as ex:
                out.append(f"失败: {type(ex).__name__}")
            config.TRADING_COST.clear(); config.TRADING_COST.update(old)
        print("  {:<14}{:>9}{:>26}{:>26}".format(p, len(cs), out[0], out[1]))

    pd.DataFrame(rows).to_csv("results/etf_realcost.csv", index=False,
                              encoding="utf-8-sig")
    print("\n成本对照已写出 results/etf_realcost.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
