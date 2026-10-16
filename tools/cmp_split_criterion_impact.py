"""A/B：份额折算复权判据口径对权威回测的影响（thresh 0.35 vs limit 按品种涨跌停）。

背景（2026-10-16）：0.35 扁平判据漏掉两处真实折算（512690 2021-12-31 −27.06%、
510500 2022-08-29 −12.74%，后者在**权威 ETF全球池**内），权威回测窗口里留着假跌。
`limit` 口径按品种涨跌停扫描可修复它们（实测零误报，见
tools/diag_split_limit_criterion.py）。本脚本用同命令同数据跑两遍，量化差额。

切换开关 = DataStore(split_criterion="limit")；默认仍为 "thresh"（0.35），
按项目纪律（复权假设变更需老大决策）未擅自改默认。本脚本只做影响量化。

运行：E:/Python32/python.exe tools/cmp_split_criterion_impact.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import pipeline
from dataprovider.store import DataStore

POOL = config.RECOMMENDED_POOLS["ETF全球"]
KW = dict(strategy="etf_rotation", topk=5, rebalance=5,
          start="20190101", end="20260911")


def run_with(criterion: str) -> dict:
    orig = pipeline.DataStore
    pipeline.DataStore = (lambda **kw: DataStore(split_criterion=criterion, **kw))
    try:
        out = pipeline.run_backtest(POOL, **KW)
    finally:
        pipeline.DataStore = orig
    return out["metrics"]


def main():
    print("=" * 78)
    print("复权判据 A/B（etf_rotation / ETF全球 / topk=5 / 5日调仓 / 20190101~20260911）")
    print("=" * 78)
    rows = {}
    for crit in ("thresh", "limit"):
        print("\n[{}] 跑中…".format(crit), flush=True)
        rows[crit] = run_with(crit)
    keys = ["累计收益", "年化收益", "最大回撤", "夏普比率", "年化波动", "胜率",
            "基准累计收益", "基准年化收益"]
    print("\n{:<14}{:>12}{:>12}{:>12}".format("指标", "thresh(现行)", "limit(按品种)", "差(limit-thresh)"))
    for k in keys:
        a = rows["thresh"].get(k)
        b = rows["limit"].get(k)
        if a is None or b is None:
            continue
        print("{:<14}{:>12.2f}{:>12.2f}{:>+12.2f}".format(k, a, b, b - a))
    print("\n解读：差额 = 修复 510500(2022-08-29, 权威池内) + 512690(2021-12-31, 不在本池)"
          " 两处漏检折算的影响。512690 不在 ETF全球池 → 本池差额全部来自 510500。")


if __name__ == "__main__":
    main()
