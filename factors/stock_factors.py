"""个股横截面因子：补充 registry 里缺失的常用 A 股因子。

约定
----
所有因子都**统一成「值越大越看好」**（低波动/反转/低换手等已内置负号），
这样策略里用 `weighted_score(factors, date, [(name, 1, w), ...])` 即可，
direction 一律为 +1。

因子方向依据（A 股常见实证）
--------------------------
- **短期反转**：过去 5 日涨幅高的，下周偏弱 → `rev5 = −ret5`
- **中期动量**：过去 60/120 日涨幅高的偏强（与短期反转共存，是不同周期效应）
- **低波动溢价**：波动率低的偏强 → `vol20 = −std20`
- **MAX 效应**：近 20 日最大单日涨幅高的偏弱 → `max20 = −max(ret)`
- **换手/流动性**：换手高的偏弱 → `turn20 = −(vol / MA20(vol))`
- **Amihud 非流动性**：非流动性高的偏强（流动性溢价）→ `illiq = −|ret|/amount`
- **偏度**：右偏（彩票型）偏弱 → `skew20 = −skew`

用法
----
    from factors.stock_factors import register_stock_factors
    register_stock_factors()      # 注册进 FACTOR_FUNCS
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .registry import register_factor


def _rev(px: dict, window: int = 5):
    c = px["close"]
    return -(c / c.shift(window) - 1)


def _vol(px: dict, window: int = 20):
    r = px["close"].pct_change()
    return -r.rolling(window).std()


def _max_ret(px: dict, window: int = 20):
    r = px["close"].pct_change()
    return -r.rolling(window).max()


def _skew(px: dict, window: int = 20):
    r = px["close"].pct_change()
    return -r.rolling(window).skew()


def _turn(px: dict, window: int = 20):
    v = px["volume"]
    return -(v / v.rolling(window).mean())


def _illiq(px: dict, window: int = 20):
    """Amihud 非流动性（用 close×volume 近似成交额）。"""
    r = px["close"].pct_change().abs()
    amt = (px["close"] * px["volume"]).replace(0, np.nan)
    return -(r / amt).rolling(window).mean()


def _amp(px: dict, window: int = 20):
    """振幅：近 window 日平均 (high−low)/close（高振幅偏弱）。"""
    amp = (px["high"] - px["low"]) / px["close"].replace(0, np.nan)
    return -amp.rolling(window).mean()


def _pv_corr(px: dict, window: int = 20):
    """量价相关：近 window 日收益与成交量变化的相关系数（过高偏投机）。"""
    r = px["close"].pct_change()
    dv = px["volume"].pct_change()
    return -r.rolling(window).corr(dv)


STOCK_FACTORS = {
    "rev5": lambda px: _rev(px, 5),
    "rev10": lambda px: _rev(px, 10),
    "vol20": lambda px: _vol(px, 20),
    "vol60": lambda px: _vol(px, 60),
    "max20": lambda px: _max_ret(px, 20),
    "skew20": lambda px: _skew(px, 20),
    "turn20": lambda px: _turn(px, 20),
    "turn60": lambda px: _turn(px, 60),
    "illiq20": lambda px: _illiq(px, 20),
    "amp20": lambda px: _amp(px, 20),
    "pv_corr20": lambda px: _pv_corr(px, 20),
}


def register_stock_factors() -> list:
    for name, fn in STOCK_FACTORS.items():
        register_factor(name, fn)
    return list(STOCK_FACTORS.keys())
