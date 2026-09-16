"""个股本地行情仓库：与 `DataStore` 同接口，直接读本地 Parquet。

为什么需要它
------------
`dataprovider/store.py` 的 `DataStore` 走 **akshare** 下载，而本机环境
（2026-09 实测）**akshare 不可用**、且东财/网易等接口被沙箱拦截。
个股长历史改由 `tools/fetch_stock_history.py` 从腾讯 `proxy.finance.qq.com`
一次性抓成本地 Parquet，本模块负责读取，使 `build_panel()` 无需改动即可用于个股。

接口对齐
--------
`read(code, start, end)` 返回**按 `YYYYMMDD` 字符串索引**的 DataFrame，列含
`open / high / low / close / volume / rate`，与 `DataStore.read()` 一致。
另外提供 `ensure()` 空实现（数据已在本地）与 `codes()`。

股票池过滤
----------
`universe_mask()` 给出**逐日可得**的过滤条件（避免前视）：
- **上市满 `min_days` 个交易日**（剔除次新，用数据自身长度判定）
- **20 日均成交额 ≥ `min_amount`**（点内可得，剔除低流动性）
- **收盘价 ≥ `min_price`**（剔除仙股）
- **非停牌**（当日成交量 > 0）
- 可选：剔除当前 ST/退市名单（`exclude_st=True`）
  ⚠️ 用「当前」ST 名单回溯过滤历史含**轻微前视**，但属业界常规做法，默认开启并在此标注。

用法
----
    from dataprovider.localstore import LocalBarsStore
    store = LocalBarsStore("data/stockbars/bars.parquet",
                           "data/stockbars/universe.csv")
    panel = build_panel(store, store.codes()[:200], start=..., end=...)
"""
from __future__ import annotations

import os
from typing import Optional

import pandas as pd


class LocalBarsStore:
    def __init__(self, bars_path: str, universe_path: Optional[str] = None,
                 lazy: bool = True):
        if not os.path.exists(bars_path):
            raise FileNotFoundError(
                "{} 不存在，请先运行 tools/fetch_stock_history.py bars".format(bars_path))
        self.bars_path = bars_path
        df = pd.read_parquet(bars_path)
        df["date"] = df["date"].astype(str).str.replace("-", "", regex=False)
        df = df.drop_duplicates(subset=["code", "date"]).sort_values(["code", "date"])
        self._df = df
        self._by_code: dict[str, pd.DataFrame] = {}
        self._lazy = lazy
        self._grouped = None if not lazy else df.groupby("code", sort=False)
        if not lazy:
            self._by_code = {c: g for c, g in df.groupby("code", sort=False)}

        self._st: set[str] = set()
        self._name: dict[str, str] = {}
        self._board: dict[str, str] = {}
        if universe_path and os.path.exists(universe_path):
            u = pd.read_csv(universe_path, dtype=str)
            self._name = dict(zip(u.code, u.name))
            self._board = dict(zip(u.code, u.board))
            self._st = set(u.loc[u.is_st.astype(str).str.lower().isin(
                ["true", "1"]), "code"])

    # ------------------------------------------------------------ 基础
    def codes(self) -> list:
        if self._lazy:
            return sorted(self._df.code.unique().tolist())
        return sorted(self._by_code.keys())

    def name_of(self, code: str) -> str:
        return self._name.get(code, "")

    def board_of(self, code: str) -> str:
        return self._board.get(code, "")

    def st_codes(self) -> set:
        return set(self._st)

    def ensure(self, codes) -> list:
        """接口对齐用：本地数据无需下载，返回缺失代码列表。"""
        have = set(self.codes())
        return [c for c in codes if c not in have]

    # ------------------------------------------------------------ 读取
    def _frame(self, code: str) -> pd.DataFrame:
        if code in self._by_code:
            return self._by_code[code]
        if self._lazy:
            try:
                g = self._grouped.get_group(code)
            except KeyError:
                raise FileNotFoundError("本地无 {} 的行情".format(code))
            self._by_code[code] = g
            return g
        raise FileNotFoundError("本地无 {} 的行情".format(code))

    def read(self, code: str, start=None, end=None) -> pd.DataFrame:
        g = self._frame(code)
        df = g.set_index("date").copy()
        df.index = df.index.astype(str)
        df = df.sort_index()
        df["rate"] = df["close"].pct_change()
        if start:
            df = df[df.index >= str(start)]
        if end:
            df = df[df.index <= str(end)]
        return df

    # ------------------------------------------------------------ 股票池
    def universe_mask(self, panel_dates, min_days: int = 120,
                      min_amount: float = 3e7, min_price: float = 2.0,
                      amount_window: int = 20, exclude_st: bool = True):
        """返回布尔 DataFrame（date × code），True = 当日可入选。

        `min_amount` 单位 = 元（用 close × volume 近似；腾讯 volume 单位是**手**，
        故成交额 ≈ close × volume × 100）。
        """
        df = self._df
        px = df.pivot(index="date", columns="code", values="close")
        vol = df.pivot(index="date", columns="code", values="volume")
        amt = (px * vol * 100.0).sort_index()
        amt_ma = amt.rolling(amount_window, min_periods=max(3, amount_window // 3)).mean()

        # 上市天数：每个标的已出现的交易日累计数
        listed = px.notna().cumsum()

        mask = (listed >= min_days) & (amt_ma >= min_amount) & (px >= min_price) & (vol > 0)
        if exclude_st and self._st:
            drop = [c for c in mask.columns if c in self._st]
            if drop:
                mask[drop] = False
        mask = mask.reindex(index=panel_dates)
        return mask.fillna(False)
