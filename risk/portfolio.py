"""组合级风控：回撤熔断 + 波动率目标（回测与实盘共享同一套逻辑）。

解决「回撤熔断/波动率目标只存在于回测引擎、实盘从未接线」的断层：
BacktestEngine 与实盘 RiskManager 都复用本类，保证两套风控口径一致。
"""
import numpy as np
import pandas as pd

import config

_TRADING_DAYS = getattr(config, "TRADING_DAYS_PER_YEAR", 244)

# 回撤熔断参数（带滞回的极端保险，只对深回撤反应）
_CB_LEVELS = [(0.25, 0.0), (0.20, 0.40), (0.15, 0.65)]
_CB_TRIGGER = 0.15    # 进入熔断的回撤幅度（≥15% 才启动）
_CB_RECOVER = 0.10    # 退出熔断的回撤幅度（≤10% 才恢复，滞回带 5%）


class PortfolioRisk:
    """组合级风控：输入组合净值历史，输出 0~1 仓位系数。"""

    def __init__(self, dd_circuit: bool = True, vol_target: float = None):
        self.dd_circuit = bool(dd_circuit)
        self.vol_target = float(vol_target) if vol_target else None
        self._cb_active = False

    def _circuit(self, dd_depth: float) -> float:
        """回撤熔断定仓（带滞回状态机）。

        2026-09-29 修复：滞回带曾完全失效
        ----------------------------------
        原实现把 dd_depth 直接喂给下面的 levels 循环，而 levels 的**最低档就是
        _CB_TRIGGER（15%）** —— active 期间 dd 落回 (10%, 15%) 区间时循环落空、
        返回 1.0，熔断立即解除，「≤10% 才恢复」的滞回带从未生效：回撤在 15%
        边界附近往返时，仓位在 0.65 与 1.0 之间反复开关（whipsaw）。
        修法：active 期间按 `max(dd, _CB_TRIGGER)` 查档 —— 只要还没恢复到
        ≤10%，就至少维持进入熔断时的 0.65 档。
        """
        if self._cb_active:
            if dd_depth <= _CB_RECOVER:
                self._cb_active = False
                return 1.0
            eff = max(dd_depth, _CB_TRIGGER)      # 滞回：未恢复前不低于进入档
        else:
            if dd_depth < _CB_TRIGGER:
                return 1.0
            self._cb_active = True
            eff = dd_depth
        for trig, lvl in _CB_LEVELS:
            if eff >= trig:
                return lvl
        return 1.0

    def _vol_target(self, nav) -> float:
        """波动率目标：实际年化波动 > 目标 → 等比降仓。"""
        if len(nav) < 20:
            return 1.0
        rets = pd.Series(nav).pct_change().dropna().tail(20)
        realized = rets.std() * np.sqrt(_TRADING_DAYS)
        # isfinite：nav 含 NaN 时 std=nan，`nan <= 0.001` 为 False 会漏过去，
        # 再把 nan 传给 np.clip 污染 scale（2026-09-29 与 _pos_ok 同一类穿透）。
        if not np.isfinite(realized) or realized <= 0.001:
            return 1.0
        return float(np.clip(self.vol_target / realized, 0.0, 1.0))

    def scale(self, nav_history) -> float:
        """nav_history: 组合净值序列（含当前值，list[float]），返回 0~1 仓位系数。"""
        if len(nav_history) < 2:
            return 1.0
        scale = 1.0
        if self.dd_circuit:
            peak = max(nav_history)
            dd_depth = 1.0 - nav_history[-1] / peak
            scale = self._circuit(dd_depth)
        if self.vol_target:
            scale = min(scale, self._vol_target(nav_history))
        return float(np.clip(scale, 0.0, 1.0))