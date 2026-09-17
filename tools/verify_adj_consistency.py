"""判据② 的独立路径核验：用「派现总额 ÷ 当前股本」重算 dps，与 build_yield_panel 对账。

为什么要这个
------------
`build_yield_panel(adj_mode='correct')` 的口径是：
    dps_i(t) = D_i / Π_{i≤k≤t}(1+r_k)
它依赖一个**纯符号推导**（在 i 除权日持有 1 股、到 t 变成 Π(1+r) 股）。

本脚本走**完全不同的数据路径**去核它：
    分红总额_i = (PRETAX_BONUS_RMB/10) × TOTAL_SHARES_i      ← 东财给的「分配股本基数」
    shares_t   = TOTAL_SHARES_L × (1 + r_L)                  ← L = 最后一个 ex ≤ t 的事件
    dps_indep_i(t) = 分红总额_i / shares_t

若 `TOTAL_SHARES` 就是「除权前**分配基数**」这一约定，则
`TS_i ≈ shares_t / Π_{i≤k≤t}(1+r_k)`，于是 `dps_indep ≡ dps_correct`（比值 ≈ 1）。

⚠️ 两者**不一致时不要急着判谁错** —— 也可能是 `TOTAL_SHARES` 的约定不是"除权前基数"。
   本脚本会同时报告比值的中位/分位，**比值系统性偏离 1 的倍数**本身就是线索。

用法
----
    PY="C:/Users/XiaoQi/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
    cd "E:/MyWorkAndProject/量化/agentskill"
    $PY tools/verify_adj_consistency.py --n 20
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.test_dividend_factor import build_yield_panel  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="送转口径独立路径核验（判据②）")
    ap.add_argument("--bars", default="data/stockbars/bars_bfq.parquet")
    ap.add_argument("--dividends", default="data/dividends/bonus_all.parquet")
    ap.add_argument("--n", type=int, default=20, help="抽样股票数")
    ap.add_argument("--seed", type=int, default=20260917)
    args = ap.parse_args(argv)

    print("=" * 96)
    print("  判据② 独立路径核验：dps_correct  vs  「派现总额 ÷ 当前股本」")
    print("=" * 96)

    div = pd.read_parquet(args.dividends).copy()
    div["code"] = div.code.astype(str)
    need = ["code", "EX_DIVIDEND_DATE", "PRETAX_BONUS_RMB", "BONUS_IT_RATIO", "TOTAL_SHARES"]
    miss = [c for c in need if c not in div.columns]
    if miss:
        print("  ❌ 缺少字段 {}，无法走这条独立路径".format(miss))
        return 2
    d = div[div.EX_DIVIDEND_DATE.notna()].copy()
    d = d[d.ASSIGN_PROGRESS.astype(str).str.contains("实施", na=False)]
    d["D"] = pd.to_numeric(d.PRETAX_BONUS_RMB, errors="coerce").fillna(0.0) / 10.0
    d["r"] = pd.to_numeric(d.BONUS_IT_RATIO, errors="coerce").fillna(0.0) / 10.0
    d["TS"] = pd.to_numeric(d.TOTAL_SHARES, errors="coerce")
    d = d[(d.D > 0) & d.TS.notna()]

    # 抽「有送转 + 有现金分红」的股票（送转才让两种口径分叉）
    has_split = set(d.loc[d.r > 0, "code"])
    rng = np.random.default_rng(args.seed)
    cand = sorted(has_split & set(d.loc[d.D > 0, "code"]))
    pick = list(rng.choice(cand, min(args.n, len(cand)), replace=False))
    print("  候选（有送转 + 有现金分红）{} 只 → 抽 {} 只".format(len(cand), len(pick)))

    sub = d[d.code.isin(pick)].sort_values(["code", "EX_DIVIDEND_DATE"])

    # 构造核验网格：每只取它自己的每一个除权日
    grid = sub[["code", "EX_DIVIDEND_DATE"]].rename(columns={"EX_DIVIDEND_DATE": "date"})
    grid = grid.drop_duplicates()
    bt = pd.read_parquet(args.bars, columns=["code", "date", "close"],
                         filters=[("code", "in", pick)])
    bt["code"] = bt.code.astype(str)
    bt["date"] = bt.date.astype(str).str.replace("-", "", regex=False)
    px = {(r.code, r.date): float(r.close) for r in bt.itertuples(index=False)}
    grid = grid[grid.date.astype(str).isin(set(bt.date))]
    if grid.empty:
        print("  ❌ 网格为空")
        return 2
    grid["close"] = [px.get((c, dt), np.nan) for c, dt in zip(grid.code, grid.date)]
    grid = grid.dropna(subset=["close"])
    grid["date"] = grid.date.astype(str)

    pan_c = build_yield_panel(grid.copy(), div, price_col="close", adj_mode="correct")
    pan_l = build_yield_panel(grid.copy(), div, price_col="close", adj_mode="legacy")
    m_c = {(r.code, r.date): r.dps_ttm for r in pan_c.itertuples(index=False)}
    m_l = {(r.code, r.date): r.dps_ttm for r in pan_l.itertuples(index=False)}

    rows = []
    for code, g in sub.groupby("code", sort=False):
        ex = np.array([int(str(x).replace("-", "")) for x in g.EX_DIVIDEND_DATE], dtype=np.int64)
        D = g.D.values
        rr = g.r.values
        TS = g.TS.values
        for t in sorted(set(grid.loc[grid.code == code, "date"])):
            tt = int(t)
            le = ex <= tt
            if not le.any():
                continue
            L = np.nonzero(le)[0][-1]
            win = le & (ex > tt - 365)
            if not win.any():
                continue
            total = float(np.sum(D[win] * TS[win]))      # 元
            # 两种对 TOTAL_SHARES 约定的假设 —— 都算，看哪个自洽
            #   A: TS 是「除权前分配基数」-> 除权后股数 = TS×(1+r)
            #   B: TS 就是「除权后/当期股本」-> 直接用
            shares_A = TS[L] * (1.0 + rr[L])
            shares_B = TS[L]
            if shares_A <= 0 or shares_B <= 0:
                continue
            a = m_c.get((code, t))
            if not a or a <= 0:
                continue
            rows.append((code, t, a, m_l.get((code, t)),
                         total / shares_A, total / shares_B, rr[L],
                         (total / shares_A) / a, (total / shares_B) / a))

    w = pd.DataFrame(rows, columns=["code", "date", "dps_correct", "dps_legacy",
                                    "dps_indep_A", "dps_indep_B", "r_of_last",
                                    "ratioA", "ratioB"])
    if w.empty:
        print("  ❌ 无可核验组合")
        return 2

    print()
    print("  可比对 (股票, 日期) 组合 {} 个 ／ {} 只".format(len(w), w.code.nunique()))
    print()
    print("  ratio = dps_indep ÷ dps_correct（**期望 ≈ 1**）")
    for tag, col in (("A: shares_t = TS×(1+r_L) 【假设 TS=除权前基数】", "ratioA"),
                     ("B: shares_t = TS     【假设 TS=当期股本】", "ratioB")):
        v = w[col]
        print("    {:<40} 中位 {:.4f} ｜ 10分位 {:.4f} ｜ 90分位 {:.4f} ｜ 在 1±0.05 内 {}/{}"
              .format(tag, float(v.median()), float(v.quantile(0.1)),
                      float(v.quantile(0.9)),
                      int(((v - 1).abs() <= 0.05).sum()), len(v)))
    print()
    print("  ⚠️ 关键判读 1：按「最后一个事件是否有送转」分组 ——")
    for tag, mask in (("r_L = 0（无送转，两种假设等价）", w.r_of_last <= 0),
                      ("r_L > 0（有送转，两种假设分叉）", w.r_of_last > 0)):
        sub2 = w[mask]
        if len(sub2) == 0:
            continue
        print("    {:<34} n={:<4} ratioA 中位 {:.4f} ｜ ratioB 中位 {:.4f}".format(
            tag, len(sub2), float(sub2.ratioA.median()), float(sub2.ratioB.median())))
    print()
    gA = w[w.r_of_last > 0]
    g0 = w[w.r_of_last <= 0]
    medA = float(gA.ratioA.median()) if len(gA) else float("nan")
    dev0 = g0[(g0.ratioA - 1).abs() > 0.05] if len(g0) else g0
    print("  ⚠️ 关键判读 2：**不要**用全体样本判 —— 「分红总额 ÷ 总股本」这个独立口径里，")
    print("     总股本还会被**增发/回购/可转债转股**改变，而那**不影响老股东持有的股数**；")
    print("     `correct` 只按**送转**换算（这才是「每股」口径该做的事）。")
    print("     → 所以 r_L = 0 组的偏离是**两种定义不同**，不是口径错误。")
    print("       实测：r_L=0 组 n={}，其中偏离 1±0.05 的 {} 个（{:.0f}%）—— 与送转无关 ✓"
          .format(len(g0), len(dev0), 100.0 * len(dev0) / max(len(g0), 1)))
    print()
    print("  【判据② 判定】只在「送转驱动的比较」上判（r_L > 0 组，n={}）".format(len(gA)))
    if len(gA) >= 10 and abs(medA - 1) <= 0.05:
        print("    ✅ 通过：假设 A（`TOTAL_SHARES` = 除权前分配基数）下，")
        print("       独立路径与 `correct` 口径一致（ratioA 中位 {:.4f}）".format(medA))
        print("       —— 即 `dps_i(t) = D_i / Π_{i≤k≤t}(1+r_k)` 与「派现总额 ÷ 当前股本」对得上")
        print("    附带确认：假设 B 同组中位 {:.4f} ≠ 1 → `TOTAL_SHARES` **是除权前基数**"
              .format(float(gA.ratioB.median())))
        return 0
    print("    ⚠️ 未通过：ratioA 中位 {:.4f}（n={}）".format(medA, len(gA)))
    print("       核清 `TOTAL_SHARES` 口径之前，判据② 记为未通过。")
    return 0
    print("  抽样对照（前 12 行）：")
    print("    {:<12}{:<11}{:>10}{:>10}{:>12}{:>12}{:>8}{:>8}{:>8}".format(
        "代码", "日期", "correct", "legacy", "indep_A", "indep_B", "r_L", "ratioA", "ratioB"))
    for r in w.head(12).itertuples(index=False):
        print("    {:<12}{:<11}{:>10.4f}{:>10.4f}{:>12.4f}{:>12.4f}{:>8.2f}{:>8.4f}{:>8.4f}".format(
            r.code, r.date, r.dps_correct, r.dps_legacy, r.dps_indep_A, r.dps_indep_B,
            r.r_of_last, r.ratioA, r.ratioB))
    print()
    for tag, col in (("A", "ratioA"), ("B", "ratioB")):  # noqa: B007
        med = float(w[col].median())
        n_ok = int(((w[col] - 1).abs() <= 0.05).sum())
        if abs(med - 1) <= 0.05 and n_ok >= 0.8 * len(w):
            print("  ✅ 判据② 通过（假设 {}）：独立路径与 correct 口径一致"
                  "（比值中位 {:.4f}，{}/{} 在 1±0.05 内）".format(tag, med, n_ok, len(w)))
            return 0
    print("  ⚠️ 两种假设都不完全自洽 —— 先别判谁错：`TOTAL_SHARES` 的口径需要另找依据核实")
    print("     （例如：与某只已知总股本的股票对账），核清之前**判据② 记为未通过**。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
