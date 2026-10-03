"""风控层：仓位限制 + 个股止损/止盈/回撤止盈。

在实盘/模拟盘场景，风控器基于持仓成本价，对策略产出的目标权重做二次修正：
- 强制平掉触发止损/止盈的持仓；
- 限制单标的权重上限与组合总仓位上限。
"""
import math

import config
from .portfolio import PortfolioRisk


class RiskManager:
    def __init__(self, risk: dict = None, dd_circuit: bool = None, vol_target: float = None):
        risk = risk or config.RISK
        self.max_position_weight = risk.get("max_position_weight", 0.30)
        self.max_total_weight = risk.get("max_total_weight", 0.95)
        self.stop_loss = risk.get("stop_loss", -0.08)
        self.take_profit = risk.get("take_profit", 0.30)
        self.trailing_stop = risk.get("trailing_stop", 0.15)
        # 组合级风控（回撤熔断 + 波动率目标，默认跟随 config，与回测同一套）
        if dd_circuit is None:
            dd_circuit = config.DEFAULT_DD_CIRCUIT
        if vol_target is None:
            vol_target = config.DEFAULT_VOL_TARGET
        self.portfolio = PortfolioRisk(dd_circuit=dd_circuit, vol_target=vol_target)

    # ---- 熔断滞回状态的跨运行持久化（2026-09-29）----
    def set_circuit_active(self, active: bool):
        """恢复熔断滞回状态（由 runner 从 DB state 表读入）。

        实盘每次 run_once 都新建 RiskManager/PortfolioRisk，`_cb_active`
        实例状态不跨天 → 滞回带（10%~15%）在实盘从未生效。与 strategy_since
        同一机制：runner 负责存取，本类只暴露状态。
        """
        self.portfolio._cb_active = bool(active)

    def circuit_active(self) -> bool:
        """当前熔断是否 active（供 runner 落库）。"""
        return bool(self.portfolio._cb_active)

    def stop_codes(self, positions: dict, prices: dict) -> set:
        """返回触发个股止损/止盈/回撤止盈的持仓 code 集合。

        ⚠️ 2026-10-02 验收修复（Q-01/D-2）新增 —— 止损判据的**单一来源**。

        原先止损判据只写在 filter_weights() 内部，而 filter_weights 只在
        **调仓日**被 runner 调用 → 非调仓日（含熔断降仓持有期，最长 60 个
        交易日）个股止损/止盈**全部失效**，与回测 engine.py「每日检查止损」
        不同构。本方法把判据提出来，供两条路径共用：
          · 调仓日路径：filter_weights() 过滤目标权重时复用；
          · 非调仓日路径：runner 每日止损检查直接调用。
        """
        out = set()
        for code, pos in (positions or {}).items():
            if not pos or pos.get("qty", 0) <= 0 or code not in prices:
                continue
            px = prices[code]
            # isfinite：nan 价时 ret<=-8% 等比较全部为 False →
            # 止损静默失效（2026-09-29，与 _pos_ok 的 nan 穿透同类）
            if not math.isfinite(px):
                continue
            cost = pos.get("cost", 0.0)
            if cost <= 0:
                continue
            ret = px / cost - 1.0
            # 回撤止盈：从持仓期最高价回落
            peak = pos.get("peak", cost)
            drawdown = (px / peak - 1.0) if peak > 0 else 0.0
            if ret <= self.stop_loss:      # 触发止损 -> 清仓
                out.add(code)
            elif ret >= self.take_profit:  # 触发止盈 -> 清仓
                out.add(code)
            elif ret > 0 and drawdown <= -self.trailing_stop:  # 回撤止盈
                out.add(code)
        return out

    def filter_weights(self, target_weights: dict, positions: dict, prices: dict,
                       nav_history=None, enforce_stops: bool = True) -> dict:
        """根据当前持仓与成本，过滤/修正目标权重。

        参数
        ----
        target_weights : {code: weight} 策略目标权重
        positions      : {code: dict(qty=股数, cost=成本价, peak=持仓期内最高价)}
        prices         : {code: 最新价}
        返回
        ----
        {code: weight} 风控修正后的目标权重
        """
        out = {}
        # 个股止损/止盈判据统一走 stop_codes()（单一来源，2026-10-02）
        stops = self.stop_codes(positions, prices) if enforce_stops else set()
        for code, w in target_weights.items():
            if w <= 0:
                continue
            # 单标的上限
            if w > self.max_position_weight:
                w = self.max_position_weight

            # 已持有且触发止损/止盈（进攻型策略 enforce_stops=False 可关闭）
            if code in stops:
                out[code] = 0.0
                continue

            out[code] = w

        # 总仓位上限：等比缩放
        total = sum(out.values())
        if total > self.max_total_weight:
            scale = self.max_total_weight / total
            out = {c: w * scale for c, w in out.items()}

        # 组合级风控：回撤熔断 + 波动率目标（基于组合净值历史，与回测共享 PortfolioRisk）
        if nav_history is not None:
            pscale = self.portfolio.scale(nav_history)
            if pscale < 1.0:
                out = {c: w * pscale for c, w in out.items()}

        return out