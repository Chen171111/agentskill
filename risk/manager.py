"""风控层：仓位限制 + 个股止损/止盈/回撤止盈。

在实盘/模拟盘场景，风控器基于持仓成本价，对策略产出的目标权重做二次修正：
- 强制平掉触发止损/止盈的持仓；
- 限制单标的权重上限与组合总仓位上限。
"""
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

    def filter_weights(self, target_weights: dict, positions: dict, prices: dict,
                       nav_history=None) -> dict:
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
        for code, w in target_weights.items():
            if w <= 0:
                continue
            # 单标的上限
            if w > self.max_position_weight:
                w = self.max_position_weight

            # 已持有：检查止损/止盈
            pos = positions.get(code)
            if pos and pos.get("qty", 0) > 0 and code in prices:
                cost = pos.get("cost", 0.0)
                px = prices[code]
                ret = (px / cost - 1.0) if cost > 0 else 0.0
                # 回撤止盈：从持仓期最高价回落
                peak = pos.get("peak", cost)
                drawdown = (px / peak - 1.0) if peak > 0 else 0.0
                if ret <= self.stop_loss:      # 触发止损 -> 清仓
                    out[code] = 0.0
                    continue
                if ret >= self.take_profit:    # 触发止盈 -> 清仓
                    out[code] = 0.0
                    continue
                if ret > 0 and drawdown <= -self.trailing_stop:  # 回撤止盈
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