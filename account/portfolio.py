"""组合账户：管理现金 + 持仓（数量、成本、峰值），支持下单撮合与市值记账。

这是「实盘/模拟盘」的账户核心，与回测账户相互独立。
"""
import config


class PortfolioAccount:
    def __init__(self, init_cash: float = None):
        self.init_cash = float(init_cash if init_cash is not None else config.INIT_CASH)
        self.cash = self.init_cash
        # positions: {code: {"qty": int, "cost": float, "peak": float}}
        self.positions = {}

    # ---- 持仓 ----
    def position_qty(self, code: str) -> int:
        pos = self.positions.get(code)
        return pos["qty"] if pos else 0

    def position_cost(self, code: str) -> float:
        pos = self.positions.get(code)
        return pos["cost"] if pos else 0.0

    def market_value(self, prices: dict) -> float:
        mv = 0.0
        for code, pos in self.positions.items():
            mv += pos["qty"] * prices.get(code, 0.0)
        return mv

    def total_equity(self, prices: dict) -> float:
        return self.cash + self.market_value(prices)

    def _add_position(self, code: str, qty: int, price: float, fee: float):
        pos = self.positions.get(code, {"qty": 0, "cost": 0.0, "peak": 0.0})
        old_qty, old_cost = pos["qty"], pos["cost"]
        new_qty = old_qty + qty
        # 加权平均成本（含手续费摊入买入方）
        if qty > 0:
            total_cost = old_cost * old_qty + price * qty + fee
            new_cost = total_cost / new_qty if new_qty > 0 else 0.0
        else:
            new_cost = old_cost
        self.positions[code] = {
            "qty": new_qty,
            "cost": new_cost,
            "peak": max(pos["peak"], price),
        }
        if new_qty <= 0:
            del self.positions[code]

    def apply_fill(self, order, price: float):
        """按成交结果更新现金与持仓。"""
        if order.status != "filled" or order.filled_qty <= 0:
            return
        qty = order.filled_qty
        notional = order.filled_price * qty
        fee = order.fee
        if order.side == "buy":
            self.cash -= notional + fee
            self._add_position(order.code, qty, order.filled_price, fee)
        else:
            self.cash += notional - fee
            self._add_position(order.code, -qty, order.filled_price, 0.0)

    def snapshot(self, prices: dict) -> dict:
        """输出账户快照。"""
        return {
            "cash": self.cash,
            "market_value": self.market_value(prices),
            "total_equity": self.total_equity(prices),
            "positions": [
                {"code": c, "qty": p["qty"], "cost": round(p["cost"], 4),
                 "peak": round(p["peak"], 4),
                 "price": prices.get(c, 0.0),
                 "pct": (prices.get(c, 0.0) / p["cost"] - 1.0) if p["cost"] > 0 else 0.0}
                for c, p in self.positions.items() if p["qty"] > 0
            ],
        }