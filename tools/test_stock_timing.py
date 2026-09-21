"""个股因子模型的「横截面输出」能否给 ETF 轮动线做择时？

动机
----
10 万资金下个股多因子线不可落地（需 2243 只持仓，门槛 2243 万）。
但模型每天都会产出一个**全市场横截面状态**——这是免费的副产品。
本脚本检验：这个状态能否预测 ETF 池的未来收益，从而给已有的 ETF 轮动线做择时。

候选信号（都是逐日可得、无需个股持仓）
--------------------------------------
| 信号 | 含义 | 直觉方向 |
|---|---|---|
| `breadth` | 因子综合分 > 0.5 的个股占比 | 高 = 多数股票"看起来好" |
| `disp` | 因子综合分的横截面标准差 | 高 = 选股环境分化 |
| `disp_ret` | 日收益的横截面标准差 | 高 = 个股分化大 |
| `above_ma` | 站上自身 MA60 的个股占比 | 经典市场宽度 |
| `score_med` | 因子综合分的中位数 | 整体"质量"水平 |

方向不预设——**用样本内数据定方向，样本外验证**。

严格性约定
----------
- 样本内 `2019-01-01 ~ 2022-12-31`，样本外 `2023-01-01 ~ 2026-09-11`
- **方向与阈值只在样本内定**，样本外不得回头调
- 若样本外 IC 符号翻转或衰减到接近 0 → **判定不可用，如实报告**

用法
----
    $PY tools/test_stock_timing.py --bars data/stockbars/bars_all.parquet \
        --universe data/stockbars/universe_all.csv
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import build_features  # noqa: E402

ETF_PREFIXES = ("51", "56", "58", "15", "16")
# ETF稳健池（与 compare_bigorder.py 一致）
POOL = ["510300.SH", "510500.SH", "510050.SH", "159915.SZ", "159949.SZ",
        "510880.SH", "518880.SH", "512880.SH", "512690.SH", "159928.SZ"]
IS_START, IS_END = "20190101", "20221231"
OOS_START, OOS_END = "20230101", "20260911"

FACTORS = ["rev20", "rev60", "rev120", "rev5",
           "vol20", "max20", "turn20", "illiq20"]


def load_etf_close(etf_dir: str) -> pd.DataFrame:
    frames = {}
    for p in glob.glob(os.path.join(etf_dir, "*.csv")):
        code = os.path.splitext(os.path.basename(p))[0]
        if not code.split(".")[0].startswith(ETF_PREFIXES):
            continue
        d = pd.read_csv(p, dtype={"date": str}, usecols=["date", "close"])
        frames[code] = d.set_index("date")["close"]
    return pd.DataFrame(frames).sort_index()


def build_signals(df: pd.DataFrame, min_listed=120, min_amount=3e7,
                  min_price=2.0) -> pd.DataFrame:
    """逐日横截面状态 -> 日频信号序列。"""
    d = df[(df.listed >= min_listed) & (df.amt_ma20 >= min_amount)
           & (df.close >= min_price) & (~df.suspended)].copy()
    print(f"  候选池: {len(d):,} 行, {d.date.nunique()} 交易日")
    # 逐日横截面排名（向量化）
    for f in FACTORS:
        d[f"_r_{f}"] = d.groupby("date")[f].rank(pct=True)
    rcols = [f"_r_{f}" for f in FACTORS]
    d["score"] = d[rcols].mean(axis=1)
    d["_above"] = (d.close > d.ma60).astype(float)

    g = d.groupby("date")
    sig = pd.DataFrame({
        "breadth": g.score.apply(lambda s: float((s > 0.5).mean())),
        "disp": g.score.std(),
        "disp_ret": g.ret1.std(),
        "above_ma": g._above.mean(),
        "score_med": g.score.median(),
        "n": g.score.size(),
    })
    return sig


def daily_ic(sig: pd.Series, fwd: pd.Series) -> pd.Series:
    """两个日频序列的滚动无关 IC —— 这里直接用全期 Spearman（单序列）。"""
    d = pd.concat([sig.rename("s"), fwd.rename("y")], axis=1).dropna()
    if len(d) < 20:
        return pd.Series(dtype=float)
    return pd.Series([d.s.rank().corr(d.y.rank())])


def ic_of(sig: pd.Series, fwd: pd.Series):
    d = pd.concat([sig.rename("s"), fwd.rename("y")], axis=1).dropna()
    if len(d) < 20:
        return float("nan"), 0
    return d.s.rank().corr(d.y.rank()), len(d)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--etf-dir",
                    default="data/stocks",
                    help="ETF 日线目录（仓库内相对路径；原来硬编码了作者机器的绝对路径）")
    ap.add_argument("--out", default="results/stock_timing.csv")
    args = ap.parse_args(argv)

    bars = pd.read_parquet(args.bars)
    bars["date"] = bars.date.astype(str).str.replace("-", "", regex=False)
    if args.universe and os.path.exists(args.universe):
        u = pd.read_csv(args.universe, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
        bars = bars[~bars.code.isin(st)]
    print(f"日线: {len(bars):,} 行, {bars.code.nunique()} 只", flush=True)
    print("计算因子…", flush=True)
    df = build_features(bars)
    print("构建横截面信号…", flush=True)
    sig = build_signals(df)
    print(f"信号: {len(sig)} 交易日 ({sig.index.min()} ~ {sig.index.max()})\n")

    # ETF 池等权收益
    px = load_etf_close(args.etf_dir)
    pool = [c for c in POOL if c in px.columns]
    print(f"ETF 池: {len(pool)} 只")
    eq = px[pool]
    mret = eq.pct_change().mean(axis=1)          # 等权 ETF 池日收益

    # 未来 h 日收益（T 日信号 -> T+1 开盘买 -> T+h 收盘卖，这里用收盘近似）
    rows = []
    print("=" * 100)
    print("=== 预测力检验（信号 T 日 -> ETF 池未来 h 日收益）===")
    for h in (1, 5, 20):
        fwd = (1 + mret).rolling(h).apply(np.prod, raw=True).shift(-h) - 1
        print(f"\n  h={h}:")
        print("    {:<12}{:>16}{:>16}{:>12}".format("信号", "样本内 19~22", "样本外 23~26", "判定"))
        for c in ["breadth", "disp", "disp_ret", "above_ma", "score_med"]:
            s_is = sig[c][(sig.index >= IS_START) & (sig.index <= IS_END)]
            s_oos = sig[c][(sig.index >= OOS_START) & (sig.index <= OOS_END)]
            ic_is, n_is = ic_of(s_is, fwd.reindex(s_is.index))
            ic_oos, n_oos = ic_of(s_oos, fwd.reindex(s_oos.index))
            same = (ic_is == ic_is and ic_oos == ic_oos
                    and np.sign(ic_is) == np.sign(ic_oos))
            strong = abs(ic_oos) >= 0.05 and abs(ic_is) >= 0.05
            verdict = ("可用" if (same and strong) else
                       "方向一致但弱" if same else "符号翻转 ✗")
            print("    {:<12}{:>16}{:>16}{:>12}".format(
                c, f"{ic_is:+.4f}(n={n_is})", f"{ic_oos:+.4f}(n={n_oos})", verdict))
            rows.append({"horizon": h, "signal": c,
                         "ic_is": ic_is, "ic_oos": ic_oos,
                         "sign_same": same, "verdict": verdict})

    pd.DataFrame(rows).to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"\n结果已写出 {args.out}")

    print("\n=== 判据 ===")
    print("  可用 = 样本内外符号一致 且 两者 |IC| ≥ 0.05")
    print("  只要出现符号翻转，无论幅度多大都判不可用（那是噪声）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
