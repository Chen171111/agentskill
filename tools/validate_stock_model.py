"""个股多因子模型的稳健性验证：样本内外切分 + 成本敏感性 + 权重方案。

为什么必须做
------------
1. **选择性偏差**：`revN` 的负号是**用全样本 IC 校准**的。若因子方向只是这段
   样本的巧合，模型就是过拟合。必须做样本内外切分。
2. **换手极高**：`tilt` 模式全区间 315,695 笔交易。若真实冲击成本高于回测假设，
   收益可能被吃光。必须做成本敏感性。
3. **等权 vs IC 加权**：因子权重当前等权，需验证是否劣于（或优于）按 IC 加权。

用法
----
    PY=.../python.exe
    $PY tools/validate_stock_model.py --bars data/stockbars/bars.parquet \
        --universe data/stockbars/universe.csv
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import (build_features, run, metrics, fmt,  # noqa: E402
                                  DEFAULT_FACTORS)
from tools.test_stock_factors import daily_ic_vectorized  # noqa: E402

# 样本内 / 样本外切分点
IS_START, IS_END = "20190101", "20221231"
OOS_START, OOS_END = "20230101", "20260911"


def factor_ic_table(df, factors, start, end, horizon=5):
    d = df[(df.date >= start) & (df.date <= end)].copy()
    d = d[(d.listed >= 120) & (d.amt_ma20 >= 3e7) & (d.close >= 2.0) & (~d.suspended)]
    d = d.sort_values(["code", "date"])
    d["fwd"] = d.groupby("code", sort=False).close.shift(-horizon) / d.close - 1
    out = {}
    for f in factors:
        ic = daily_ic_vectorized(d, f, "fwd")
        if len(ic) < 20:
            out[f] = (float("nan"), 0.0, 0)
            continue
        out[f] = (ic.mean(), ic.mean() / ic.std() * len(ic) ** 0.5, len(ic))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--tilt-min", type=float, default=0.45)
    ap.add_argument("--hold", type=int, default=5)
    args = ap.parse_args(argv)

    bars = pd.read_parquet(args.bars)
    bars["date"] = bars.date.astype(str).str.replace("-", "", regex=False)
    if args.universe and os.path.exists(args.universe):
        u = pd.read_csv(args.universe, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
        bars = bars[~bars.code.isin(st)]
    print(f"日线: {len(bars):,} 行, {bars.code.nunique()} 只, "
          f"{bars.date.nunique()} 交易日\n")
    print("计算因子…", flush=True)
    df = build_features(bars)
    facs = [f for f, _ in DEFAULT_FACTORS]

    # ================= 1. 样本内外因子方向一致性 =================
    print("=" * 96)
    print("=== 1. 样本内外因子 IC 方向一致性（h=5）===")
    print("  判据：两段 IC 符号相同 → 方向不是全样本校准出来的巧合。")
    ic_is = factor_ic_table(df, facs, IS_START, IS_END)
    ic_oos = factor_ic_table(df, facs, OOS_START, OOS_END)
    print("\n  {:<12}{:>24}{:>24}{:>10}".format(
        "因子", f"样本内 {IS_START[:4]}~{IS_END[:4]}", f"样本外 {OOS_START[:4]}~{OOS_END[:4]}", "方向"))
    n_ok = 0
    for f in facs:
        a, ta, na = ic_is[f]
        b, tb, nb = ic_oos[f]
        same = (a == a and b == b and np.sign(a) == np.sign(b))
        n_ok += int(same)
        print("  {:<12}{:>24}{:>24}{:>10}".format(
            f, f"{a:+.4f}(t={ta:+.1f})", f"{b:+.4f}(t={tb:+.1f})",
            "一致 ✓" if same else "翻转 ✗"))
    print(f"\n  → {n_ok}/{len(facs)} 个因子两段方向一致")

    # ================= 2. 样本内外组合表现 =================
    print("\n" + "=" * 96)
    print("=== 2. 样本内外组合表现（tilt 模式）===")
    for tag, s, e in [("样本内 " + IS_START[:4] + "~" + IS_END[:4], IS_START, IS_END),
                      ("样本外 " + OOS_START[:4] + "~" + OOS_END[:4], OOS_START, OOS_END),
                      ("全区间 2019~2026", IS_START, OOS_END)]:
        try:
            eq, tr, meta = run(df, DEFAULT_FACTORS, start=s, end=e, topk=50,
                               hold=args.hold, weight_mode="tilt",
                               tilt_min=args.tilt_min)
        except Exception as ex:
            print(f"  {tag}: 失败 {ex}")
            continue
        m = metrics(eq.equity)
        # 基准
        b = []
        dd = df[(df.date >= s) & (df.date <= e)]
        for dt, g in dd.groupby("date"):
            u = ((g.listed >= 120) & (g.amt_ma20 >= 3e7)
                 & (g.close >= 2.0) & (~g.suspended))
            b.append((dt, float(g.loc[u, "ret1"].mean())))
        beq = (1 + pd.DataFrame(b, columns=["d", "r"]).set_index("d").r).cumprod()
        bm = metrics(beq)
        print(fmt(tag + " 策略", m))
        print(fmt(tag + " 基准", bm))
        print("  {:<22} 超额 年化 {:+.2f}pp  夏普 {:+.2f}  回撤 {:+.2f}pp\n".format(
            "", m.get("年化收益", 0) - bm.get("年化收益", 0),
            m.get("夏普比率", 0) - bm.get("夏普比率", 0),
            m.get("最大回撤", 0) - bm.get("最大回撤", 0)))

    # ================= 3. 成本敏感性 =================
    print("=" * 96)
    print("=== 3. 成本敏感性（全区间，tilt 模式）===")
    print("  说明：回测里卖方向另含 0.1% 印花税；下表只调「滑点」。")
    print("  {:<12}{:>10}{:>10}{:>9}{:>10}{:>9}".format(
        "滑点(单边)", "年化%", "累计%", "夏普", "最大回撤%", "交易笔数"))
    for slip in (0.0005, 0.0010, 0.0020, 0.0030):
        eq, tr, meta = run(df, DEFAULT_FACTORS, start=IS_START, end=OOS_END,
                           topk=50, hold=args.hold, weight_mode="tilt",
                           tilt_min=args.tilt_min, slippage=slip)
        m = metrics(eq.equity)
        print("  {:<12}{:>10.2f}{:>10.2f}{:>9.2f}{:>10.2f}{:>9}".format(
            f"{slip:.2%}", m.get("年化收益", 0), m.get("累计收益", 0),
            m.get("夏普比率", 0), m.get("最大回撤", 0), meta["n_trades"]))

    # ================= 4. 权重方案：等权 vs IC 加权 =================
    print("\n" + "=" * 96)
    print("=== 4. 权重方案：等权 vs 用「样本内 IC」加权（在样本外检验）===")
    ic_w = {}
    for f in facs:
        v = ic_is[f][0]
        ic_w[f] = abs(v) if v == v else 0.0
    tot = sum(ic_w.values()) or 1.0
    ic_specs = [(f, max(ic_w[f] / tot, 0.0)) for f in facs]
    print("  样本内 IC 权重:", {f: round(w, 3) for f, w in ic_specs})
    for tag, specs in [("等权", DEFAULT_FACTORS), ("样本内IC加权", ic_specs)]:
        try:
            eq, tr, meta = run(df, specs, start=OOS_START, end=OOS_END, topk=50,
                               hold=args.hold, weight_mode="tilt",
                               tilt_min=args.tilt_min)
        except Exception as ex:
            print(f"  {tag}: 失败 {ex}")
            continue
        m = metrics(eq.equity)
        print(fmt(f"样本外 {tag}", m))
    return 0


if __name__ == "__main__":
    sys.exit(main())
