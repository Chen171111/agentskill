"""执行器：把目标权重/持仓差转成订单并提交给 broker。

自动化交易核心闭环：
    目标权重(target) --对比--> 当前持仓(actual) --差异--> 订单 --broker--> 成交 --apply_fill--> 账户
"""
import math
import config
from .broker import Broker, PaperBroker, Order
from .costs import estimate_fee


def _round_lot(qty: int) -> int:
    """A股按 100 股整手（向下取整）。"""
    return int(qty // 100 * 100)


class ExecutionEngine:
    def __init__(self, broker: Broker = None, on_order=None):
        self.broker = broker or PaperBroker()
        self.on_order = on_order

    def _record(self, order):
        if self.on_order is not None:
            self.on_order(order)

    def _submit(self, order, account):
        if getattr(self.broker, "requires_trading_session", False):
            from tools.trading_session import require_trading_session
            require_trading_session()
        order.status = "pending_submit"
        self._record(order)  # durable intent BEFORE any external side effect
        try:
            self.broker.submit(order)
        except BaseException as exc:
            if order.status == "rejected":
                order.reason = "{} [已拒绝:{}]".format(order.reason, str(exc)[:150])
            else:
                order.status = "unknown"
                order.reason = "{} [提交结果未知:{}]".format(order.reason, str(exc)[:150])
            self._record(order)
            if not isinstance(exc, Exception):
                raise
        else:
            if order.status == "filled":
                account.apply_fill(order, order.filled_price)
            self._record(order)
        return order

    def _terms(self):
        return {key: getattr(self.broker, key, config.TRADING_COST[key])
                for key in ("commission_rate", "min_commission", "sell_tax_rate")}

    def rebalance(self, account, target_weights: dict, prices: dict) -> list:
        """Submit journaled sells, then size buys using confirmed available cash."""
        for code, weight in target_weights.items():
            if not math.isfinite(float(weight)) or weight < 0:
                raise ValueError("目标权重必须是有限非负数: {}".format(code))
        if sum(target_weights.values()) > 1.00000001:
            raise ValueError("目标权重之和不能超过1")
        self.broker.update_quotes(prices)
        equity = account.total_equity(prices)
        if not math.isfinite(equity) or equity <= 0:
            return []
        target_mv = {c: equity * w for c, w in target_weights.items() if w > 0}
        placed = []
        for code, position in list(account.positions.items()):
            px = prices.get(code, 0.)
            if not math.isfinite(px) or px <= 0:
                continue
            qty = account.position_qty(code)
            available = position.get("available")
            if available is None:
                available = max(0, qty - int(position.get("frozen") or 0))
            target = target_mv.get(code, 0.)
            if qty * px > target:
                wanted = qty if target == 0 else _round_lot(int((qty * px - target) / px))
                sell_qty = min(wanted, max(0, int(available)), qty)
                if sell_qty:
                    order = self._submit(Order(code, "sell", sell_qty, px, "减仓/清仓"), account)
                    placed.append(order)
                    if order.status == "unknown":
                        return placed
        # A submitted sale is not buying power. Only broker truth / filled cash counts.
        if placed and hasattr(self.broker, "reconcile"):
            if not self.broker.reconcile(account, prices):
                raise RuntimeError("卖出后购买力对账失败，停止买入")
        cash_left = float(account.cash)
        terms = self._terms()
        for code, target in target_mv.items():
            px = prices.get(code, 0.)
            if not math.isfinite(px) or px <= 0:
                continue
            remaining = target - account.position_qty(code) * px
            if remaining <= 0 or cash_left <= 0:
                continue
            slip = self.broker.slippage_rate if isinstance(self.broker, PaperBroker) else 0.
            fill = px * (1 + slip)
            qty = _round_lot(int(min(remaining, cash_left) / fill))
            while qty > 0 and (fill * qty + estimate_fee(fill * qty, code, cost=terms)) > cash_left:
                qty -= 100
            if qty <= 0:
                continue
            order = Order(code, "buy", qty, px, "加仓/建仓")
            order.cash_limit = cash_left
            order.max_notional = remaining
            cost = fill * qty + estimate_fee(fill * qty, code, cost=terms)
            self._submit(order, account)
            placed.append(order)
            if order.status not in ("rejected", "cancelled"):
                cash_left -= cost
            if order.status == "unknown":
                break
        return placed
