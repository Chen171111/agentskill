"""券商（broker）抽象接口 + 模拟撮合实现。

设计目标：把「信号 → 下单 → 成交」与具体券商解耦。
- PaperBroker : 本地模拟撮合（按收盘价成交，计入滑点/佣金/印花税），用于模拟盘；
- 接入实盘时，实现 LiveBroker 子类（如基于 easytrader），无需改动上层执行逻辑。
"""
import abc
import itertools
import uuid
from datetime import datetime

import config

_cost = config.TRADING_COST


class Order:
    """订单对象。side: buy/sell。"""

    _ids = itertools.count(1)

    def __init__(self, code: str, side: str, qty: int, price: float, reason: str = ""):
        self.id = "{}-{}".format(datetime.now().strftime("%Y%m%d%H%M%S"), next(self._ids))
        self.code = code
        self.side = side          # buy / sell
        self.qty = int(qty)       # 股数（A股 100 取整）
        self.price = float(price)
        self.reason = reason
        self.status = "submitted"  # submitted / filled / rejected
        self.filled_price = None
        self.filled_qty = 0
        self.fee = 0.0

    def to_dict(self):
        return {
            "id": self.id, "code": self.code, "side": self.side, "qty": self.qty,
            "price": round(self.price, 4), "reason": self.reason,
            "status": self.status, "filled_price": self.filled_price,
            "filled_qty": self.filled_qty, "fee": round(self.fee, 4),
        }


class Broker(abc.ABC):
    """券商抽象基类。"""

    @abc.abstractmethod
    def submit(self, order: Order) -> Order:
        """提交订单并返回成交后的订单。"""


class PaperBroker(Broker):
    """模拟撮合券商。

    调用方通过当前价 update_quotes() 设置各标的成交价，submit() 时按该价撮合。
    - 买入成交价 = 基准价 * (1+滑点)
    - 卖出成交价 = 基准价 * (1-滑点)
    - 计入双边佣金 + 卖出印花税
    """

    def __init__(self, cost: dict = None):
        cost = cost or _cost
        self.commission_rate = cost.get("commission_rate", 0.0003)
        self.min_commission = cost.get("min_commission", 5.0)
        self.sell_tax_rate = cost.get("sell_tax_rate", 0.001)
        self.slippage_rate = cost.get("slippage_rate", 0.0005)
        self.quotes = {}

    def update_quotes(self, prices: dict):
        """更新各标的当前价（用于撮合）。"""
        self.quotes.update(prices)

    def submit(self, order: Order) -> Order:
        px = self.quotes.get(order.code)
        if px is None or px <= 0:
            order.status = "rejected"
            return order

        if order.side == "buy":
            filled = px * (1 + self.slippage_rate)
        else:
            filled = px * (1 - self.slippage_rate)

        notional = filled * order.qty
        fee = notional * (self.commission_rate + self.slippage_rate)
        fee = max(fee, self.min_commission if order.qty > 0 else 0.0)
        if order.side == "sell":
            fee += notional * self.sell_tax_rate

        order.status = "filled"
        order.filled_price = filled
        order.filled_qty = order.qty
        order.fee = fee
        return order