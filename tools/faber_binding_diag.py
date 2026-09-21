"""D1 诊断：Faber（MA60 绝对趋势过滤）的门槛 **binding 率**。

要回答的问题
------------
HANDOFF §4.6-3 说「Faber 是否切换 → 等 ≥8 周影子数据」。但**影子数据是 0 条**
（机制只在调仓日记录，而 09-16 起全是非调仓日）→ 这个决策永远不会到期。

在等之前先问一句：**这个开关到底有多大影响？** 如果它在多数调仓日
**根本不改变选出的组合**（门槛不 binding），那按最简原则直接关掉即可，
不必再等一年前向数据。

做法（可复现，只看价格，不跑回测）
----------------------------------
对每个交易日：
  1. 池内 11 只按 `momentum20` 降序排（`etf_rotation` 的主排序因子）
  2. **关 Faber** → 取前 5
  3. **开 Faber** → 先剔除 `close ≤ MA60` 的，再取前 5
  4. 比较两套 top5 是否一致

同时报告「每日被 MA60 剔除的标的数」分布 —— 这是门槛强弱的直观刻画。

结论进 `docs/ETF线_收益归因.md` 与 HANDOFF。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import argparse  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import config  # noqa: E402

POOL = "ETF全球"
TOP_K = 5
REBALANCE = 5
TREND_WINDOW = 60
MOM_WINDOW = 20
START, END = "20190101", "20260911"


def main(argv=None) -> int:
    # ⚠️ 2026-09-21 新增 argparse：原来本脚本**没有命令行接口** ——
    #    传 `--help` 不会打帮助，而是**直接开跑完整诊断**（静默忽略该参数）。
    #    参数默认值与原模块常量完全一致 → 不带参数运行时结果不变。
    ap = argparse.ArgumentParser(
        description="Faber 趋势门槛 binding 率诊断（开/关 MA60 门槛的两套 top-k 对比）")
    ap.add_argument("--pool", default=POOL, help="config.RECOMMENDED_POOLS 里的池名")
    ap.add_argument("--top-k", type=int, default=TOP_K)
    ap.add_argument("--rebalance", type=int, default=REBALANCE)
    ap.add_argument("--trend-window", type=int, default=TREND_WINDOW,
                    help="趋势均线窗口（默认 60）")
    ap.add_argument("--mom-window", type=int, default=MOM_WINDOW)
    ap.add_argument("--start", default=START)
    ap.add_argument("--end", default=END)
    ap.add_argument("--out", default="results/faber_binding_diag.csv")
    args = ap.parse_args(argv)
    codes = config.RECOMMENDED_POOLS[args.pool]
    fr = {}
    for c in codes:
        p = os.path.join(ROOT, "data", "stocks", "{}.csv".format(c))
        if not os.path.exists(p):
            print("  ⚠️ 缺 {}".format(p))
            continue
        d = pd.read_csv(p, dtype={"date": str}, usecols=["date", "close"])
        fr[c] = d.set_index("date")["close"].astype(float)
    px = pd.DataFrame(fr).sort_index()
    px = px[(px.index >= args.start) & (px.index <= args.end)]
    print("池 {} ｜ {} 只 ｜ {} ~ {} ｜ {} 个交易日".format(
        args.pool, px.shape[1], px.index.min(), px.index.max(), len(px)))

    ma = px.rolling(args.trend_window).mean()
    mom = px.pct_change(args.mom_window)
    valid = mom.notna() & ma.notna()
    below = (px <= ma) & valid                    # 被 Faber 剔除
    n_below = below.sum(axis=1)

    print("\n=== 每日被 MA60 剔除的标的数（池内 {} 只）===".format(px.shape[1]))
    vc = n_below[valid.any(axis=1)].value_counts().sort_index()
    for k, v in vc.items():
        print("  剔除 {:>2} 只：{:>4} 天（{:>5.1f}%）".format(
            k, v, v / vc.sum() * 100))
    print("  平均剔除 {:.2f} 只 ｜ 中位 {:.0f} 只 ｜ 至少剔 1 只的天数占 {:.1f}%".format(
        n_below[valid.any(axis=1)].mean(), n_below[valid.any(axis=1)].median(),
        (n_below[valid.any(axis=1)] >= 1).mean() * 100))

    # ── binding 率：只在调仓日比较两套 top5 ──
    idx = list(px.index[valid.any(axis=1)])
    rebal_days = idx[::args.rebalance]
    same = diff = n_filtered_top = 0
    rows = []
    for dt in rebal_days:
        row = mom.loc[dt].where(valid.loc[dt]).dropna().sort_values(ascending=False)
        if len(row) == 0:
            continue
        top_off = list(row.index[:args.top_k])
        ok_codes = [c for c in row.index if not bool(below.loc[dt, c])]
        top_on = ok_codes[:args.top_k]
        eq = (top_off == top_on)
        same += int(eq)
        diff += int(not eq)
        if set(top_off) - set(top_on):
            n_filtered_top += 1
        rows.append({"date": dt, "n_below": int(below.loc[dt].sum()),
                     "n_cand_on": len(ok_codes), "same": eq})
    r = pd.DataFrame(rows)
    tot = len(r)
    print("\n=== binding 诊断（{} 个调仓日）===".format(tot))
    print("  两套 top5 **完全相同** : {:>4} 天（{:>5.1f}%）".format(same, same / tot * 100))
    print("  两套 top5 **不同**     : {:>4} 天（{:>5.1f}%）  ← 门槛真正生效".format(
        diff, diff / tot * 100))
    print("  被剔的标的**落进了 top5**（真正改了组合）: {:>4} 天（{:>5.1f}%）".format(
        n_filtered_top, n_filtered_top / tot * 100))
    print("\n  开 Faber 后候选数 < {} 的天数: {}".format(
        args.top_k, int((r.n_cand_on < args.top_k).sum())))
    print("  开 Faber 后候选数 == 0 的天数: {}".format(int((r.n_cand_on == 0).sum())))
    r.to_csv(os.path.join(ROOT, args.out),
             index=False, encoding="utf-8-sig")
    print("\n  明细已写出 " + args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
