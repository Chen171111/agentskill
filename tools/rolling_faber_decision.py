"""Faber 趋势过滤：**滚动样本外**判定（回答「该不该留」）。

为什么必须滚动
--------------
`docs/ETF线_收益归因.md` 第四节已证明：Faber 的贡献**随区间剧烈波动**——
含 2019（强牛）时 −2.19pp，不含时 +2.13pp，差异 4.3pp/年全部来自一个年份。
**单次回测无法判定它的去留。**

方法（决策规则事先定死，不调参）
--------------------------------
1. 跑两条全区间净值：`Faber 开`（trend_window=60）与 `Faber 关`（None）
2. 滚动推进：在时点 t，用 **[t−W, t]** 的**夏普比率**判断哪个更好，选中的那个
   在 **[t, t+step]** 持有
3. 用**段内日收益**复利拼接（⚠️ 不能把各段净值归一化后直接 concat ——
   不同净值水平会在边界产生人为跳变）

对照：始终开 / 始终关 / 懒人基准（等权买入持有整池）

判据
----
- 滚动决策若**明显优于**「始终开」与「始终关」两者 → 说明"择时切换 Faber"有价值
- 若**介于两者之间**或与某一固定选择持平 → 说明切换不产生价值，
  应选**结构上更稳的那个**（本例：保留 Faber，因其在 71% 配置上为正贡献）

用法
----
    $PY tools/rolling_faber_decision.py
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
START, END = "20190101", "20260911"
WINDOW_Y = 2.0          # 回看窗口（年）
STEP_Y = 0.5            # 持有步长（年）—— 半年一次决策，样本更多


def use_real_cost():
    config.TRADING_COST.clear()
    config.TRADING_COST.update(REAL_COST)


def curve(trend_window):
    r = run_backtest(codes=config.RECOMMENDED_POOLS[POOL], strategy="etf_rotation",
                     start=START, end=END, topk=TOP_K, rebalance=REBALANCE,
                     init_cash=INIT_CASH,
                     strategy_params={"risk_parity": True,
                                      "trend_window": trend_window})
    eq = r["result"].equity
    return eq["equity"] if "equity" in eq.columns else eq


def benchmark():
    fr = {}
    for c in config.RECOMMENDED_POOLS[POOL]:
        d = pd.read_csv(f"data/stocks/{c}.csv", dtype={"date": str},
                        usecols=["date", "close"])
        fr[c] = d.set_index("date")["close"]
    px = pd.DataFrame(fr).sort_index()
    q = px[(px.index >= START) & (px.index <= END)].ffill().dropna()
    return (1 + q.pct_change().mean(axis=1).dropna()).cumprod()


def stats(eq, tag):
    r = eq.pct_change().dropna()
    years = len(eq) / TD
    cum = eq.iloc[-1] / eq.iloc[0] - 1
    ann = (1 + cum) ** (1 / years) - 1 if years > 0 else 0.0
    vol = r.std() * np.sqrt(TD)
    sh = r.mean() / r.std() * np.sqrt(TD) if r.std() > 0 else 0.0
    mdd = float((eq / eq.cummax() - 1).min())
    print("  {:<30} 年化 {:>7.2f}%  波动 {:>6.2f}%  夏普 {:>5.2f}  "
          "回撤 {:>7.2f}%  卡玛 {:>5.2f}".format(
              tag, ann * 100, vol * 100, sh, mdd * 100,
              (ann / abs(mdd)) if mdd else 0.0))
    return {"配置": tag, "年化": ann * 100, "波动": vol * 100, "夏普": sh,
            "最大回撤": mdd * 100, "卡玛": (ann / abs(mdd)) if mdd else 0.0}


def main() -> int:
    use_real_cost()
    print(f"池 {POOL}  topk={TOP_K}  {REBALANCE}日调仓  真实成本万5 / 10万")
    print(f"滚动: 回看 {WINDOW_Y} 年（按夏普判定），持有 {STEP_Y} 年\n")

    on = curve(60)
    off = curve(None)
    bm = benchmark()
    idx = on.index.intersection(off.index).intersection(bm.index)
    on, off, bm = on.reindex(idx), off.reindex(idx), bm.reindex(idx)
    on, off, bm = on / on.iloc[0], off / off.iloc[0], bm / bm.iloc[0]
    print(f"对齐后 {len(idx)} 个交易日 ({idx.min()} ~ {idx.max()})\n")

    w = int(WINDOW_Y * TD)
    st = int(STEP_Y * TD)
    rets, picks, prev = [], [], None
    i = w
    while i < len(idx):
        j = min(i + st, len(idx) - 1)
        if j <= i:
            break
        sc = {}
        for tag, s in (("开", on), ("关", off)):
            r = s.iloc[i - w:i].pct_change().dropna()
            sc[tag] = (r.mean() / r.std() * np.sqrt(TD)) if r.std() > 0 else -9
        pick = max(sc, key=sc.get)
        src = on if pick == "开" else off
        rets.append(src.iloc[i:j + 1].pct_change().dropna())
        picks.append({"start": idx[i], "end": idx[j], "pick": pick,
                      "sharpe_on": round(sc["开"], 3),
                      "sharpe_off": round(sc["关"], 3)})
        if pick != prev:
            print(f"  {idx[i]} ~ {idx[j]}: 选 **{pick} Faber**"
                  f"（过去{WINDOW_Y:.0f}年夏普 开{sc['开']:.2f} / 关{sc['关']:.2f}）",
                  flush=True)
        prev = pick
        i = j

    allr = pd.concat(rets)
    allr = allr[~allr.index.duplicated(keep="first")].sort_index()
    roll = (1 + allr).cumprod()
    roll = pd.concat([pd.Series([1.0], index=[idx[0]]), roll])

    print("\n" + "=" * 104)
    print("=== 结果 ===")
    rows = [stats(bm, "懒人基准（等权买入持有整池）"),
            stats(on, "始终开 Faber"),
            stats(off, "始终关 Faber"),
            stats(roll, "滚动决策（样本外）")]

    n_on = sum(1 for p in picks if p["pick"] == "开")
    print(f"\n  滚动共 {len(picks)} 次决策，其中选「开」{n_on} 次 / "
          f"选「关」{len(picks)-n_on} 次")
    print(f"  决策区间: {picks[0]['start']} ~ {picks[-1]['end']}")

    m_roll = rows[3]
    m_on, m_off = rows[1], rows[2]
    print("\n=== 判据 ===")
    print("  滚动决策夏普 {:.2f}  vs  始终开 {:.2f}  /  始终关 {:.2f}".format(
        m_roll["夏普"], m_on["夏普"], m_off["夏普"]))
    better = max(m_on["夏普"], m_off["夏普"])
    if m_roll["夏普"] > better + 0.05:
        print("  → ✅ 滚动切换**优于**两个固定选择 → 「择时切换 Faber」有价值")
    elif m_roll["夏普"] >= better - 0.05:
        print("  → ⚪ 滚动与最佳固定选择**持平** → 切换不产生价值，"
              "应选结构上更稳的那个")
    else:
        print("  → ❌ 滚动**劣于**最佳固定选择 → 切换是负贡献")

    pd.DataFrame(rows).to_csv("results/rolling_faber.csv", index=False,
                              encoding="utf-8-sig")
    pd.DataFrame(picks).to_csv("results/rolling_faber_picks.csv", index=False,
                               encoding="utf-8-sig")
    print("\n结果已写出 results/rolling_faber.csv 与 rolling_faber_picks.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
