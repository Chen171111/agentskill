"""市场状态（regime）择时检验：什么时候该用个股多因子模型？

动机
----
个股多因子模型是**防御型**的：熊市/调整年超额为正（2022 +8.49pp、2024 +11.22pp），
小盘牛市年跑输（2020 −12.25pp、2025 −18.93pp）。
若能用**事前可得**的市场状态指标提前分辨，就能只在有利环境启用它。

指标（全部滚动 + `shift(1)`，**无前视**）
----------------------------------------
| 指标 | 含义 |
|---|---|
| `hs300_mom60` | 沪深300 的 60 日动量 —— 市场趋势 |
| `small_big60` | 中证1000 的 60 日收益 − 沪深300 的 60 日收益 —— 小盘相对强弱 |
| `hs300_vol20` | 沪深300 的 20 日已实现波动率（年化）—— 市场风险 |
| `ew_mom60` | 等权全池（基准净值）的 60 日动量 —— 全市场趋势 |

做法：每个指标按**历史分位**分 3 档，统计模型在各档的超额收益。
若分档之间超额差异显著 → 该指标有择时价值。

用法
----
    $PY tools/diag_regime.py --model results/stock_tilt_all.parquet \
        --indexes data/indexes/indexes_all.parquet
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TRADING_DAYS = 244.0


def ann_stats(ret: pd.Series) -> dict:
    ret = ret.dropna()
    if len(ret) < 20:
        return {}
    eq = (1 + ret).cumprod()
    years = len(ret) / TRADING_DAYS
    cum = eq.iloc[-1] - 1
    ann = (1 + cum) ** (1 / years) - 1 if years > 0 else 0.0
    vol = ret.std() * np.sqrt(TRADING_DAYS)
    sharpe = ret.mean() / ret.std() * np.sqrt(TRADING_DAYS) if ret.std() > 0 else 0.0
    mdd = float((eq / eq.cummax() - 1).min())
    return {"年化": ann * 100, "波动": vol * 100, "夏普": sharpe,
            "回撤": mdd * 100, "累计": cum * 100, "n日": len(ret)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="results/stock_tilt_all.parquet")
    ap.add_argument("--indexes", default="data/indexes/indexes_all.parquet")
    ap.add_argument("--out", default="results/regime.csv")
    args = ap.parse_args(argv)

    m = pd.read_parquet(args.model)
    m.index = m.index.astype(str).str.replace("-", "", regex=False)
    m = m.sort_index()
    print(f"模型净值: {len(m)} 行, {m.index.min()} ~ {m.index.max()}, 列={list(m.columns)}")

    ix = pd.read_parquet(args.indexes)
    ix["date"] = ix.date.astype(str).str.replace("-", "", regex=False)
    px = ix.pivot_table(index="date", columns="code", values="close").sort_index()
    print(f"指数: {px.shape[0]} 行 × {px.shape[1]} 个  {list(px.columns)}\n")

    df = m.copy()
    df["r_model"] = df.equity.pct_change()
    df["r_bench"] = df.bench.pct_change()

    # ---------- 构造 regime 指标（全部 shift(1)，无前视） ----------
    def mom(s, n):
        return (s / s.shift(n) - 1).shift(1)

    hs300 = px["SH000300"]
    zz1000 = px["SH000852"]

    df["hs300_mom60"] = mom(hs300, 60).reindex(df.index)
    df["ew_mom60"] = mom(df.bench, 60)
    df["small_big60"] = (mom(zz1000, 60) - mom(hs300, 60)).reindex(df.index)
    df["hs300_vol20"] = (hs300.pct_change().rolling(20).std()
                         * np.sqrt(TRADING_DAYS)).shift(1).reindex(df.index)

    INDICATORS = [
        ("hs300_mom60", "沪深300 60日动量", "市场趋势"),
        ("ew_mom60", "等权全池 60日动量", "全市场趋势"),
        ("small_big60", "小盘−大盘 60日动量差", "小盘相对强弱"),
        ("hs300_vol20", "沪深300 20日波动率", "市场风险"),
    ]

    rows = []
    for col, label, kind in INDICATORS:
        d = df.dropna(subset=[col, "r_model", "r_bench"])
        if len(d) < 120:
            continue
        # 按历史分位分 3 档（用全样本分位，仅作诊断；实盘需用滚动分位）
        q1, q2 = d[col].quantile([1 / 3, 2 / 3])
        groups = [("低", d[d[col] <= q1]), ("中", d[(d[col] > q1) & (d[col] <= q2)]),
                  ("高", d[d[col] > q2])]
        print("=" * 96)
        print(f"{label}（{kind}）  低档阈值 {q1:+.4f} / 高档阈值 {q2:+.4f}")
        print("=" * 96)
        print("  {:<6} {:>7} {:>9} {:>9} {:>10} {:>9} {:>9}".format(
            "档位", "天数", "模型年化", "基准年化", "超额pp", "模型夏普", "基准夏普"))
        for gname, g in groups:
            if len(g) < 20:
                continue
            ms, bs = ann_stats(g.r_model), ann_stats(g.r_bench)
            if not ms or not bs:
                continue
            print("  {:<6} {:>7} {:>9.2f} {:>9.2f} {:>10.2f} {:>9.2f} {:>9.2f}".format(
                gname, len(g), ms["年化"], bs["年化"],
                ms["年化"] - bs["年化"], ms["夏普"], bs["夏普"]))
            rows.append({"指标": label, "类型": kind, "档位": gname,
                         "天数": len(g), "模型年化": ms["年化"],
                         "基准年化": bs["年化"], "超额pp": ms["年化"] - bs["年化"],
                         "模型夏普": ms["夏普"], "基准夏普": bs["夏普"],
                         "模型回撤": ms["回撤"]})
        print()

    if rows:
        r = pd.DataFrame(rows)
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        r.to_csv(args.out, index=False, encoding="utf-8-sig")
        print("=" * 96)
        print("汇总：各指标「低档 − 高档」的超额差（越大说明该指标越有择时价值）")
        print("=" * 96)
        print("  {:<24} {:>12} {:>12} {:>12}".format(
            "指标", "低档超额pp", "高档超额pp", "差值pp"))
        for label, g in r.groupby("指标", sort=False):
            lo = g[g.档位 == "低"]
            hi = g[g.档位 == "高"]
            if len(lo) and len(hi):
                a, b = lo.iloc[0].超额pp, hi.iloc[0].超额pp
                print("  {:<24} {:>12.2f} {:>12.2f} {:>12.2f}".format(label, a, b, a - b))
        print(f"\n结果已写出 {args.out}")
        print("\n判读：若某指标「低档超额」明显高于「高档超额」，"
              "说明该指标低位时应启用模型、高位时应回避。")

    # ---------- 组合条件：波动率 × 趋势 ----------
    d = df.dropna(subset=["hs300_vol20", "hs300_mom60", "r_model", "r_bench"])
    if len(d) > 200:
        qv = d.hs300_vol20.quantile(1 / 3)
        qm = d.hs300_mom60.quantile(2 / 3)
        good = (d.hs300_vol20 <= qv) & (d.hs300_mom60 > qm)
        print("\n" + "=" * 96)
        print("组合条件：低波动 且 趋势向上（沪深300 波动率 ≤ 1/3 分位 且 60日动量 > 2/3 分位）")
        print("=" * 96)
        print("  {:<22} {:>7} {:>9} {:>9} {:>10} {:>9} {:>9}".format(
            "状态", "天数", "模型年化", "基准年化", "超额pp", "模型夏普", "模型回撤"))
        for name, mask in (("双优（启用）", good), ("其余（回避）", ~good)):
            g = d[mask]
            if len(g) < 20:
                continue
            ms, bs = ann_stats(g.r_model), ann_stats(g.r_bench)
            if not ms or not bs:
                continue
            print("  {:<22} {:>7} {:>9.2f} {:>9.2f} {:>10.2f} {:>9.2f} {:>9.2f}".format(
                name, len(g), ms["年化"], bs["年化"], ms["年化"] - bs["年化"],
                ms["夏普"], ms["回撤"]))
        print("\n  双优状态的年份分布（判断是否被少数时期主导）：")
        yrs = pd.Series(d.index[good.values]).str[:4].value_counts().sort_index()
        print("    " + "  ".join(f"{y}:{c}天" for y, c in yrs.items()))

    # ---------- 稳健性：改用滚动分位（无前视）重做 ----------
    WIN = 504          # 约 2 年滚动窗口
    df["vol_q"] = df.hs300_vol20.rolling(WIN, min_periods=252).rank(pct=True)
    df["mom_q"] = df.hs300_mom60.rolling(WIN, min_periods=252).rank(pct=True)
    d2 = df.dropna(subset=["vol_q", "mom_q", "r_model", "r_bench"])
    if len(d2) > 200:
        good2 = (d2.vol_q <= 1 / 3) & (d2.mom_q > 2 / 3)
        print("\n" + "=" * 96)
        print(f"稳健性检验：阈值改用**滚动 {WIN} 日分位**（无前视，样本 {len(d2)} 天）")
        print("=" * 96)
        print("  {:<22} {:>7} {:>9} {:>9} {:>10} {:>9} {:>9}".format(
            "状态", "天数", "模型年化", "基准年化", "超额pp", "模型夏普", "模型回撤"))
        for name, mask in (("双优（市场强）", good2), ("其余（市场弱）", ~good2)):
            g = d2[mask]
            if len(g) < 20:
                continue
            ms, bs = ann_stats(g.r_model), ann_stats(g.r_bench)
            if not ms or not bs:
                continue
            print("  {:<22} {:>7} {:>9.2f} {:>9.2f} {:>10.2f} {:>9.2f} {:>9.2f}".format(
                name, len(g), ms["年化"], bs["年化"], ms["年化"] - bs["年化"],
                ms["夏普"], ms["回撤"]))
        print("\n  判据：滚动分位版本若仍显示「市场强时超额差、市场弱时超额好」，")
        print("        说明规律成立且无前视；若差异消失，则原结论是全样本分位的假象。")

        # ---------- 样本内外切分（分组仍用滚动分位，无前视） ----------
        print("\n" + "=" * 96)
        print("样本内外切分（分组用滚动 504 日分位，无前视）")
        print("=" * 96)
        print("  {:<8} {:<10} {:>7} {:>9} {:>9} {:>10}".format(
            "区间", "状态", "天数", "模型年化", "基准年化", "超额pp"))
        for tag, s, e in (("样本内", "20190101", "20221231"),
                          ("样本外", "20230101", "20260911")):
            dd = d2[(d2.index >= s) & (d2.index <= e)]
            if len(dd) < 60:
                continue
            gs = (dd.vol_q <= 1 / 3) & (dd.mom_q > 2 / 3)
            for nm, msk in (("市场强", gs), ("市场弱", ~gs)):
                g = dd[msk]
                if len(g) < 20:
                    continue
                ms, bs = ann_stats(g.r_model), ann_stats(g.r_bench)
                if not ms or not bs:
                    continue
                print("  {:<8} {:<10} {:>7} {:>9.2f} {:>9.2f} {:>10.2f}".format(
                    tag, nm, len(g), ms["年化"], bs["年化"], ms["年化"] - bs["年化"]))

        # ---------- 切换成本测算 ----------
        state = good2.astype(int).values
        switches = int((np.diff(state) != 0).sum())
        years = len(d2) / TRADING_DAYS
        per_year = switches / years if years else 0
        # 一次切换 = 卖光 + 买满：佣金 0.03% ×2 + 滑点 0.05% ×2 + 印花税 0.1%
        cost_round = (0.0003 + 0.0005) * 2 + 0.001
        ann_cost = per_year * cost_round * 100

        w = good2.mean()
        gs, gw = d2[good2], d2[~good2]
        ms_s, bs_s = ann_stats(gs.r_model), ann_stats(gs.r_bench)
        ms_w, bs_w = ann_stats(gw.r_model), ann_stats(gw.r_bench)
        if all((ms_s, bs_s, ms_w, bs_w)):
            model_all = w * ms_s["年化"] + (1 - w) * ms_w["年化"]
            bench_all = w * bs_s["年化"] + (1 - w) * bs_w["年化"]
            timing = w * bs_s["年化"] + (1 - w) * ms_w["年化"]
            adv = timing - bench_all
            print("\n" + "=" * 96)
            print("切换成本测算（市场强时用基准，市场弱时用模型）")
            print("=" * 96)
            print(f"  状态切换 {switches} 次 / {years:.1f} 年 = {per_year:.1f} 次/年")
            print(f"  单次切换成本约 {cost_round * 100:.3f}%（买卖佣金+滑点+印花税）")
            print(f"  年化成本 ≈ {ann_cost:.2f}pp")
            print(f"\n  加权年化：一直用模型 {model_all:.2f}%  |  一直用基准 "
                  f"{bench_all:.2f}%  |  择时 {timing:.2f}%")
            print(f"  择时优势（vs 一直用基准）：{adv:+.2f}pp")
            print(f"  扣成本后：{adv - ann_cost:+.2f}pp  "
                  f"→ {'仍为正，方案成立' if adv - ann_cost > 0 else '被成本吃掉，方案不成立'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
