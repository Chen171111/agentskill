"""对照回测：ETF 轮动基线 vs 叠加精灵大单信号（A 路线效果评估）。

精灵数据区间 2022-10-10 ~ 2024-07-31，故回测区间取该窗口。
池子用 `ETF稳健`（10 只 ETF，全部有代理篮子）。

用法
----
    cd E:/MyWorkAndProject/量化/agentskill
    python tools/compare_bigorder.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                    # noqa: E402
from pipeline import run_backtest                # noqa: E402

POOL = "ETF稳健"
START = "20221010"
END = "20240731"
TOPK = 3
REBALANCE = 5


def show(tag, r):
    m = r["metrics"]
    keys = ["年化收益", "累计收益", "最大回撤", "夏普比率", "卡玛比率",
            "年化波动", "胜率", "超额收益"]
    parts = ["{}={}".format(k, round(m[k], 2)) for k in keys if k in m]
    print("  {:<30} {}".format(tag, "  ".join(parts)))
    return m


def run(tag, strategy, params=None):
    try:
        r = run_backtest(
            codes=config.RECOMMENDED_POOLS[POOL],
            strategy=strategy, start=START, end=END,
            topk=TOPK, rebalance=REBALANCE,
            strategy_params=params or {},
        )
    except Exception as e:
        import traceback
        print("  {:<30} 失败: {}: {}".format(tag, type(e).__name__, e))
        traceback.print_exc()
        return None
    return show(tag, r)


def main():
    print("池子 {}: {}".format(POOL, ",".join(config.RECOMMENDED_POOLS[POOL])))
    print("区间 {} ~ {}  topk={}  rebalance={}\n".format(START, END, TOPK, REBALANCE))

    print("=== 1. 基线 ===")
    base = run("etf_rotation(基线)", "etf_rotation", {"risk_parity": True})

    print("\n=== 2. 大单叠加（逐项开关）===")
    variants = [
        ("+big_amt 倾斜", {"use_amt_tilt": True, "use_ratio_gate": False,
                          "use_breadth_gate": False}),
        ("+big_amt 倾斜(权重2)", {"use_amt_tilt": True, "amt_weight": 2.0}),
        ("+big_ratio 排雷门(0)", {"use_amt_tilt": False, "use_ratio_gate": True,
                                "ratio_thr": 0.0}),
        ("+big_ratio 排雷门(-0.3)", {"use_amt_tilt": False, "use_ratio_gate": True,
                                  "ratio_thr": -0.3}),
        ("+mkt_breadth 降仓门(0.8/0.5)", {"use_amt_tilt": False,
                                        "use_breadth_gate": True,
                                        "breadth_q": 0.80, "breadth_scale": 0.5}),
        ("+mkt_breadth 降仓门(0.7/0.3)", {"use_amt_tilt": False,
                                        "use_breadth_gate": True,
                                        "breadth_q": 0.70, "breadth_scale": 0.3}),
        ("三者全开", {"use_amt_tilt": True, "use_ratio_gate": True,
                    "use_breadth_gate": True}),
    ]
    results = {"etf_rotation(基线)": base}
    for tag, p in variants:
        results[tag] = run(tag, "bigorder_etf", p)

    print("\n=== 汇总（相对基线）===")
    if base:
        for tag, m in results.items():
            if m is None or tag == "etf_rotation(基线)":
                continue
            d_ret = m.get("年化收益", 0) - base.get("年化收益", 0)
            d_dd = m.get("最大回撤", 0) - base.get("最大回撤", 0)
            d_sh = m.get("夏普比率", 0) - base.get("夏普比率", 0)
            print("  {:<30} 年化差 {:+.2f}pp   回撤差 {:+.2f}pp   夏普差 {:+.2f}"
                  .format(tag, d_ret, d_dd, d_sh))


if __name__ == "__main__":
    main()
