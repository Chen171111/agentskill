"""复现根 HANDOFF 的 Faber 趋势过滤验证，解释与本次归因的矛盾。

矛盾
----
根 `HANDOFF.md` 记录（2026-09）：
    「Faber 均线趋势过滤默认开启（trend_window=60）。复测 2020-01~2026-08：
      年化 33.01%→**51.19%**、最大回撤 −17.05%→−11.98%」
即当时 **Faber 过滤是显著正贡献**。

而 `tools/attribute_etf_return.py` 实测（池 ETF全球 / 2019~2026）：
    关掉 Faber 过滤 年化 **+2.19pp**、夏普 +0.29 → **是拖累**。

**51.19% 年化远高于实盘池的 7.67%**，说明当时用的**不是同一个池子/配置**。
本脚本系统性扫描「池子 × topk」，找出哪套配置能复现 33.01% → 51.19%。

用法
----
    $PY tools/reproduce_faber_validation.py
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                            # noqa: E402
from pipeline import run_backtest                        # noqa: E402

PERIOD = ("20200101", "20260831")      # HANDOFF 记录的区间
REBALANCE = 5
INIT_CASH = 100_000.0
REAL_COST = {"commission_rate": 0.0005, "min_commission": 5.0,
             "sell_tax_rate": 0.001, "slippage_rate": 0.0005}
TD = 244

# HANDOFF 的目标值
TARGET_WITH, TARGET_WITHOUT = 51.19, 33.01
TARGET_DD_WITH, TARGET_DD_WITHOUT = -11.98, -17.05


def use_real_cost():
    config.TRADING_COST.clear()
    config.TRADING_COST.update(REAL_COST)


def run(codes, topk, trend_window, s, e):
    r = run_backtest(codes=codes, strategy="etf_rotation", start=s, end=e,
                     topk=topk, rebalance=REBALANCE, init_cash=INIT_CASH,
                     strategy_params={"risk_parity": True,
                                      "trend_window": trend_window})
    eq = r["result"].equity
    eq = eq["equity"] if "equity" in eq.columns else eq
    ret = eq.pct_change().dropna()
    years = len(eq) / TD
    cum = eq.iloc[-1] / eq.iloc[0] - 1
    ann = (1 + cum) ** (1 / years) - 1 if years > 0 else 0.0
    mdd = float((eq / eq.cummax() - 1).min())
    return ann * 100, mdd * 100, len(eq), eq.index.min(), eq.index.max()


def main() -> int:
    use_real_cost()
    print(f"复现目标（HANDOFF 记录，区间 {PERIOD[0]}~{PERIOD[1]}）：")
    print(f"  开 Faber: 年化 {TARGET_WITH:.2f}%  回撤 {TARGET_DD_WITH:.2f}%")
    print(f"  关 Faber: 年化 {TARGET_WITHOUT:.2f}%  回撤 {TARGET_DD_WITHOUT:.2f}%\n")

    pools = [k for k in config.RECOMMENDED_POOLS if not k.startswith("宽基成长")
             and not k.startswith("行业轮动")]      # 排除指数池（不可交易）
    rows = []
    print("=" * 118)
    print("  {:<12}{:>6}{:>28}{:>28}{:>14}".format(
        "池子", "topk", "开 Faber(tw=60)", "关 Faber(tw=None)", "判定"))
    for pool in pools:
        codes = config.RECOMMENDED_POOLS[pool]
        for topk in (3, 5, 10):
            try:
                a = run(codes, topk, 60, *PERIOD)
                b = run(codes, topk, None, *PERIOD)
            except Exception as ex:
                print(f"  {pool:<12}{topk:>6}  失败: {type(ex).__name__}: {ex}")
                continue
            # 数据是否覆盖整个区间？
            covered = (a[3] <= PERIOD[0] and a[4] >= "20260801")
            mark = ""
            # 与目标值比对（宽松：年化差 < 5pp）
            if abs(a[0] - TARGET_WITH) < 5 and abs(b[0] - TARGET_WITHOUT) < 5:
                mark = "★ 接近目标"
            elif not covered:
                mark = "数据未覆盖全区间"
            print("  {:<12}{:>6}{:>28}{:>28}{:>14}".format(
                pool, topk,
                f"{a[0]:>7.2f}% / DD {a[1]:>7.2f}%",
                f"{b[0]:>7.2f}% / DD {b[1]:>7.2f}%", mark))
            rows.append({"pool": pool, "topk": topk,
                         "ann_with": a[0], "dd_with": a[1],
                         "ann_without": b[0], "dd_without": b[1],
                         "start": a[3], "end": a[4], "covered": covered,
                         "faber_gain_pp": a[0] - b[0],
                         "mark": mark})

    df = pd.DataFrame(rows)
    df.to_csv("results/faber_reproduce.csv", index=False, encoding="utf-8-sig")

    print("\n" + "=" * 118)
    print("=== 汇总：Faber 过滤在各池子上的净贡献（年化 pp）===")
    if len(df):
        g = df.groupby("pool").faber_gain_pp.agg(["mean", "min", "max"]).round(2)
        print(g.to_string())
        n_pos = int((df.faber_gain_pp > 0).sum())
        print(f"\n  全部 {len(df)} 个组合中，Faber 为正贡献的有 {n_pos} 个 "
              f"({n_pos/len(df):.0%})")
        print("  读法：正数 = Faber 有用（与 HANDOFF 一致）；"
              "负数 = Faber 是拖累（与本次归因一致）")

    print("\n结果已写出 results/faber_reproduce.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
