"""ETF 轮动线：收益归因 —— 查清「跑输等权买入持有」的 5.15pp 漏在哪一步。

背景
----
`docs/ETF线_真实成本复核.md` 第六节发现：`etf_rotation` 全区间年化 2.49% / 夏普 0.35，
而**等权买入持有整个 ETF全球 池**是 7.64% / 夏普 0.60。策略压了回撤（−33.9%→−13.8%）
但每年少赚 5.15pp，风险调整后仍是输的。

本脚本把策略组件**逐个关掉**，定位损失来源：
- Faber 趋势过滤（`trend_window=60`）→ 是否让它长期空仓错过上涨？
- 趋势门（`trend_gate`）、风险平价（`risk_parity`）
- 回撤熔断（`dd_circuit`）、波动率目标（`vol_target`）
- 集中度（`topk`）

同时输出**空仓天数占比**与**平均持仓数**，验证机制假设。

用法
----
    $PY tools/attribute_etf_return.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                            # noqa: E402
from pipeline import run_backtest                        # noqa: E402

POOL = "ETF全球"
TOP_K, REBALANCE = 5, 5
INIT_CASH = 100_000.0
REAL_COST = {"commission_rate": 0.0005, "min_commission": 5.0,
             "sell_tax_rate": 0.001, "slippage_rate": 0.0005}
TD = 244
PERIODS = [("IS 19~22", "20190101", "20221231"),
           ("OOS 23~26", "20230101", "20260911"),
           ("全区间", "20190101", "20260911")]

# (标签, 策略参数, 引擎参数)
VARIANTS = [
    ("A 基线（现状全开）", {}, {}),
    ("B 关 Faber 趋势过滤", {"trend_window": None}, {}),
    ("C 关趋势门", {"trend_gate": False}, {}),
    ("D 关风险平价", {"risk_parity": False}, {}),
    ("E 关回撤熔断", {}, {"dd_circuit": False}),
    ("F 关波动率目标", {}, {"vol_target": 0}),
    ("G topk=20", {"topk": 20}, {}),
    ("H 纯动量（关趋势+门+RP+风控）",
     {"trend_window": None, "trend_gate": False, "risk_parity": False},
     {"dd_circuit": False, "vol_target": 0}),
    ("I 关趋势过滤 + topk=20", {"trend_window": None, "trend_gate": False,
                                "topk": 20}, {}),
]


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


def run(codes, sparams, eparams, s, e):
    r = run_backtest(codes=codes, strategy="etf_rotation", start=s, end=e,
                     topk=sparams.get("topk", TOP_K), rebalance=REBALANCE,
                     init_cash=INIT_CASH,
                     strategy_params={k: v for k, v in sparams.items() if k != "topk"},
                     **eparams)
    eq = r["result"].equity
    eq = eq["equity"] if "equity" in eq.columns else eq
    # 持仓诊断
    hold = r["result"].holdings
    if hold:
        n = pd.Series({d: len(v) for d, v in hold.items()})
        zero = float((n == 0).mean())
        avg = float(n.mean())
    else:
        zero, avg = float("nan"), float("nan")
    return eq, zero, avg


def benchmark(s, e):
    """等权买入持有整个池子。

    ⚠️ **必须做份额折算复权** —— 本函数直接读 CSV（不走 `DataStore`），
    而 `data/stocks/*.csv` 是**未复权**的。不复权的话基准里会混进假跳变
    （如 510500 在 2015-04-15 的 +248.6%），基准被抬高，归因结论就错了。
    策略侧走 `pipeline.run_backtest` → `DataStore.read()` 已自动复权，
    **两侧口径必须一致**，否则「策略 vs 基准」的差里会混进数据口径差。
    """
    from dataprovider.adjust import repair_frame
    fr = {}
    for c in config.RECOMMENDED_POOLS[POOL]:
        d = pd.read_csv(f"data/stocks/{c}.csv", dtype={"date": str},
                        usecols=["date", "close"])
        d = d.set_index("date").sort_index()
        d, n_fix, _ = repair_frame(d, c)
        if n_fix:
            print(f"  [benchmark] {c} 份额折算自动复权 {n_fix} 处")
        fr[c] = d["close"]
    px = pd.DataFrame(fr).sort_index()
    q = px[(px.index >= s) & (px.index <= e)].ffill().dropna()
    r = q.pct_change().mean(axis=1).dropna()
    return (1 + r).cumprod()


def main() -> int:
    use_real_cost()
    codes = config.RECOMMENDED_POOLS[POOL]
    print(f"池子 {POOL}（{len(codes)} 只）  topk={TOP_K}  {REBALANCE}日调仓  "
          f"真实成本万5 / 资金 {INIT_CASH:,.0f}")
    print("空仓占比 = 当日持仓为 0 的交易日比例（诊断趋势过滤是否长期空仓）\n")

    rows = []
    for ptag, s, e in PERIODS:
        print("=" * 112)
        print(f"=== {ptag} ===")
        b = metrics(benchmark(s, e))
        print("  {:<32}{:>9}{:>9}{:>8}{:>10}{:>8}".format(
            "等权买入持有（懒人基准）", f"{b['年化']:.2f}", f"{b['波动']:.2f}",
            f"{b['夏普']:.2f}", f"{b['最大回撤']:.2f}", f"{b['卡玛']:.2f}"))
        print("  " + "-" * 104)
        print("  {:<32}{:>9}{:>9}{:>8}{:>10}{:>8}{:>10}{:>10}".format(
            "变体", "年化%", "波动%", "夏普", "回撤%", "卡玛", "空仓占比", "平均持仓"))
        for tag, sp, ep in VARIANTS:
            try:
                eq, zero, avg = run(codes, sp, ep, s, e)
                m = metrics(eq)
                print("  {:<32}{:>9.2f}{:>9.2f}{:>8.2f}{:>10.2f}{:>8.2f}"
                      "{:>10.1%}{:>10.1f}".format(
                          tag, m["年化"], m["波动"], m["夏普"], m["最大回撤"],
                          m["卡玛"], zero, avg))
                rows.append({"period": ptag, "variant": tag, **m,
                             "cash_ratio": zero, "avg_names": avg,
                             "bench_年化": b["年化"], "bench_夏普": b["夏普"]})
            except Exception as ex:
                print(f"  {tag:<32} 失败: {type(ex).__name__}: {ex}")
        print()

    df = pd.DataFrame(rows)
    df.to_csv("results/etf_attribution.csv", index=False, encoding="utf-8-sig")

    full = df[df.period == "全区间"]
    if len(full):
        base = full[full.variant.str.startswith("A ")]
        print("=" * 112)
        print("=== 归因结论（全区间，相对基线 A）===")
        b0 = base.iloc[0] if len(base) else None
        for _, r in full.iterrows():
            if b0 is None or r.variant.startswith("A "):
                continue
            print("  {:<32} 年化 {:+.2f}pp  夏普 {:+.2f}  回撤 {:+.2f}pp".format(
                r.variant, r["年化"] - b0["年化"], r["夏普"] - b0["夏普"],
                r["最大回撤"] - b0["最大回撤"]))
        print("\n  懒人基准（等权买入持有）全区间: 年化 {:.2f}%  夏普 {:.2f}".format(
            b0["bench_年化"], b0["bench_夏普"]))
        best = full.loc[full["年化"].idxmax()]
        print("  全区间年化最高的变体: {}  {:.2f}%".format(best.variant, best["年化"]))
    print("\n结果已写出 results/etf_attribution.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
