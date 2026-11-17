"""Causal market timing shared by daily trading and the backtest.

All inputs are signal prices. Missing, stale, or insufficient history returns
scale_off for an enabled rule. RSRS needs N + max(2*N, 20) aligned bars.
"""
import numpy as np
import pandas as pd


def _series(data, column=None):
    if isinstance(data, pd.DataFrame):
        if data.empty or not len(data.columns):
            return None
        if column is not None and column not in data.columns:
            return None
        return data[column if column is not None else data.columns[0]]
    return data if isinstance(data, pd.Series) else None


def timing_scale(timing, date, close, high=None, low=None, benchmark=None,
                 window=20, scale_off=.3) -> float:
    """Return a 0..1 exposure multiplier using only observations through date.

    benchmark accepts a Series or a column name; absent benchmark uses the first
    close column in both entry points. MA/absolute-momentum risk-off returns
    scale_off; BIAS bands retain 0/.3/.5/1 at -8%/-5%/0 boundaries.
    """
    if timing in (None, '', 'none'):
        return 1.
    if timing not in ('ma20', 'abs_mom', 'bias', 'rsrs'):
        raise ValueError('Unsupported timing rule: {!r}'.format(timing))
    if int(window) < 1 or not np.isfinite(scale_off) or not 0 <= scale_off <= 1:
        raise ValueError('window must be positive and scale_off must be in [0, 1]')
    window = int(window)
    column = benchmark if isinstance(benchmark, str) else None
    bc = _series(benchmark) if benchmark is not None and column is None else _series(close, column)
    if bc is None or date not in bc.index:
        return float(scale_off)
    bc = pd.to_numeric(bc.loc[:date], errors='coerce')
    needed = window + 1 if timing == 'abs_mom' else window
    if len(bc) < needed:
        return float(scale_off)
    recent = bc.iloc[-needed:]
    if not np.isfinite(recent).all() or not (recent > 0).all():
        return float(scale_off)
    if timing == 'rsrs':
        column = column if column is not None else bc.name
        bh, bl = _series(high, column), _series(low, column)
        if bh is None or bl is None:
            return float(scale_off)
        n = max(5, window)
        m = max(n * 2, 20)
        paired = pd.concat([bh.loc[:date], bl.loc[:date]], axis=1).reindex(bc.index).tail(n + m)
        if len(paired) < n + m or not np.isfinite(paired.to_numpy(dtype=float)).all():
            return float(scale_off)
        hi, lo = paired.iloc[:, 0].to_numpy(), paired.iloc[:, 1].to_numpy()
        beta = []
        for i in range(n, len(paired)):
            x, y = lo[i - n:i], hi[i - n:i]
            variance = float(np.var(x))
            if variance <= 0:
                return float(scale_off)
            beta.append(float(np.mean((x - x.mean()) * (y - y.mean())) / variance))
        sd = float(np.std(beta, ddof=1))
        if not np.isfinite(sd) or sd <= 1e-12:
            return float(scale_off)
        z = (beta[-1] - float(np.mean(beta))) / sd
        return 1. if z > 0 else float(scale_off)
    if timing == 'abs_mom':
        return 1. if recent.iloc[-1] / recent.iloc[0] - 1 > 0 else float(scale_off)
    mean = float(recent.mean())
    if timing == 'ma20':
        return 1. if float(recent.iloc[-1]) > mean else float(scale_off)
    bias = float(recent.iloc[-1]) / mean - 1
    if bias <= -.08:
        return 0.
    if bias <= -.05:
        return .3
    return .5 if bias <= 0 else 1.
