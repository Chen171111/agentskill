"""对照：ETF 份额折算**不复权** vs **读取时自动复权**，对实盘配置的影响。

为什么需要这个脚本（2026-09-16 的真实事故）
--------------------------------------------
09-13 修好的 4 处份额折算，**被 09-16 的每日刷新静默回退了** ——
`DataStore.refresh()` 会用 akshare 的**原始未复权全量历史**覆盖 `data/stocks/*.csv`。

**本脚本是干净的对照实验**：同一目录、同一份文件、同一套引擎与参数，
**只切换 `DataStore.repair_splits`**（True/False），因此差异 100% 来自复权本身。

⚠️ 不要用「`data/stocks` vs `data/stocks_repaired`」做对照 ——
   后者是 09-13 的快照，**末日停在 20260910**（比实盘旧 3 个交易日），
   会把「数据版本差异」混进「复权差异」里（实测会多出约 0.14pp 的假差异）。

用法
----
    $PY tools/cmp_split_impact.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402


def one(repair: bool, label: str):
    """同一目录下，只切换是否复权。"""
    from dataprovider import store as store_mod
    orig_init = store_mod.DataStore.__init__

    def patched(self, force_download=False, repair_splits=True):
        orig_init(self, force_download=force_download, repair_splits=repair)

    store_mod.DataStore.__init__ = patched
    try:
        from pipeline import run_backtest
        codes = list(config.RECOMMENDED_POOLS["ETF全球"])
        out = run_backtest(codes, strategy="etf_rotation", start="20190101",
                           end="20260911", topk=5, rebalance=5)
    finally:
        store_mod.DataStore.__init__ = orig_init
    m = out["metrics"]
    print(f"  {label:<24} 年化 {m['年化收益']:>7.2f}%  夏普 {m['夏普比率']:>5.2f}  "
          f"回撤 {m['最大回撤']:>8.2f}%  累计 {m['累计收益']:>8.2f}%", flush=True)
    return m


def main() -> int:
    print("=" * 100)
    print("  ETF 份额折算复权对照（实盘配置 etf_rotation / ETF全球 / topk=5 / 5日）")
    print("=" * 100)
    print(f"  数据目录 data/stocks ｜ 资金口径 INIT_CASH = {config.INIT_CASH:,.0f}")
    print("  ⚠️ 两行用的是**同一份文件**，只切换 DataStore.repair_splits\n")
    a = one(False, "不复权（旧行为）")
    b = one(True, "读取时自动复权（现行为）")
    print()
    print(f"  → 不复权把年化压低 {b['年化收益'] - a['年化收益']:+.2f}pp、"
          f"夏普压低 {b['夏普比率'] - a['夏普比率']:+.2f}、"
          f"回撤放大 {a['最大回撤'] - b['最大回撤']:+.2f}pp")
    return 0


if __name__ == "__main__":
    sys.exit(main())
