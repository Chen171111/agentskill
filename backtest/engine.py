"""回测引擎：逐日推进，调用策略产出目标权重并再平衡（含大盘择时）。"""
from typing import Optional

import numpy as np
import pandas as pd

from .account import BacktestAccount


class BacktestResult:
    def __init__(self, equity: pd.DataFrame, holdings: dict,
                 benchmark: Optional[pd.Series] = None):
        self.equity = equity
        self.holdings = holdings
        self.benchmark = benchmark


class BacktestEngine:
    def __init__(self, panel, factors, strategy, init_cash=None, benchmark=None,
                 timing=None, timing_window=20, timing_scale_off=0.3,
                 benchmark_high=None, benchmark_low=None):
        self.panel = panel
        self.factors = factors
        self.strategy = strategy
        self.acc = BacktestAccount(init_cash=init_cash)
        self.benchmark = benchmark
        self.timing = timing
        self.timing_window = timing_window
        self.timing_scale_off = timing_scale_off
        self._timing_ma = None
        self._timing_mom = None
        self._timing_rsrs = None
        if timing and benchmark is not None and len(benchmark) > timing_window:
            bc = benchmark.dropna()
            if timing == "ma20":
                self._timing_ma = bc.rolling(timing_window).mean()
            elif timing == "abs_mom":
                self._timing_mom = bc / bc.shift(timing_window) - 1
            elif timing == "rsrs":
                self._timing_rsrs = self._build_rsrs(
                    benchmark, benchmark_high, benchmark_low, timing_window)

    def _build_rsrs(self, close, high, low, window):
        if high is None or low is None:
            return None
        bh, bl = high.dropna(), low.dropna()
        if len(bh) < window + 2:
            return None
        hi, lo = bh.values, bl.values
        N = max(5, min(window, len(bh) - 2))
        beta = [np.nan] * len(bh)
        for i in range(N, len(bh)):
            beta[i] = np.polyfit(lo[i - N:i], hi[i - N:i], 1)[0]
        alpha = pd.Series(beta, index=bh.index)
        M = max(N * 2, 20)
        return (alpha - alpha.rolling(M).mean()) / alpha.rolling(M).std()

    def _timing_scale(self, date) -> float:
        if self._timing_ma is None and self._timing_mom is None and self._timing_rsrs is None:
            return 1.0
        if self._timing_rsrs is not None:
            z = self._timing_rsrs.get(date)
            if z is None or z != z:
                return 1.0
            return 1.0 if z > 0 else self.timing_scale_off
        if self._timing_ma is not None:
            c = self._timing_ma.get(date)
            if c is None or c != c:
                return 1.0
            return 1.0 if self.benchmark.get(date, 0) > c else self.timing_scale_off
        if self._timing_mom is not None:
            c = self._timing_mom.get(date)
            if c is None or c != c:
                return 1.0
            return 1.0 if c > 0 else self.timing_scale_off
        return 1.0

    def run(self) -> BacktestResult:
        dates = self.panel.dates
        close = self.panel.get("close")
        rate = self.panel.get("rate")
        bench_nav = None
        if self.benchmark is not None:
            bc = self.benchmark
            b0 = bc.loc[dates].dropna()
            bench_nav = b0 / b0.iloc[0]

        holdings = {}
        for date in dates:
            if date in rate.index:
                rates = {c: (r if r == r else 0.0) for c, r in rate.loc[date].items()}
            else:
                rates = {}
            self.acc.update(date, rates)

            weights = self.strategy.generate_weights(date, self.factors, self.panel)
            if weights is not None and weights:
                scale = self._timing_scale(date)
                if scale < 1.0:
                    weights = {c: w * scale for c, w in weights.items()}
                self.acc.rebalance(weights)
            holdings[date] = self.acc.holding()

        return BacktestResult(self.acc.results(), holdings, bench_nav)