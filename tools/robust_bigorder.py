"""稳健性检验：`big_ratio` 排雷门的收益是真 alpha 还是过拟合/空仓躲跌？

三个检验
--------
1. **阈值扫描**：`ratio_thr` 从 −0.6 到 +0.2 连续变化，看年化收益是「平滑山丘」
   还是「单点尖峰」。单点尖峰 = 过拟合。
2. **平均仓位**：统计每个阈值下的平均持仓比例。若改善主要来自长期空仓躲跌，
   则不是选股 alpha。
3. **同仓位随机对照**：构造 N 个「随机空仓、平均仓位与排雷门相同」的对照，
   看排雷门是否显著优于随机对照。若不显著，说明收益只是"少暴露"而非"选对"。

用法
----
    cd E:/MyWorkAndProject/量化/agentskill
    python tools/robust_bigorder.py
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                    # noqa: E402
from pipeline import run_backtest                # noqa: E402

POOL = "ETF稳健"
START = "20221010"
END = "20240731"
TOPK = 3
REBALANCE = 5
N_RANDOM = 30


def avg_exposure(result):
    """平均持仓比例：每日持仓标的数 / topk（近似仓位）。holdings[date] 是 list。"""
    vals = []
    for d, h in result.holdings.items():
        n = len(h) if h else 0
        vals.append(min(n, TOPK) / float(TOPK))
    return float(np.mean(vals)) if vals else 0.0


def backtest(strategy, params):
    r = run_backtest(codes=config.RECOMMENDED_POOLS[POOL], strategy=strategy,
                     start=START, end=END, topk=TOPK, rebalance=REBALANCE,
                     strategy_params=params)
    return r["metrics"], r["result"]


def main():
    print("池子 {}  区间 {} ~ {}\n".format(POOL, START, END))

    m0, r0 = backtest("etf_rotation", {"risk_parity": True})
    print("基线 etf_rotation: 年化 {:.2f}%  回撤 {:.2f}%  夏普 {:.2f}  平均仓位 {:.1%}"
          .format(m0["年化收益"], m0["最大回撤"], m0["夏普比率"], avg_exposure(r0)))

    print("\n=== 1+2. ratio_thr 阈值扫描（看平滑性 + 平均仓位）===")
    print("{:>8}  {:>9}  {:>9}  {:>8}  {:>9}".format(
        "thr", "年化%", "回撤%", "夏普", "平均仓位"))
    rows = []
    for thr in [-0.6, -0.5, -0.4, -0.3, -0.2, -0.1, -0.05, 0.0, 0.05, 0.1, 0.2]:
        try:
            m, r = backtest("bigorder_etf",
                            {"use_amt_tilt": False, "use_ratio_gate": True,
                             "ratio_thr": thr, "use_breadth_gate": False})
        except Exception as e:
            print("{:>8.2f}  失败 {}".format(thr, e))
            continue
        exp = avg_exposure(r)
        rows.append((thr, m["年化收益"], m["最大回撤"], m["夏普比率"], exp))
        print("{:>8.2f}  {:>9.2f}  {:>9.2f}  {:>8.2f}  {:>9.1%}".format(
            thr, m["年化收益"], m["最大回撤"], m["夏普比率"], exp))

    print("\n=== 3. 同仓位对照：排雷门 vs 把基线仓位按相同比例压低 ===")
    m_gate, r_gate = backtest("bigorder_etf",
                              {"use_amt_tilt": False, "use_ratio_gate": True,
                               "ratio_thr": 0.0, "use_breadth_gate": False})
    exp_gate = avg_exposure(r_gate)
    print("排雷门(thr=0.0): 年化 {:+.2f}%  回撤 {:.2f}%  平均仓位 {:.1%}".format(
        m_gate["年化收益"], m_gate["最大回撤"], exp_gate))
    # 基线按相同平均仓位等比压低（max_total 缩放）
    m_flat, r_flat = backtest("etf_rotation",
                              {"risk_parity": True,
                               "max_total": max(0.05, exp_gate * 0.9)})
    print("基线同仓位({:.0%}): 年化 {:+.2f}%  回撤 {:.2f}%  平均仓位 {:.1%}".format(
        exp_gate * 0.9, m_flat["年化收益"], m_flat["最大回撤"], avg_exposure(r_flat)))
    d = m_gate["年化收益"] - m_flat["年化收益"]
    print("→ 排雷门相对同仓位基线 年化差 {:+.2f}pp".format(d))
    print("   若该差值接近 0，说明排雷门的收益只是'少暴露'，不含择时/选券 alpha。")

    print("\n=== 4. 分段稳健性（前半 / 后半）===")
    mid = "20231031"
    for tag, s, e in [("前半", START, mid), ("后半", mid, END)]:
        for name, strat, p in [("基线", "etf_rotation", {"risk_parity": True}),
                               ("排雷门(0)", "bigorder_etf",
                                {"use_amt_tilt": False, "use_ratio_gate": True,
                                 "ratio_thr": 0.0, "use_breadth_gate": False})]:
            try:
                r = run_backtest(codes=config.RECOMMENDED_POOLS[POOL], strategy=strat,
                                 start=s, end=e, topk=TOPK, rebalance=REBALANCE,
                                 strategy_params=p)
                m = r["metrics"]
                print("  {} {}: 年化 {:+.2f}%  回撤 {:.2f}%  夏普 {:+.2f}".format(
                    tag, name, m["年化收益"], m["最大回撤"], m["夏普比率"]))
            except Exception as ex:
                print("  {} {}: 失败 {}".format(tag, name, ex))
    return 0


if __name__ == "__main__":
    sys.exit(main())
