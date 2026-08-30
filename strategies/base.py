"""策略基类与横截面排名工具。"""
import abc

import numpy as np
import pandas as pd


def volatility_weighted(codes, panel, date, lookback=20):
    """风险平价权重：按各标的近 lookback 日波动率倒数分配权重（抑制高波动标的）。

    返回 {code: weight}，权重和 = 1。
    """
    weights = {}
    rates = panel.get("rate")
    if rates is None:
        return {}
    for c in codes:
        if c not in rates.columns:
            continue
        # 该标的截至 date 的近期收益率序列
        r = rates[c].loc[:date].dropna().tail(lookback)
        vol = float(r.std())
        weights[c] = 1.0 / vol if vol > 1e-9 else 0.0
    total = sum(weights.values())
    if total <= 0:
        return {}
    return {c: w / total for c, w in weights.items()}


class Strategy(abc.ABC):
    """策略抽象基类。

    子类实现 generate_weights(date, factors, panel) -> {code: weight}。
    """

    name = "base"

    def __init__(self, topk: int = 3, rebalance_every: int = 5, max_weight: float = 0.30,
                 max_total: float = 0.90, **kwargs):
        self.topk = topk
        self.rebalance_every = rebalance_every
        self.max_weight = max_weight
        self.max_total = max_total
        self._since = 0

    @abc.abstractmethod
    def generate_weights(self, date, factors, panel):
        raise NotImplementedError

    def _is_rebalance(self) -> bool:
        self._since += 1
        return self._since % self.rebalance_every == 0

    def _snapshot(self, factors, date, factor_name):
        df = factors.get(factor_name)
        if df is None or date not in df.index:
            return None
        return df.loc[date].dropna()


def rank_snapshot(series: pd.Series, ascending: bool = False) -> pd.Series:
    r = series.rank(pct=True)
    return (1 - r) if ascending else r


def weighted_score(factors, date, factor_specs) -> pd.Series:
    score = None
    tw = 0.0
    for fname, direction, w in factor_specs:
        s = factors.get(fname)
        if s is None or date not in s.index:
            continue
        sr = rank_snapshot(s.loc[date].dropna(), ascending=(direction < 0))
        score = sr.mul(w).copy() if score is None else score.add(sr.mul(w), fill_value=0.0)
        tw += w
    if score is None or tw <= 0:
        return pd.Series(dtype=float)
    return score / tw