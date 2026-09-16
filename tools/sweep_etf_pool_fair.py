"""ETF 扩池：**同区间**公平对照 + 样本内外检验。

为什么必须同区间
----------------
`build_panel` 对池子取**交集** → 池里最晚上市的 ETF 决定回测起点。
上次扫描里：
- C 池「上市≤2019」名义 2019~2026，**实际 20190906 起**
- D 池「上市≤2021」名义 2019~2026，**实际 20211229 起**
- E 池「全27只」名义 2019~2026，**实际 20250409 起**（只 1.4 年！）

直接横比这些数字是**错的**（区间不同、行情不同）。
本脚本在**固定区间**上对比不同规模的池子，只有「实际起 = 区间起点」的池子才纳入。

设计
----
区间 P2 = 2022-01-01 ~ 2026-09-11：此时 A/B/C/D 与现有池**全部**已上市 →
可以公平对比 10 / 11 / 16 / 21 / 25 只这五档池子规模。

样本内外（同一池子、同一区间切两段）：
- IS : 2022-01-01 ~ 2024-06-30
- OOS: 2024-07-01 ~ 2026-09-11

用法
----
    $PY tools/sweep_etf_pool_fair.py
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                              # noqa: E402
from pipeline import run_backtest                          # noqa: E402

STRATEGY = "etf_rotation"
TOP_K = 5
REBALANCE = 5
CASH = 100_000.0

P_FAIR = ("20220101", "20260911")        # 五档池子均可比
IS = ("20220101", "20240630")
OOS = ("20240701", "20260911")


def list_etfs(d='data/stocks'):
    out = []
    for f in sorted(os.listdir(d)):
        if f.endswith('.csv') and f[:-4].split('.')[0][:2] in ('51', '56', '58', '15', '16'):
            code = f[:-4]
            df = pd.read_csv(os.path.join(d, f), dtype={'date': str}, usecols=['date'])
            df['date'] = df.date.str.replace('-', '', regex=False)
            out.append((code, df.date.min()))
    return dict(out)


def build_pools(info):
    allc = sorted(info)
    return {
        "现有 ETF全球": config.RECOMMENDED_POOLS["ETF全球"],
        "A ≤2013": [c for c in allc if info[c] <= "20131231"],
        "B ≤2016": [c for c in allc if info[c] <= "20161231"],
        "C ≤2019": [c for c in allc if info[c] <= "20191231"],
        "D ≤2021": [c for c in allc if info[c] <= "20211231"],
    }


def run(codes, s, e):
    r = run_backtest(codes=codes, strategy=STRATEGY, start=s, end=e,
                     topk=TOP_K, rebalance=REBALANCE, init_cash=CASH)
    return r["metrics"], r["meta"]


def main() -> int:
    info = list_etfs()
    pools = build_pools(info)

    print("=== 同区间公平对照（2022-01 ~ 2026-09，五档池子均可比）===")
    print("  策略 etf_rotation / topk=5 / 5日调仓 / 10万 / 万5 / ETF免印花税")
    print()
    print("  {:<14}{:>6}{:>10}{:>10}{:>10}{:>9}{:>8}{:>9}{:>8}".format(
        "池子", "标的", "实际起", "实际止", "年化%", "波动%", "夏普", "回撤%", "卡玛"))
    print("  " + "-" * 84)

    rows = []
    for name, cs in pools.items():
        try:
            m, meta = run(cs, *P_FAIR)
        except Exception as ex:
            print(f"  {name:<14} 失败: {type(ex).__name__}")
            continue
        ok = str(meta.get('start', '')) == P_FAIR[0]
        flag = "" if ok else " ⚠️区间被压"
        print("  {:<14}{:>6}{:>10}{:>10}{:>10.2f}{:>9.2f}{:>8.2f}{:>9.2f}{:>8.2f}{}".format(
            name, len(cs), str(meta.get('start')), str(meta.get('end')),
            m.get("年化收益", 0), m.get("年化波动", 0), m.get("夏普比率", 0),
            m.get("最大回撤", 0), m.get("卡玛比率", 0), flag))
        rows.append({"池子": name, "标的": len(cs), "实际起": meta.get("start"),
                     "年化": m.get("年化收益", 0), "夏普": m.get("夏普比率", 0),
                     "回撤": m.get("最大回撤", 0), "卡玛": m.get("卡玛比率", 0)})

    # ---- 样本内 / 样本外 ----
    print()
    print("=== 样本内(22-01~24-06) vs 样本外(24-07~26-09) ===")
    print("  {:<14}{:>6}{:>22}{:>22}{:>10}".format(
        "池子", "标的", "样本内 年化/夏普", "样本外 年化/夏普", "Δ夏普"))
    print("  " + "-" * 78)
    for name, cs in pools.items():
        try:
            mi, metai = run(cs, *IS)
            mo, metao = run(cs, *OOS)
        except Exception as ex:
            print(f"  {name:<14} 失败: {type(ex).__name__}")
            continue
        d = mo.get("夏普比率", 0) - mi.get("夏普比率", 0)
        print("  {:<14}{:>6}{:>22}{:>22}{:>+10.2f}".format(
            name, len(cs),
            f"{mi.get('年化收益',0):.2f}% / {mi.get('夏普比率',0):.2f}",
            f"{mo.get('年化收益',0):.2f}% / {mo.get('夏普比率',0):.2f}", d))
        rows.append({"池子": name + "[IS]", "标的": len(cs),
                     "年化": mi.get("年化收益", 0), "夏普": mi.get("夏普比率", 0),
                     "回撤": mi.get("最大回撤", 0), "卡玛": mi.get("卡玛比率", 0)})
        rows.append({"池子": name + "[OOS]", "标的": len(cs),
                     "年化": mo.get("年化收益", 0), "夏普": mo.get("夏普比率", 0),
                     "回撤": mo.get("最大回撤", 0), "卡玛": mo.get("卡玛比率", 0)})

    print()
    print("注：IS 与 OOS 是**同一池子的两段**，不是「IS 挑参数、OOS 验证」。")
    print("    池子按**上市时间**分层（中性，非按业绩挑选），故无前视偏差。")

    if rows:
        pd.DataFrame(rows).to_csv("results/etf_pool_fair.csv", index=False,
                                  encoding="utf-8-sig")
        print("\n结果已写出 results/etf_pool_fair.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
