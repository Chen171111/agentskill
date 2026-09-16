"""诊断：腾讯 `qfq` 前复权是**仿射变换**，不保持日收益。

结论（2026-09-15 实测）
----------------------
腾讯的 `qfq` 满足

    bfq = k · qfq + m

- **中国神华 SH601088**（无送转）：k = 1，m = 15.57（2019 年）→ **纯减法**
- **兖矿能源 SH600188**（有送转）：k ≈ 1.96，m ≈ 10.98 → 仿射

判据（区分减法 / 乘法 / 仿射）
------------------------------
对同一天的 `open/close/high/low` 四个价位：

- 若 **diff 恒定**、ratio 变化 → 减法
- 若 **ratio 恒定**、diff 变化 → 乘法（标准前复权）
- 若 **两者都不恒定**，但四个价位能被一组 (k, m) 完美拟合 → 仿射

**为什么这是个问题**：减法/仿射复权**不保持日收益**。
`qfq` 口径的日收益被放大约 `bfq/qfq` 倍。实测中国神华 2019-01-04：
真实 **+2.15%**，qfq 口径 **+17.92%**。

而 `放大倍数 = 1 + m/qfq` → **qfq 越小（跌得越惨）放大越狠**，
恰好是反转策略要买的标的。

检查项
------
1. **仿射验证**（`--sample N`，需联网）：抽样拟合 (k, m)，报告拟合残差
2. **放大倍数分布**（不需要联网）：用 `qfq + 分红数据` 算出池内每行的放大倍数
   （`m_t` = 未来累计每股现金分红，可由 `data/dividends` 直接累加）

用法
----
    PY=.../python.exe
    $PY tools/diag_qfq_adjust.py --sample 8
    $PY tools/diag_qfq_adjust.py --sample 0 --bars data/stockbars/bars_all.parquet \
        --dividends data/dividends/bonus_all.parquet
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.fetch_stock_history import _get, to_symbol

NEW = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"


def fetch(sym: str, kind: str, start: str, end: str, pages: int = 3):
    """取 {date: (open, close, high, low)}。"""
    rows, cursor, seen = {}, end, set()
    for _ in range(pages):
        t = _get(f"{NEW}?param={sym},day,{start},{cursor},800,{kind}", timeout=35)
        if not t:
            break
        try:
            node = (json.loads(t).get("data") or {}).get(sym) or {}
        except Exception:
            break
        arr = node.get("qfqday") or node.get("day") or []
        if not arr:
            break
        new = 0
        for x in arr:
            k = x[0].replace("-", "")
            if k in seen or k < start.replace("-", ""):
                continue
            seen.add(k)
            new += 1
            rows[k] = tuple(float(x[j]) for j in (1, 2, 3, 4))
        e = min(x[0] for x in arr)
        if new == 0 or e.replace("-", "") <= start.replace("-", ""):
            break
        cursor = (datetime.strptime(e, "%Y-%m-%d")
                  - timedelta(days=1)).strftime("%Y-%m-%d")
        time.sleep(0.05)
    return rows


def fit_affine(q: tuple, b: tuple) -> tuple[float, float, float]:
    """对一天内的 4 个价位拟合 b = k·q + m，返回 (k, m, 最大残差)。"""
    x = np.array(q, dtype=float)
    y = np.array(b, dtype=float)
    A = np.vstack([x, np.ones_like(x)]).T
    (k, m), *_ = np.linalg.lstsq(A, y, rcond=None)
    return float(k), float(m), float(np.abs(A @ [k, m] - y).max())


def check_affine(sample: int, start: str, end: str, bars_path: str) -> None:
    print(f"【1】仿射验证（抽样 {sample} 只，联网）")
    b = pd.read_parquet(bars_path, columns=["code", "date"])
    codes = sorted(b.code.unique())
    rng = np.random.default_rng(11)
    picked = list(rng.choice(codes, min(sample, len(codes)), replace=False))
    # 优先带上已知的高分红股，便于对照
    picked = ["SH601088", "SH600188", "SZ000002"] + [
        c for c in picked if c not in ("SH601088", "SH600188", "SZ000002")]
    print(f"  {'代码':<10}{'日期':<10}{'k':>8}{'m':>10}{'最大残差':>10}   判定")
    for code in picked[:sample + 3]:
        sym = to_symbol(code)
        q = fetch(sym, "qfq", start, end, pages=1)
        bb = fetch(sym, "bfq", start, end, pages=1)
        ks = sorted(set(q) & set(bb))
        if not ks:
            print(f"  {code:<10}{'—':<10}{'取数失败':>28}")
            continue
        k, m, res = fit_affine(q[ks[-1]], bb[ks[-1]])
        d = [bb[ks[-1]][i] - q[ks[-1]][i] for i in range(4)]
        r = [bb[ks[-1]][i] / q[ks[-1]][i] if q[ks[-1]][i] else np.nan
             for i in range(4)]
        d_flat = max(d) - min(d)
        r_flat = (max(r) - min(r)) / np.mean(r)
        if d_flat < 1e-6:
            verdict = "减法（diff 恒定）"
        elif r_flat < 1e-6:
            verdict = "乘法（ratio 恒定）"
        elif res < 0.02:
            verdict = f"**仿射** k={k:.3f}≠1"
        else:
            verdict = "都不像，需人工看"
        print(f"  {code:<10}{ks[-1]:<10}{k:>8.4f}{m:>10.4f}{res:>10.4f}   {verdict}")
    print("  → 判定依据：diff 恒定=减法；ratio 恒定=乘法；两者都不恒定但 (k,m) 拟合残差极小=仿射")
    print("  → 三种口径里**只有乘法**保持日收益；减法/仿射都会放大收益，且 qfq 越小放大越狠\n")


def check_amp(bars_path: str, div_path: str, min_price: float,
              min_amount: float, min_listed: int) -> pd.DataFrame:
    print("【2】池内放大倍数分布（不需要联网：qfq + 分红数据即可算）")
    b = pd.read_parquet(bars_path)
    b["date"] = b.date.astype(str).str.replace("-", "", regex=False)
    b = b.sort_values(["code", "date"])

    g = b.groupby("code", sort=False)
    b["amt"] = b.close * b.volume * 100.0
    b["amt_ma20"] = g.amt.transform(lambda s: s.rolling(20).mean())
    b["listed"] = g.cumcount() + 1
    b["_in"] = ((b.listed >= min_listed) & (b.amt_ma20 >= min_amount)
                & (b.close >= min_price) & (b.volume.fillna(0) > 0))

    div = pd.read_parquet(div_path)
    div = div[div.EX_DIVIDEND_DATE.notna()].copy()
    div["D"] = pd.to_numeric(div.PRETAX_BONUS_RMB, errors="coerce").fillna(0) / 10.0
    div = div[div.D > 0]

    # m_t = 未来累计每股现金分红（t 之后所有除权日的 D 之和）
    # ⚠️ 先把分红按 code 分好组再进循环 —— 在循环里做 div[div.code==code]
    # 是 5,260 × 37,779 次全表扫描，实测直接被超时杀掉。
    div_by_code = {c: g.sort_values("EX_DIVIDEND_DATE")
                   for c, g in div.groupby("code", sort=False)}
    m = np.zeros(len(b))
    for code, idx in b.groupby("code", sort=False).indices.items():
        d = div_by_code.get(code)
        if d is None or d.empty:
            continue
        ex, D = d.EX_DIVIDEND_DATE.values, d.D.values
        suf = np.concatenate([np.cumsum(D[::-1])[::-1], [0.0]])
        m[idx] = suf[np.searchsorted(ex, b.date.values[idx], side="right")]
    b["m"] = m
    b["amp"] = np.where(b.close > 0, 1.0 + b.m / b.close, np.nan)

    d = b[b._in]
    print(f"  池内 {len(d):,} 行 / {d.code.nunique():,} 只")
    for q in (0.5, 0.75, 0.9, 0.95, 0.99, 0.999):
        print(f"    p{q*100:>5.1f}  放大倍数 {d.amp.quantile(q):>8.3f}")
    print(f"    max   {d.amp.max():>8.3f}    均值 {d.amp.mean():>8.3f}")
    print()
    rows = []
    for th in (1.05, 1.10, 1.25, 1.50, 2.00, 3.00):
        n = int((d.amp > th).sum())
        rows.append({"放大倍数 >": th, "行数": n, "占比%": n / len(d) * 100,
                     "涉及股票": int(d.loc[d.amp > th, "code"].nunique())})
    t = pd.DataFrame(rows)
    print(t.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    return t


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="qfq 仿射复权诊断")
    ap.add_argument("--bars", default="data/stockbars/bars_all.parquet")
    ap.add_argument("--dividends", default="data/dividends/bonus_all.parquet")
    ap.add_argument("--sample", type=int, default=8,
                    help="仿射验证抽样股票数（0=跳过，需联网）")
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2021-12-31")
    ap.add_argument("--min-price", type=float, default=2.0)
    ap.add_argument("--min-amount", type=float, default=3e7)
    ap.add_argument("--min-listed", type=int, default=120)
    ap.add_argument("--out", default="results/diag_qfq_adjust.csv")
    args = ap.parse_args(argv)

    print("=" * 78)
    print("  qfq 复权口径诊断：bfq = k·qfq + m（仿射），不保持日收益")
    print("=" * 78)
    if args.sample:
        check_affine(args.sample, args.start, args.end, args.bars)
    t = check_amp(args.bars, args.dividends, args.min_price,
                  args.min_amount, args.min_listed)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    t.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"\n  结果已写出 {args.out}")
    print("  → 修正办法：用不复权价 + 分红回放重建总收益，见 tools/rebuild_returns.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
