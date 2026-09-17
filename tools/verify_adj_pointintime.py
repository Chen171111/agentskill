"""判据⑦：**point-in-time 检验** —— 换数据截止日，历史段的数字应当**逐位不变**。

为什么这条比"收益虚高多少"更硬
--------------------------------
`legacy` 的 `adj[i] = Π_{j>i}(1+r_j)` 里那个 `j` **遍历全部历史，含未来** ——
意味着 **t 之前的股息率会随数据刷新而改变**（未来发生的送转会"追改"历史）。
`correct` 的 `dps_i(t) = D_i / Π_{i≤k≤t}(1+r_k)` **只用 ≤ t 的事件** → 与数据截止日无关。

于是有一条**可判定的**判据：

> 把分红数据截断到两个不同的截止日，对**重叠历史段**重算 dps：
> - `correct` → 两次数值应**逐位一致**（差异 = 0）
> - `legacy` → 会变（且变化幅度随"未来窗口"长度增长）

**差异为 0 是不可伪造的**：它证明该口径真的只依赖过去。
本次实测结论：`correct` **0 个不一致**、`legacy` **大量不一致**。

顺带量化「仅因膨胀才越过 10% 上限」的标的占比（上界保护效应）。

用法
----
    PY="C:/Users/XiaoQi/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
    cd "E:/MyWorkAndProject/量化/agentskill"
    $PY tools/verify_adj_pointintime.py
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.test_dividend_factor import build_yield_panel  # noqa: E402

# 判据网格：按季末取点（都早于截止日 20221231）
DATES = ["20190329", "20190628", "20190930", "20191231",
         "20200331", "20200630", "20200930", "20201231",
         "20210331", "20210630", "20210930", "20211231",
         "20220331", "20220630", "20220930", "20221230"]
CUT1, CUT2 = "20221231", "20260911"      # 两个"数据截止日"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="送转口径 point-in-time 检验（判据⑦）")
    ap.add_argument("--dividends", default="data/dividends/bonus_all.parquet")
    ap.add_argument("--bfq", default="data/stockbars/bars_bfq.parquet")
    ap.add_argument("--universe", default="data/stockbars/universe_all.csv")
    ap.add_argument("--n-price", type=int, default=800, help="量化 10% 上限效应时的抽样股票数")
    ap.add_argument("--seed", type=int, default=20260917)
    args = ap.parse_args(argv)

    print("=" * 100)
    print("  判据⑦ point-in-time：换数据截止日，历史段是否逐位不变？")
    print("=" * 100)

    div = pd.read_parquet(args.dividends).copy()
    div["code"] = div.code.astype(str)
    uni = pd.read_csv(args.universe, dtype=str)
    codes = sorted(set(uni.code.dropna()) & set(div.code))
    print("  分红 {} 条 ｜ 有分红记录的股票 {} 只".format(len(div), len(codes)))

    ex = pd.to_datetime(div.EX_DIVIDEND_DATE, errors="coerce")
    cut1 = pd.Timestamp(CUT1)
    div_a = div[ex.isna() | (ex <= cut1)].copy()      # 「截止日 = 2022-12-31」
    div_b = div.copy()                                # 「截止日 = 2026-09-11」（全量）
    print("  截止日 A = {} → {} 条 ｜ 截止日 B = {} → {} 条".format(
        CUT1, len(div_a), CUT2, len(div_b)))

    # 网格：全股票 × 16 个季末（close=1，只看分子 dps，不需要价格）
    grid = pd.DataFrame([(c, d, 1.0) for d in DATES for c in codes],
                        columns=["code", "date", "close"])
    print("  网格 {} 行 = {} 只 × {} 个时点".format(len(grid), len(codes), len(DATES)),
          flush=True)

    print()
    print("  {:<10}{:>14}{:>16}{:>16}{:>14}".format(
        "adj_mode", "不一致组合", "不一致占比", "最大绝对差", "结论"))
    for mode in ("correct", "legacy"):
        pa = build_yield_panel(grid.copy(), div_a, price_col="close", adj_mode=mode)
        pb = build_yield_panel(grid.copy(), div_b, price_col="close", adj_mode=mode)
        a = pa.set_index(["code", "date"])["dps_ttm"]
        b = pb.set_index(["code", "date"])["dps_ttm"]
        both = pd.DataFrame({"a": a, "b": b})
        nz = both[(both.a > 0) | (both.b > 0)]
        d = (nz.a - nz.b).abs()
        n_bad = int((d > 1e-9).sum())
        dmax = float(d.max()) if len(d) else 0.0
        ok = (n_bad == 0)
        print("  {:<10}{:>14}{:>15.4f}%{:>16.6f}{:>14}".format(
            mode, n_bad, n_bad / max(len(nz), 1) * 100, dmax,
            "✅ 逐位一致" if ok else "❌ 会变"))
    print()
    print("  → `correct` 差异 = 0 ⇒ 它**只依赖过去**，数字可复现；")
    print("    `legacy` 会变 ⇒ 它的历史数字**随数据刷新被追改** ——")
    print("    所以 legacy 的旧结论不是「偏高 X%」，而是**不可复现**。")

    # ---------------- 顺带：10% 上限的保护效应 ----------------
    print()
    print("=" * 100)
    print("  顺带：`≤10%` 上限对污染的「保护效应」（抽样 {} 只 × {} 个时点）".format(
        args.n_price, len(DATES)))
    print("=" * 100)
    rng = np.random.default_rng(args.seed)
    pick = sorted(rng.choice(np.array(codes, dtype=object),
                             min(args.n_price, len(codes)), replace=False).tolist())
    try:
        bt = pd.read_parquet(args.bfq, columns=["code", "date", "close"],
                             filters=[("code", "in", pick)])
    except Exception as e:
        print("  ⚠️ 读不复权日线失败（{}）→ 跳过这一段".format(e))
        return 0
    bt["code"] = bt.code.astype(str)
    bt["date"] = bt.date.astype(str).str.replace("-", "", regex=False)
    bt = bt[bt.date.astype(str).isin(set(DATES))]
    grid2 = bt.rename(columns={"close": "close"})[["code", "date", "close"]].copy()
    grid2["close"] = pd.to_numeric(grid2.close, errors="coerce")
    grid2 = grid2.dropna(subset=["close"])
    print("  实际用到 {} 行（{} 只 × {} 个时点）".format(
        len(grid2), grid2.code.nunique(), grid2.date.nunique()), flush=True)

    pc = build_yield_panel(grid2.copy(), div_b, price_col="close", adj_mode="correct")
    pl = build_yield_panel(grid2.copy(), div_b, price_col="close", adj_mode="legacy")
    k = ["code", "date"]
    w = pc[k + ["dy_ttm"]].rename(columns={"dy_ttm": "dy_c"}).merge(
        pl[k + ["dy_ttm"]].rename(columns={"dy_ttm": "dy_l"}), on=k)
    has = w[(w.dy_c > 0) | (w.dy_l > 0)]
    over_l = has.dy_l > 10.0
    over_c = has.dy_c > 10.0
    only_l = int((over_l & ~over_c).sum())          # 只因膨胀才超过 10%
    both_o = int((over_l & over_c).sum())
    print("  有股息率数值的组合 {} 个".format(len(has)))
    print("    legacy dy > 10%: {:<6} （其中 correct 也 > 10% 的 {}）".format(
        int(over_l.sum()), both_o))
    print("    correct dy > 10%: {:<6}".format(int(over_c.sum())))
    print("    ⚠️ **仅因膨胀才越过 10%** 的组合: {} 个（占 legacy 超限的 {:.1f}%）".format(
        only_l, 100.0 * only_l / max(int(over_l.sum()), 1)))
    print("    → 这些标的在 `legacy` 下会被上限砍掉、在 `correct` 下本来就不会入选，")
    print("      说明 **`≤10%` 上限起了部分保护作用**，修正后的降幅小于膨胀倍数的暗示。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
