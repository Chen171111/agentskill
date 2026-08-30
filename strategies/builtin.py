"""内置策略：动量、均值回归、双均线、多因子、连板龙头。"""
import pandas as pd

from .base import Strategy, weighted_score, rank_snapshot, volatility_weighted


class MomentumStrategy(Strategy):
    """动量轮动：动量 + 均线趋势双因子打分取 TopK。"""
    name = "momentum"

    def __init__(self, topk=3, window=20, risk_parity=False, **kw):
        super().__init__(topk=topk, **kw)
        self.window = window
        self.risk_parity = bool(risk_parity)
        self.factor = "momentum{}".format(window) if window != 20 else "momentum20"

    def generate_weights(self, date, factors, panel):
        if not self._is_rebalance():
            return None
        score = weighted_score(factors, date,
                               [(self.factor, 1, 1.0), ("sma_gap", 1, 1.0)])
        if score is None or score.empty:
            return {}
        codes = score.sort_values(ascending=False).head(self.topk).index
        if self.risk_parity and len(codes) > 1:
            # 风险平价：按波动率倒数分配，替代等权
            w = volatility_weighted(list(codes), panel, date)
            return {c: ww * self.max_total for c, ww in w.items()}
        return {c: min(1.0 / max(len(codes), 1) * 0.9, self.max_total) for c in codes}


class MeanReversionStrategy(Strategy):
    """均值回归：乖离率越低越看好。"""
    name = "mean_reversion"

    def __init__(self, topk=3, factor="bias20", **kw):
        super().__init__(topk=topk, **kw)
        self.factor = factor

    def generate_weights(self, date, factors, panel):
        if not self._is_rebalance():
            return None
        s = self._snapshot(factors, date, self.factor)
        if s is None or s.empty:
            return {}
        scores = rank_snapshot(s, ascending=True)
        codes = scores.sort_values(ascending=False).head(self.topk).index
        return {c: min(1.0 / max(len(codes), 1) * 0.9, self.max_total) for c in codes}


class CrossMovingStrategy(Strategy):
    """双均线趋势：sma_gap 越强越看好。"""
    name = "cross_moving"

    def __init__(self, topk=3, factor="sma_gap", **kw):
        super().__init__(topk=topk, **kw)
        self.factor = factor

    def generate_weights(self, date, factors, panel):
        if not self._is_rebalance():
            return None
        s = self._snapshot(factors, date, self.factor)
        if s is None or s.empty:
            return {}
        scores = rank_snapshot(s, ascending=False)
        codes = scores.sort_values(ascending=False).head(self.topk).index
        return {c: min(1.0 / max(len(codes), 1) * 0.9, self.max_total) for c in codes}


class LianbanLeadStrategy(Strategy):
    """连板龙头接力：选连板最高标的，情绪门控制空仓。"""
    name = "lianban_lead"

    def __init__(self, topk=1, min_zt=1, **kw):
        super().__init__(topk=topk, **kw)
        self.min_zt = min_zt

    def generate_weights(self, date, factors, panel):
        if not self._is_rebalance():
            return None
        lb = self._snapshot(factors, date, "lianban")
        zt = self._snapshot(factors, date, "zt_daily")
        if lb is None or zt is None or lb.empty or zt.empty:
            return {}
        zt_cnt = int((zt > 0).sum())
        if zt_cnt < self.min_zt:
            return {}
        codes = lb.sort_values(ascending=False).head(self.topk).index.tolist()
        codes = [c for c in codes if lb.loc[c] > 0]
        if not codes:
            return {}
        w = min(1.0 / len(codes) * 0.9, self.max_total)
        return {c: w for c in codes}


class MultiFactorStrategy(Strategy):
    """多因子打分：加权横截面得分取 TopK。"""
    name = "multifactor"

    DEFAULT_SPECS = [
        ("momentum20", 1, 1.0),
        ("sma_gap", 1, 1.0),
        ("macd_hist", 1, 1.0),
        ("bias20", -1, 0.5),
    ]

    def __init__(self, topk=3, specs=None, **kw):
        super().__init__(topk=topk, **kw)
        self.specs = specs or list(self.DEFAULT_SPECS)

    def generate_weights(self, date, factors, panel):
        if not self._is_rebalance():
            return None
        score = weighted_score(factors, date, self.specs)
        if score is None or score.empty:
            return {}
        codes = score.sort_values(ascending=False).head(self.topk).index
        w = min(1.0 / max(len(codes), 1) * 0.9, self.max_total)
        return {c: w for c in codes}