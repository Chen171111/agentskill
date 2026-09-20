"""构造「引擎内扣红利税」所需的**每股派现表**（D4，2026-09-20）。

背景：为什么要把红利税从「换数据文件」挪进引擎
------------------------------------------------
原先的「税后」口径是**换一份数据文件** —— `rebuild_returns.py --tax-rate 0.1`
把分红按税后金额回放进 `close` 路径，得到 `bars_total_tax10.parquet`。

⚠️ **问题**：那个 `close` 会进入**价格类过滤与动量因子**（`min_price >= 2.0`、
`rev20`/`rev60`、涨跌停判据……）→ **税后版与无税版选出来的股可能不同**。
于是长窗口里 `税后 − 无税` 的 Δ **不能读作「税成本」** —— 它混杂了「换了组合」。

本工具为「引擎内扣税」提供输入：价格路径**保持为 `bars_total`（税前）不变**，
让 `tools/backtest_stock.py::run()` 在**除权日**按持仓扣掉税额
（`--tax-rate` / `--div-tax`），从而做到「**同一组合只扣税**」。

口径换算（关键，别跳过）
------------------------
`bars_total` 是**总收益路径**（分红再投资）且 `rebuild_returns.rebuild()` 做了
「**末日锚定到真实价**」。因此定义

    g(t) = bars_total.close(t) / bars_bfq.close(t)

实测性质（2026-09-20 核）：
- `g(末日) = 1.0000`（锚定的直接结果）
- `g(t) ≤ 1` 且随 t **单调递增到 1**；分红越多的股票 g 越低
  （`SH600000` 0.70 → 0.81 → 1.00，`SH601088` 0.55 → 0.72 → 1.00）
- 全样本中位数 0.975、5% 分位 0.515

引擎里的 `units` 是**该口径下的股数**，所以除权日要扣的「每股派现」必须换算到同一口径：

    dps_adj(t) = D(t) × g(t−1)

其中 `D(t)` = 除权日**每股税前派现**（元/股；**送转不计税**，故只用 `PRETAX_BONUS_RMB`）。
`D` 的提取复用 `tools/rebuild_returns.load_events()`（**单一来源**，避免两处口径漂移）。

输出
----
`data/stockbars/div_tax_table.parquet`，列：`date, code, dps_adj`，**只含除权日**（约 3 万行）。
引擎在其余日期不扣税。

用法
----
    $PY tools/build_div_tax.py
    $PY tools/build_div_tax.py --out data/stockbars/div_tax_table.parquet
"""
from __future__ import annotations

import argparse
import os
import sys
from functools import lru_cache

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.rebuild_returns import load_events  # noqa: E402

DEFAULT_DIV_TAX = "data/stockbars/div_tax_table.parquet"


@lru_cache(maxsize=4)
def load_div_tax(path: str = DEFAULT_DIV_TAX) -> pd.DataFrame:
    """加载扣税表 —— **供各回测脚本统一调用**（避免每个脚本各写一份口径）。

    返回 `DataFrame[date(str), code, dps_adj]`，直接喂给
    `backtest_stock.run(..., div_tax=..., tax_rate=...)`。

    ⚠️ 带 `lru_cache`（表只有 ~3.7 万行、只读）—— 所以调用方**就近加载**也不会重复读盘。
    """
    if not os.path.exists(path):
        raise SystemExit(
            "找不到扣税表 {} —— 先跑 `$PY tools/build_div_tax.py`".format(path))
    df = pd.read_parquet(path)
    df["date"] = df.date.astype(str)
    return df


def build(total_path: str, bfq_path: str, div_path: str) -> pd.DataFrame:
    tot = pd.read_parquet(total_path, columns=["date", "code", "close"])
    bfq = pd.read_parquet(bfq_path, columns=["date", "code", "close"])
    for d in (tot, bfq):
        d["date"] = d.date.astype(str)
    tot = tot.rename(columns={"close": "c_tot"})
    bfq = bfq.rename(columns={"close": "c_bfq"})
    m = tot.merge(bfq, on=["date", "code"], how="inner")
    m["g"] = m.c_tot / m.c_bfq

    # 除权日要用**前一日**的 g
    m = m.sort_values(["code", "date"])
    m["g_prev"] = m.groupby("code", sort=False).g.shift(1)
    gmap = m[["date", "code", "g", "g_prev"]]

    # 展开分红事件（D = 每股税前派现；送转不计税）
    ev = load_events(div_path)
    rows = [{"code": c, "date": d, "D": v[0]}
            for c, per in ev.items() for d, v in per.items() if v[0] != 0]
    if not rows:
        raise RuntimeError("分红事件为空 —— 检查 --dividends 路径")
    edf = pd.DataFrame(rows)
    edf["date"] = edf.date.astype(str)

    out = edf.merge(gmap, on=["date", "code"], how="left")
    miss = int(out.g_prev.isna().sum())
    # g_prev 缺失 = 该除权日恰是该股在本数据里的**第一天**（没有前一日可比）
    # → 退回**当日的 g**。g 随 t 单调升到 1，用当日 g 比用 1.0 更接近真实比例；
    #   两者都略偏保守（多扣一点点税），且这些行绝大多在回测区间之前。
    out["g_prev"] = out.g_prev.fillna(out.g).fillna(1.0)
    out["dps_adj"] = out.D * out.g_prev
    out = out[["date", "code", "dps_adj"]].sort_values(["date", "code"])
    return out.reset_index(drop=True), miss


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="构造引擎内扣税用的每股派现表")
    ap.add_argument("--total", default="data/stockbars/bars_total.parquet")
    ap.add_argument("--bfq", default="data/stockbars/bars_bfq.parquet")
    ap.add_argument("--dividends", default="data/dividends/bonus_all.parquet")
    ap.add_argument("--out", default="data/stockbars/div_tax_table.parquet")
    args = ap.parse_args(argv)

    print("=" * 92)
    print("  构造 div_tax_table（引擎内扣红利税用）")
    print("=" * 92)
    print("  total = {}".format(args.total))
    print("  bfq   = {}".format(args.bfq))
    out, n_missing = build(args.total, args.bfq, args.dividends)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    tmp = args.out + ".tmp"
    out.to_parquet(tmp, index=False)          # ⚠️ 原子写（项目铁律：大文件缓存必须原子写）
    os.replace(tmp, args.out)

    print("\n  除权日行数 : {:,}".format(len(out)))
    print("  涉及股票   : {:,}".format(out.code.nunique()))
    print("  日期范围   : {} ~ {}".format(out.date.min(), out.date.max()))
    print("  dps_adj    : 中位 {:.4f}  95% {:.4f}  最大 {:.4f}".format(
        out.dps_adj.median(), out.dps_adj.quantile(0.95), out.dps_adj.max()))
    print("  g_prev 缺失（该股数据首日即除权）: {:,}".format(n_missing))
    print("\n  已写出 {}".format(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
