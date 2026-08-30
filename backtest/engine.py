"""回测引擎：逐日推进，调用策略产出目标权重并再平衡（含大盘择时 + 组合层风控）。"""
from typing import Optional

import numpy as np
import pandas as pd

import config
from .account import BacktestAccount

_TRADING_DAYS = getattr(config, "TRADING_DAYS_PER_YEAR", 244)

# 回撤熔断参数（带滞回的极端保险，只对深回撤反应，避免浅阈值反复"打脸"）
# 降仓档位：(回撤幅度阈值 → 仓位系数)，从深到浅排列
_CB_LEVELS = [(0.25, 0.0), (0.20, 0.40), (0.15, 0.65)]
_CB_TRIGGER = 0.15    # 进入熔断的回撤幅度（≥15% 才启动）
_CB_RECOVER = 0.10    # 退出熔断的回撤幅度（≤10% 才恢复，滞回带 5%）


class BacktestResult:
    def __init__(self, equity: pd.DataFrame, holdings: dict,
                 benchmark: Optional[pd.Series] = None):
        self.equity = equity
        self.holdings = holdings
        self.benchmark = benchmark


class BacktestEngine:
    def __init__(self, panel, factors, strategy, init_cash=None, benchmark=None,
                 timing=None, timing_window=20, timing_scale_off=0.3,
                 benchmark_high=None, benchmark_low=None,
                 dd_circuit=False, vol_target=None, stability_min_overlap=None):
        self.panel = panel
        self.factors = factors
        self.strategy = strategy
        self.acc = BacktestAccount(init_cash=init_cash)
        self.benchmark = benchmark
        self.timing = timing
        self.timing_window = timing_window
        self.timing_scale_off = timing_scale_off
        # 组合层风控
        self.dd_circuit = bool(dd_circuit)          # 回撤熔断开关
        self.vol_target = float(vol_target) if vol_target else None  # 目标年化波动率
        # 信号稳定性过滤：本/上期 TopK 重叠度 < 阈值 → 空仓（BigQuant 信号稳定性思想）
        self.stability_min_overlap = (float(stability_min_overlap)
                                      if stability_min_overlap else None)
        self._last_codes = None                     # 上一调仓期的标的集合
        self._nav_history = []                      # 组合净值历史（用于组合层风控）
        self._cb_active = False                     # 回撤熔断滞回状态（是否处于熔断态）
        self._timing_ma = None
        self._timing_mom = None
        self._timing_rsrs = None
        self._timing_bias = None
        if timing and benchmark is not None and len(benchmark) > timing_window:
            bc = benchmark.dropna()
            if timing == "ma20":
                self._timing_ma = bc.rolling(timing_window).mean()
            elif timing == "abs_mom":
                self._timing_mom = bc / bc.shift(timing_window) - 1
            elif timing == "rsrs":
                self._timing_rsrs = self._build_rsrs(
                    benchmark, benchmark_high, benchmark_low, timing_window)
            elif timing == "bias":
                # BIAS 温度计：大盘 20 日乖离率，用于多档仓位调度
                ma = bc.rolling(timing_window).mean()
                self._timing_bias = (bc - ma) / ma

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
        if (self._timing_ma is None and self._timing_mom is None
                and self._timing_rsrs is None and self._timing_bias is None):
            return 1.0
        if self._timing_rsrs is not None:
            z = self._timing_rsrs.get(date)
            if z is None or z != z:
                return 1.0
            return 1.0 if z > 0 else self.timing_scale_off
        if self._timing_bias is not None:
            # BIAS 温度计多档仓位（聚宽"坦克300"思想）：
            # 大盘极端深跌→空仓；均线下方→减仓；均线上方→满仓
            b = self._timing_bias.get(date)
            if b is None or b != b:
                return 1.0
            if b <= -0.08:
                return 0.0
            if b <= -0.05:
                return 0.3
            if b <= 0:
                return 0.5
            return 1.0
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

    def _circuit_scale(self, dd_depth: float) -> float:
        """回撤熔断定仓（带滞回状态机）。

        进入熔断：回撤 ≥ _CB_TRIGGER（15%）→ 按深度定档降仓；
        退出熔断：回撤 ≤ _CB_RECOVER（10%）→ 恢复满仓（滞回带避免震荡）。
        """
        if self._cb_active:
            # 熔断态：回撤修复到恢复线以下才退出
            if dd_depth <= _CB_RECOVER:
                self._cb_active = False
                return 1.0
        else:
            # 非熔断态：回撤触及触发线才进入
            if dd_depth >= _CB_TRIGGER:
                self._cb_active = True
            else:
                return 1.0

        # 熔断态：按深度定档（从深到浅）
        for trig, lvl in _CB_LEVELS:
            if dd_depth >= trig:
                return lvl
        return 1.0

    def _portfolio_risk_scale(self) -> float:
        """组合层风控：回撤熔断 + 波动率目标仓位，返回 0~1 的仓位系数。"""
        scale = 1.0
        nav = self._nav_history
        if len(nav) < 2:
            return scale

        # 1) 回撤熔断（带滞回的极端保险）
        if self.dd_circuit:
            peak = max(nav)
            dd_depth = 1.0 - nav[-1] / peak   # 回撤幅度(正数)
            scale = self._circuit_scale(dd_depth)

        # 2) 波动率目标仓位（组合实际波动 > 目标 → 等比降仓）
        if self.vol_target and len(nav) >= 20:
            rets = pd.Series(nav).pct_change().dropna().tail(20)
            realized = rets.std() * np.sqrt(_TRADING_DAYS)
            if realized > 0.001:
                scale = min(scale, self.vol_target / realized)

        return float(np.clip(scale, 0.0, 1.0))

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
            self._nav_history.append(self.acc.total())

            weights = self.strategy.generate_weights(date, self.factors, self.panel)
            if weights is not None and weights:
                # 信号稳定性过滤（BigQuant 思想）：本/上期标的集合重叠度过低 → 空仓
                if self.stability_min_overlap is not None:
                    cur_codes = set(weights.keys())
                    if self._last_codes is not None:
                        overlap = len(cur_codes & self._last_codes) / max(len(cur_codes), 1)
                        if overlap < self.stability_min_overlap:
                            # 信号不稳，本期空仓（清掉已有持仓，不建新仓）
                            self.acc.rebalance({})
                            self._last_codes = cur_codes
                            holdings[date] = self.acc.holding()
                            continue
                    self._last_codes = cur_codes
                # 大盘择时
                scale = self._timing_scale(date)
                # 组合层风控（回撤熔断 + 波动率目标）
                scale *= self._portfolio_risk_scale()
                if scale < 1.0:
                    weights = {c: w * scale for c, w in weights.items()}
                self.acc.rebalance(weights)
            holdings[date] = self.acc.holding()

        return BacktestResult(self.acc.results(), holdings, bench_nav)