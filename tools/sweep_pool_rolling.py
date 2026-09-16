"""ETF 池选择：**滚动样本外**评估（回答「该用哪个池」而不是「哪个池历史最好」）。

为什么必须滚动
--------------
用一张全历史表挑最优池 = 过拟合。本会话在个股线上刚踩过这个坑
（宽度门样本内 0.76%→12.27%、样本外 5.19%→1.28%）。
正确做法是**只用过去数据选池，在随后一段验证**。

方法
----
对每个池子先跑一次全区间回测，得到逐日净值曲线（避免重复回测）。
然后按滚动步长推进：

    在时点 t：用 [t−W, t] 的夏普比率选出"过去最好"的池
    在 [t, t+step]：持有该池，取它的实际收益
    滚动拼接 → 一条**只用历史信息**的净值曲线

对照基准：
- **固定池**（每个池子单独持有全期）——实盘现在就是固定用 `ETF全球`
- **等权全池**（5 个池子等权，分散化下界）
- **事后最优**（全期最好的那个池，上界，不可实现，仅作参考）

口径
----
真实成本（万5 / 10万），与 `docs/ETF线_真实成本复核.md` 一致。

用法
----
    $PY tools/sweep_pool_rolling.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                            # noqa: E402
from pipeline import run_backtest                        # noqa: E402

# 参与评选的池子。**不含 `科技进攻`**——它含 2024~2025 才上市的新 ETF，
# build_panel 取交集后历史过短，无法参与滚动评估（见 docs/ETF线_真实成本复核.md 第四节）。
POOLS = ["ETF全球", "ETF稳健", "ETF宽基", "ETF行业", "ETF防御"]
STRATEGY = "etf_rotation"
TOP_K, REBALANCE = 5, 5
INIT_CASH = 100_000.0
REAL_COST = {"commission_rate": 0.0005, "min_commission": 5.0,
             "sell_tax_rate": 0.001, "slippage_rate": 0.0005}

START, END = "20190101", "20260911"
WINDOW = 3          # 选池回看窗口（年）
STEP = 1            # 持有步长（年）
TD = 244


def use_real_cost():
    config.TRADING_COST.clear()
    config.TRADING_COST.update(REAL_COST)


def curve(pool: str) -> pd.Series:
    codes = config.RECOMMENDED_POOLS[pool]
    r = run_backtest(codes=codes, strategy=STRATEGY, start=START, end=END,
                     topk=TOP_K, rebalance=REBALANCE, init_cash=INIT_CASH)
    eq = r["result"].equity
    return eq.equity if "equity" in eq.columns else eq["equity"]


def stats(eq: pd.Series, tag: str):
    ret = eq.pct_change().dropna()
    years = len(eq) / TD
    cum = eq.iloc[-1] / eq.iloc[0] - 1
    ann = (1 + cum) ** (1 / years) - 1 if years > 0 else 0.0
    vol = ret.std() * np.sqrt(TD)
    sh = ret.mean() / ret.std() * np.sqrt(TD) if ret.std() > 0 else 0.0
    mdd = float((eq / eq.cummax() - 1).min())
    print("  {:<26} 年化 {:>7.2f}%  波动 {:>6.2f}%  夏普 {:>5.2f}  "
          "回撤 {:>7.2f}%  卡玛 {:>5.2f}".format(
              tag, ann * 100, vol * 100, sh, mdd * 100,
              (ann / abs(mdd)) if mdd else 0.0))
    return {"配置": tag, "年化": ann * 100, "波动": vol * 100, "夏普": sh,
            "最大回撤": mdd * 100, "卡玛": (ann / abs(mdd)) if mdd else 0.0}


def main() -> int:
    use_real_cost()
    print(f"池子候选: {', '.join(POOLS)}")
    print(f"口径: 真实成本万5 / 资金 {INIT_CASH:,.0f} / 策略 {STRATEGY} / "
          f"topk={TOP_K} / {REBALANCE}日")
    print(f"滚动: 回看 {WINDOW} 年选池，持有 {STEP} 年，区间 {START}~{END}\n")

    print("跑各池全区间回测…", flush=True)
    curves = {}
    for p in POOLS:
        try:
            curves[p] = curve(p)
            print(f"  {p}: {len(curves[p])} 个交易日", flush=True)
        except Exception as e:
            print(f"  {p}: 失败 {type(e).__name__}: {e}")

    # 对齐到公共交易日
    idx = None
    for s in curves.values():
        idx = s.index if idx is None else idx.intersection(s.index)
    curves = {p: s.reindex(idx).ffill().bfill() for p, s in curves.items()}
    # 归一化到 1
    curves = {p: s / s.iloc[0] for p, s in curves.items()}
    print(f"对齐后: {len(idx)} 个交易日 ({idx.min()} ~ {idx.max()})\n")

    print("=" * 108)
    print("=== 1. 固定池（现状对照）===")
    rows = [stats(curves[p], f"固定 {p}" + ("  ← 实盘在用" if p == "ETF全球" else ""))
            for p in POOLS]

    print("\n" + "=" * 108)
    print("=== 2. 滚动样本外选池（只用历史信息）===")
    w_days, s_days = WINDOW * TD, STEP * TD
    seg_rets, picked, prev = [], [], None
    i = w_days
    while i < len(idx):
        j = min(i + s_days, len(idx) - 1)
        if j <= i:
            break
        # 只用 [i-w, i] 的数据算各池夏普
        scores = {}
        for p, s in curves.items():
            r = s.iloc[i - w_days:i].pct_change().dropna()
            scores[p] = (r.mean() / r.std() * np.sqrt(TD)) if r.std() > 0 else -9
        best = max(scores, key=scores.get)
        # 持有 [i, j]：取**段内日收益**，最后统一复利拼接。
        # ⚠️ 不能把各段净值归一化后直接 concat —— 不同池在边界处净值水平不同，
        #    会产生人为跳变，把波动率抬到高于任何单池（曾误得 14.33% vs 单池 ~8%）。
        seg = curves[best].iloc[i:j + 1]
        seg_rets.append(seg.pct_change().dropna())
        picked.append({"start": idx[i], "end": idx[j], "pool": best,
                       "trailing_sharpe": round(scores[best], 3)})
        if best != prev:
            print(f"  {idx[i]} ~ {idx[j]}: 选中 {best} "
                  f"(过去{WINDOW}年夏普 {scores[best]:.2f})", flush=True)
        prev = best
        i = j
    allr = pd.concat(seg_rets)
    allr = allr[~allr.index.duplicated(keep="first")].sort_index()
    roll = (1 + allr).cumprod()
    roll = pd.concat([pd.Series([1.0], index=[idx[0]]), roll])
    rows.append(stats(roll, "滚动选池（样本外）"))

    print("\n" + "=" * 108)
    print("=== 3. 其它对照 ===")
    eqw = pd.concat([curves[p] for p in POOLS], axis=1).mean(axis=1)
    stats(eqw, "等权全池（分散化）")
    best_p = max(POOLS, key=lambda p: curves[p].iloc[-1])
    stats(curves[best_p], f"事后最优（{best_p}，不可实现）")

    # ---- 关键：滚动结果与固定池必须**同区间**比较 ----
    print("\n" + "=" * 108)
    print("=== 4. ⚠️ 同区间对照（滚动覆盖的区间内，各固定池表现）===")
    print(f"  滚动区间: {roll.index.min()} ~ {roll.index.max()}"
          f"（因需 {WINDOW} 年回看，起点被推后；固定池全区间是 {idx.min()} 起）")
    print("  拿不同区间的数字对比会得出错误结论——必须切到同一段。")
    same = []
    for p in POOLS:
        s = curves[p].reindex(roll.index).ffill().bfill()
        same.append(stats(s, f"固定 {p}" + ("  ← 实盘在用" if p == "ETF全球" else "")))
    m_roll = stats(roll, "滚动选池（样本外）")
    b = curves["ETF全球"].reindex(roll.index).ffill().bfill()
    br = b.pct_change().dropna()
    b_sh = br.mean() / br.std() * np.sqrt(TD) if br.std() > 0 else 0.0
    print("\n  → 同区间下：滚动选池夏普 {:.2f}  vs  固定 ETF全球夏普 {:.2f}"
          "  差异 {:+.2f}".format(m_roll["夏普"], b_sh, m_roll["夏普"] - b_sh))
    print("  ⚠️ 本次滚动只有 {} 次选池决策——样本极小，".format(len(picked))
          + "单次运气即可主导结果，**不足以支持改实盘**。".format())

    print("\n" + "=" * 108)
    print("=== 结论判据 ===")
    r_ret = roll.pct_change().dropna()
    r_sh = r_ret.mean() / r_ret.std() * np.sqrt(TD)
    print(f"  滚动选池（同区间）夏普 {r_sh:.2f}  vs  固定 ETF全球 {b_sh:.2f}"
          f"  差异 {r_sh - b_sh:+.2f}")
    print("  判据：① 必须同区间比；② 决策次数要够（本次仅 {} 次，太少）；".format(len(picked)))
    print("        ③ 差异要明显且稳健。三条都满足才值得改实盘。")

    pd.DataFrame(rows).to_csv("results/pool_rolling.csv", index=False,
                              encoding="utf-8-sig")
    pd.DataFrame(picked).to_csv("results/pool_rolling_picks.csv", index=False,
                                encoding="utf-8-sig")
    print("\n结果已写出 results/pool_rolling.csv 与 pool_rolling_picks.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
