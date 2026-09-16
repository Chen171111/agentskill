"""诊断并修复 ETF 行情里的**份额拆分/折算未复权**问题。

问题
----
`data/stocks/*.csv` 是从 akshare 取的**不复权**行情。ETF 发生**份额拆分/折算**时，
价格会跳变（如 1 拆 5），但份额数变了 —— **真实收益率是连续的**。
回测引擎只看到价格，于是把跳变当成真实涨跌。

实测命中（`ETF全球` 池，单日 |收益| > 20%）：

| ETF | 日期 | 价格跳变 | 被当成 |
|---|---|---|---|
| 510500 中证500ETF | 2015-04-15 | 2.243 → 7.818 | **+248.6%** |
| 159928 消费ETF | 2021-06-25 | 4.940 → 1.261 | **−74.5%** |
| 513100 纳指ETF | 2022-01-14 | 5.192 → 1.015 | **−80.5%** |
| 513500 标普500ETF | 2022-03-30 | 2.735 → 1.390 | **−49.2%** |

其中 2 处**实际被策略持有**，造成虚假亏损（净值单日 −2.45% / −2.77%），
合计约 −5.1%，折合约 **0.66pp/年** 的虚假拖累。另 1 处（+248.6%）会制造虚假收益。

修复方法
--------
对检测到的跳变日，把**跳变日之前**的价格整段乘以跳变比率，使收益率序列连续。
**不覆盖原文件** —— 修复结果写到 `data/stocks_repaired/`，供人工核对后再决定是否替换。

用法
----
    $PY tools/diag_price_anomalies.py                    # 只诊断
    $PY tools/diag_price_anomalies.py --repair           # 诊断 + 写修复副本
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                            # noqa: E402

THRESH = 0.20          # 单日 |收益| 超过此值即视为异常（ETF 无涨跌停，但 20% 已极罕见）


def scan(path: str, thresh: float = THRESH):
    """返回 [(date, prev_close, close, ret)]。"""
    d = pd.read_csv(path, dtype={"date": str}).set_index("date").sort_index()
    r = d.close.pct_change()
    out = []
    for dt, v in r.items():
        if v == v and abs(v) > thresh:
            i = d.index.get_loc(dt)
            out.append((dt, float(d.close.iloc[i - 1]), float(d.close.iloc[i]), float(v)))
    return d, out


def repair(d: pd.DataFrame, jumps) -> pd.DataFrame:
    """把每个跳变日**之前**的价格整段乘以跳变比率，使收益连续。

    注意：从最早的跳变开始处理，避免多跳变时索引错位。
    """
    out = d.copy()
    for dt, prev_close, close, ret in jumps:      # 按日期升序
        ratio = close / prev_close                # 真实价格比率 ≈ 1（份额变了，价格才跳）
        # 把该日之前（不含该日）的价格乘以 ratio，使其与该日及之后连续
        mask = out.index < dt
        out.loc[mask, ["open", "high", "low", "close"]] *= ratio
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repair", action="store_true", help="写修复副本到 data/stocks_repaired/")
    ap.add_argument("--pool", default="ETF全球")
    args = ap.parse_args()

    codes = config.RECOMMENDED_POOLS[args.pool]
    print(f"池 {args.pool}（{len(codes)} 只）  阈值 |日收益| > {THRESH:.0%}\n")
    print("=" * 96)

    total = 0
    hits = []
    for c in codes:
        p = f"data/stocks/{c}.csv"
        if not os.path.exists(p):
            continue
        d, jumps = scan(p)
        if not jumps:
            continue
        total += len(jumps)
        print(f"{c} {config.ETF_NAMES.get(c, '')}")
        for dt, pc, cl, ret in jumps:
            print(f"    {dt}  {pc:>9.3f} -> {cl:>9.3f}   ({ret:+.1%})")
            hits.append((c, dt, pc, cl, ret))
        if args.repair:
            rp = repair(d, jumps)
            # 修复后自检
            rr = rp.close.pct_change()
            after = int((rr.abs() > THRESH).sum())
            os.makedirs("data/stocks_repaired", exist_ok=True)
            rp.reset_index().to_csv(f"data/stocks_repaired/{c}.csv", index=False,
                                    encoding="utf-8-sig")
            print(f"    → 修复副本已写出；修复后仍有 {after} 处异常")
        print()

    print("=" * 96)
    print(f"合计发现 {total} 处异常（{len({h[0] for h in hits})} 只 ETF）")

    if args.repair:
        # 把未命中的 ETF 也复制过去，保证副本目录是完整池
        for c in codes:
            src, dst = f"data/stocks/{c}.csv", f"data/stocks_repaired/{c}.csv"
            if os.path.exists(src) and not os.path.exists(dst):
                pd.read_csv(src, dtype={"date": str}).to_csv(
                    dst, index=False, encoding="utf-8-sig")
        print(f"完整修复副本已写出 data/stocks_repaired/（{len(codes)} 只）")
        print("⚠️ **未覆盖原文件**。核对无误后再决定是否替换 data/stocks/。")
    else:
        print("（加 --repair 可写修复副本，不覆盖原文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
