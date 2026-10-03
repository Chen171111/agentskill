"""因子注册表：内置技术因子，输入单标的序列 dict，输出 Series。"""
from typing import Callable, Dict

import numpy as np
import pandas as pd


def _rsi(px: dict, period=14):
    close = px["close"]
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _macd_hist(px: dict, fast=12, slow=26, signal=9):
    close = px["close"]
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    dif = ema_f - ema_s
    return dif - dif.ewm(span=signal, adjust=False).mean()


def _bias20(px: dict):
    close = px["close"]
    ma = close.rolling(20).mean()
    return (close - ma) / ma


def _sma_gap(px: dict, fast=5, slow=20):
    close = px["close"]
    return (close.rolling(fast).mean() - close.rolling(slow).mean()) / close.rolling(slow).mean()


def _natr(px: dict, period=14):
    h, l, c = px["high"], px["low"], px["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean() / c * 100


def _momentum(px: dict, window=20):
    return px["close"] / px["close"].shift(window) - 1


def _vol_ratio(px: dict, window=5):
    v = px["volume"]
    return v / v.rolling(window).mean()


def _zt_thr(code=None):
    """涨停判据阈值：按标的分档取单一来源（tools/limit_rules），留 0.2pp 容差。

    2026-10-03 验收修复（P2-10）：原硬编码 9.8% 容差阈值只对主板 10% 成立 ——
    双创（20%）漏判涨停、ST（5%）误判。ST ±5% 档仍未建模（无 point-in-time
    ST 快照，quant2 已同款登记为已知残留）。
    """
    if code:
        from tools.limit_rules import limit_of
        lim = limit_of(code)
        if lim is not None:
            return lim - 0.002
    return 0.10 - 0.002   # 主板档 − 容差（code 不可用时的回退）


def _zt_daily(px: dict, thr: float = None):
    if thr is None:
        thr = _zt_thr(px.get("code"))
    return (px["close"].pct_change() >= thr).astype(int)


def _lianban(px: dict, thr: float = None):
    if thr is None:
        thr = _zt_thr(px.get("code"))
    zt = _zt_daily(px, thr)
    run = zt.cumsum() - zt.cumsum().where(zt == 0).ffill().fillna(0)
    return run.where(zt > 0, 0)


FACTOR_FUNCS: Dict[str, Callable] = {
    "rsi": lambda px: _rsi(px),
    "macd_hist": lambda px: _macd_hist(px),
    "bias20": lambda px: _bias20(px),
    "sma_gap": lambda px: _sma_gap(px),
    "natr": lambda px: _natr(px),
    "momentum5": lambda px: _momentum(px, 5),
    "momentum10": lambda px: _momentum(px, 10),
    "momentum20": lambda px: _momentum(px, 20),
    "momentum60": lambda px: _momentum(px, 60),
    "momentum120": lambda px: _momentum(px, 120),
    "momentum250": lambda px: _momentum(px, 250),
    "vol_ratio": lambda px: _vol_ratio(px),
    "zt_daily": lambda px: _zt_daily(px),
    "lianban": lambda px: _lianban(px),
}


def register_factor(name: str, fn: Callable) -> None:
    FACTOR_FUNCS[name] = fn


def list_factors() -> list:
    return list(FACTOR_FUNCS.keys())