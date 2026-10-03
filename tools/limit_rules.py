"""A 股涨跌停幅度 —— **回测/因子唯一来源**（2026-10-03 建立，验收 P1-1/P2-10 配套）。

为什么要单独成一个文件
----------------------
涨跌停分档原先分散在两处且互不一致：

| 位置 | 覆盖 | 缺陷 |
|---|---|---|
| `backtest/engine.py::_limit_pct` | 个股 + ETF（2026-10-02 补） | 与因子层的涨停判据口径不同源 |
| `factors/registry.py::_zt_daily` | 个股（硬编码 thr=0.098） | 双创（20%）漏判涨停、ST（5%）误判 |

「同一判据多处实现 = 迟早分叉」是本项目反复付过学费的病
（复权阈值 4 处实现 3 处未回灌、成本万3/万5 分叉，均见 docs 复盘）。
故收敛到本模块，`tools/selftest.py` 断言分档字面量不得再散落。

制度依据
--------
| 品种 | 幅度 | 判定 |
|---|---|---|
| 主板个股 / 主板 ETF / 跨境 / 商品 / 债券 ETF | ±10% | 默认档 |
| 科创板个股（688） / 科创板 ETF（588/589 前缀） | ±20% | 前缀 |
| 创业板个股（300/301） | ±20% | 前缀 |
| 创业板/双创 ETF（深市） | ±20% | **显式名单**（深交所按基金名单公布，代码前缀无法区分） |
| 北交所个股（43/83/87/88） | ±30% | 前缀 |
| ST / *ST | ±5% | **未建模**（需 point-in-time ST 快照，当前无数据源；
  quant2 已同款登记为已知残留） |

⚠️ ETF 双创名单是**维护制**：扩池纳入新的创业板/科创板 ETF 必须同步补录
`ETF_LIMIT_20`，否则其 20% 涨停日会被按 10% 档漏判（偏乐观）。
依据：`docs/参考_A股ETF涨跌幅限制.md`（交易所规则一手梳理）。

用法
----
    from tools.limit_rules import limit_of
    lim = limit_of("159915.SZ")     # 0.20
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataprovider.store import classify_code  # noqa: E402

# 双创 ETF 显式名单（深市 159 开头无法从前缀区分，见模块 docstring）
ETF_LIMIT_20 = frozenset({
    "159915",   # 创业板ETF（易方达）
    "159780",   # 双创ETF（华夏）
    "159781",   # 双创ETF（招商）
})
ETF_20_PREFIX = ("588", "589")     # 沪市科创板 ETF


def limit_of(code: str) -> float:
    """单个标的的涨跌停幅度（比例，0.10 = ±10%）。

    个股按板块分档；ETF 默认 10%、双创 20%（前缀 + 显式名单）；
    指数等不可交易标的双保险返回 None（调用方应已用 validate_tradeable 拦截）。
    """
    kind = classify_code(code)
    base = code.upper().split(".")[0]
    if kind == "etf":
        if base.startswith(ETF_20_PREFIX) or base in ETF_LIMIT_20:
            return 0.20
        return 0.10
    if kind != "stock":
        return None
    if base.startswith("68"):                 # 科创板
        return 0.20
    if base.startswith(("300", "301")):       # 创业板
        return 0.20
    if code.upper().endswith(".BJ"):          # 北交所（按市场后缀，避免前缀误伤）
        return 0.30
    return 0.10                               # 主板


def limit_price_round(code: str) -> int:
    """涨跌停价的取整精度（小数位）：股票 0.01 元、基金 0.001 元。

    依据交易所规则「计算结果四舍五入至申报价格最小变动单位」；
    ETF 若按 0.01 取整会把门槛压低 → 9.9% 涨幅的合法开盘被误拦。
    """
    return 3 if classify_code(code) == "etf" else 2
