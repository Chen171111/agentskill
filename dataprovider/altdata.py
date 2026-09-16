"""精灵大单数据接入层（A 路线：个股资金流 → ETF 映射）。

把 `精灵历史数据` 项目产出的 **ETF 级大单信号** parquet 接入 agentskill 面板，
供策略通过 `panel.get(<字段>)` 直接消费。

信号来源（由 `E:/MyWorkAndProject/精灵历史数据` 项目生成）
--------------------------------------------------------
    metrics/etf_bigorder.parquet
    date, etf, etf_big_ratio, etf_big_net, etf_big_amt, etf_cover,
    mkt_breadth, mkt_ratio, mkt_net, mkt_n

字段语义
--------
| 字段 | 层级 | 含义 |
|---|---|---|
| `big_ratio` | 个券 | 该 ETF 代理篮子当日大单净买比（−1~+1） |
| `big_amt` | 个券 | 篮子当日大单总量（手） |
| `big_cover` | 个券 | 篮子当日有效覆盖个股数 |
| `mkt_breadth` | 市场 | 全市场大单净买为正的个股占比（0~1） |
| `mkt_ratio` | 市场 | 全市场大单净买比均值 |

市场级字段会被**广播到面板所有列**，使策略可统一用 `panel.get()` 读取。

⚠️ 重要限制
-----------
精灵数据是**历史快照，止于 2024-07-31**，无增量更新。因此本模块目前只服务
**历史回测**；要上实盘必须能持续拿到精灵数据的增量。

用法
----
    from dataprovider.altdata import attach_bigorder
    panel = attach_bigorder(panel)                       # 用默认路径
    panel = attach_bigorder(panel, "/path/to/etf_bigorder.parquet")
"""
from __future__ import annotations

import os
from typing import Dict, Optional

import pandas as pd

# 默认信号路径：优先环境变量，其次按约定位置查找
_ENV_KEY = "JINGLING_ETF_SIGNAL"
_FALLBACKS = [
    r"E:\MyWorkAndProject\精灵历史数据\metrics\etf_bigorder.parquet",
    r"E:\MyWorkAndProject\精灵历史数据\metrics\daily_bigorder.parquet",
]

# 个券级字段 -> 面板字段名
PER_ETF_FIELDS = {
    "etf_big_ratio": "big_ratio",
    "etf_big_net": "big_net",
    "etf_big_amt": "big_amt",
    "etf_cover": "big_cover",
}
# 市场级字段 -> 面板字段名（广播到所有列）
MARKET_FIELDS = {
    "mkt_breadth": "mkt_breadth",
    "mkt_ratio": "mkt_ratio",
    "mkt_net": "mkt_net",
}

_CACHE: Dict[str, pd.DataFrame] = {}


def default_signal_path() -> Optional[str]:
    p = os.environ.get(_ENV_KEY)
    if p and os.path.exists(p):
        return p
    for p in _FALLBACKS:
        if os.path.exists(p):
            return p
    return None


def load_signal(path: Optional[str] = None) -> pd.DataFrame:
    """读取 ETF 级大单信号（带进程内缓存）。"""
    path = path or default_signal_path()
    if not path:
        raise FileNotFoundError(
            "未找到大单信号 parquet。请设置环境变量 {} 或传入 path。".format(_ENV_KEY))
    if path not in _CACHE:
        sig = pd.read_parquet(path)
        sig["date"] = sig["date"].astype(str)
        _CACHE[path] = sig
    return _CACHE[path]


def attach_bigorder(panel, path: Optional[str] = None, fields=None):
    """把大单信号字段注入面板，返回**新的** Panel。

    - 个券级字段按 `etf` 透视成 date × etf；面板未持有的 ETF 列会被丢弃。
    - 市场级字段广播到面板全部列，便于统一读取。
    - 只保留面板日期范围内的信号（多出的日期直接裁掉）。
    """
    from .panel import Panel

    sig = load_signal(path)
    # want 装的是**目标字段名**（如 "big_ratio"），与下面的 dst 比较
    want = (set(fields) if fields
            else set(PER_ETF_FIELDS.values()) | set(MARKET_FIELDS.values()))

    close = panel.get("close")
    if close is None:
        raise ValueError("面板缺少 close 字段，无法对齐日期")
    dates = list(close.index)
    cols = list(panel.codes)

    tables = dict(panel.fields)
    for src, dst in {**PER_ETF_FIELDS, **MARKET_FIELDS}.items():
        if dst not in want or src not in sig.columns:
            continue
        t = sig.pivot_table(index="date", columns="etf", values=src, aggfunc="last")
        t = t.reindex(index=dates)
        if dst in MARKET_FIELDS.values():
            # 市场级：取任一列（各 ETF 行相同）后广播
            mkt = t.bfill(axis=1).iloc[:, 0] if t.shape[1] else pd.Series(index=dates)
            t = pd.DataFrame({c: mkt for c in cols}, index=dates)
        else:
            t = t.reindex(columns=cols)
        tables[dst] = t

    return Panel(tables, panel.codes, panel.categories)
