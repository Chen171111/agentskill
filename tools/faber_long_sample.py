"""Faber 趋势过滤：**长样本**判定（2015~2026，12 年）。

为什么需要它
------------
`docs/ETF线_收益归因.md` 第四节证明 Faber 的贡献随区间剧烈波动：
2019~2026 它是拖累（−2.19pp），2020~2026 它是正贡献（+2.13pp），
**单一年份即可翻转符号** → 7.7 年样本太短，无法判定。

好消息：`ETF全球` 池的**数据交集起点是 2014-01-15**
（最晚上市的是 513500 标普500ETF），故回测可从 **2015-01-01** 起，
得到 **约 12 年**样本。

本脚本在该长样本上回答：
1. Faber 开/关，在长样本与各子区间上谁更好？
2. 滚动决策（过去 3 年比夏普 → 持有 1 年）在长样本上是否有价值？

用法
----
    $PY tools/faber_long_sample.py
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

FULL = ("20150101", "20260911")
SUBS = [("2015~2019", "20150101", "20191231"),
        ("2020~2026", "20200101", "20260911"),
        ("2015~2022", "20150101", "20221231"),
        ("2023~2026", "20230101", "20260911")]
WINDOW_Y, STEP_Y = 3.0, 1.0


def use_real_cost():
    config.TRADING_COST.clear()
    config.TRADING_COST.update(REAL_COST)


def curve(tw, s, e):
    r = run_backtest(codes=config.RECOMMENDED_POOLS[POOL], strategy="etf_rotation",
                     start=s, end=e, topk=TOP_K, rebalance=REBALANCE,
                     init_cash=INIT_CASH,
                     strategy_params={"risk_parity": True, "trend_window": tw})
    eq = r["result"].equity
    return eq["equity"] if "equity" in eq.columns else eq


def m(eq):
    r = eq.pct_change().dropna()
    years = len(eq) / TD
    cum = eq.iloc[-1] / eq.iloc[0] - 1
    ann = (1 + cum) ** (1 / years) - 1 if years > 0 else 0.0
    vol = r.std() * np.sqrt(TD)
    sh = r.mean() / r.std() * np.sqrt(TD) if r.std() > 0 else 0.0
    mdd = float((eq / eq.cummax() - 1).min())
    return ann * 100, vol * 100, sh, mdd * 100, (ann / abs(mdd)) if mdd else 0.0


def main() -> int:
    use_real_cost()
    print(f"池 {POOL}  topk={TOP_K}  {REBALANCE}日调仓  真实成本万5 / 10万\n")

    print("=" * 104)
    print("=== 1. Faber 开/关 在各区间的对比 ===")
    print("  {:<14}{:>30}{:>30}{:>14}".format("区间", "开 Faber (tw=60)",
                                              "关 Faber (tw=None)", "Faber 净贡献"))
    rows = []
    for tag, s, e in [("**长样本 2015~2026**", *FULL)] + SUBS:
        try:
            a, b = curve(60, s, e), curve(None, s, e)
            ma, mb = m(a), m(b)
            d_ann = ma[0] - mb[0]
            d_sh = ma[2] - mb[2]
            print("  {:<14}{:>30}{:>30}{:>14}".format(
                tag,
                f"{ma[0]:>6.2f}% 夏普{ma[2]:>5.2f} 回撤{ma[3]:>7.2f}%",
                f"{mb[0]:>6.2f}% 夏普{mb[2]:>5.2f} 回撤{mb[3]:>7.2f}%",
                f"{d_ann:+.2f}pp / {d_sh:+.2f}"))
            rows.append({"period": tag, "ann_on": ma[0], "sh_on": ma[2],
                         "dd_on": ma[3], "ann_off": mb[0], "sh_off": mb[2],
                         "dd_off": mb[3], "faber_ann_pp": d_ann,
                         "faber_sh": d_sh})
        except Exception as ex:
            print(f"  {tag:<14} 失败: {type(ex).__name__}: {ex}")

    print("\n" + "=" * 104)
    print("=== 2. 长样本上的滚动决策（回看 3 年 → 持有 1 年）===")
    on, off = curve(60, *FULL), curve(None, *FULL)
    idx = on.index.intersection(off.index)
    on, off = on.reindex(idx), off.reindex(idx)
    on, off = on / on.iloc[0], off / off.iloc[0]
    w, st = int(WINDOW_Y * TD), int(STEP_Y * TD)
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
                      "sh_on": round(sc["开"], 3), "sh_off": round(sc["关"], 3)})
        if pick != prev:
            print(f"  {idx[i]} ~ {idx[j]}: 选 **{pick}**"
                  f"（过去3年夏普 开{sc['开']:.2f} / 关{sc['关']:.2f}）")
        prev = pick
        i = j
    allr = pd.concat(rets)
    allr = allr[~allr.index.duplicated(keep="first")].sort_index()
    roll = (1 + allr).cumprod()
    roll = pd.concat([pd.Series([1.0], index=[idx[0]]), roll])

    mr, mo, mf = m(roll), m(on), m(off)
    print("\n  {:<26}{:>10}{:>9}{:>8}{:>10}{:>8}".format(
        "配置", "年化%", "波动%", "夏普", "回撤%", "卡玛"))
    for tag, v in [("始终开 Faber", mo), ("始终关 Faber", mf),
                   ("滚动决策（样本外）", mr)]:
        print("  {:<26}{:>10.2f}{:>9.2f}{:>8.2f}{:>10.2f}{:>8.2f}".format(
            tag, v[0], v[1], v[2], v[3], v[4]))
    n_on = sum(1 for p in picks if p["pick"] == "开")
    print(f"\n  滚动 {len(picks)} 次决策：选开 {n_on} / 选关 {len(picks)-n_on}")
    print("  决策区间 {} ~ {}".format(picks[0]["start"], picks[-1]["end"]))

    better = max(mo[2], mf[2])
    print("\n=== 判据（长样本）===")
    if mr[2] > better + 0.05:
        print("  ✅ 滚动切换优于两个固定选择 → 切换有价值")
    elif mr[2] >= better - 0.05:
        print("  ⚪ 滚动与最佳固定选择持平 → 切换不产生价值")
    else:
        print("  ❌ 滚动劣于最佳固定选择 → 切换是负贡献")
    print("  固定选择对比: 开 夏普 {:.2f}  vs  关 夏普 {:.2f}  → {}".format(
        mo[2], mf[2], "开更好" if mo[2] > mf[2] else "关更好"))

    pd.DataFrame(rows).to_csv("results/faber_long.csv", index=False,
                              encoding="utf-8-sig")
    pd.DataFrame(picks).to_csv("results/faber_long_picks.csv", index=False,
                               encoding="utf-8-sig")
    print("\n结果已写出 results/faber_long.csv 与 faber_long_picks.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
