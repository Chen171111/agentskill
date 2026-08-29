"""券商（broker）抽象接口 + 模拟撮合实现 + 同花顺客户端接入。

设计目标：把「信号 → 下单 → 成交」与具体券商解耦。
- PaperBroker : 本地模拟撮合（按收盘价成交，计入滑点/佣金/印花税），用于模拟盘；
- ThsBroker   : 同花顺客户端（含模拟炒股），通过 easytrader 操控桌面界面下单。
  接入实盘时，实现新的 Broker 子类即可，无需改动上层执行逻辑。
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


class ThsBroker(Broker):
    """同花顺客户端（ths）下单接口，基于 easytrader。

    依赖
    ----
    pip install easytrader

    使用前提
    --------
    1. 安装同花顺经典版客户端（v8.60+，而非极速版），并启动；
    2. 手动登录到交易/模拟炒股窗口；
    3. 客户端设置：系统设置>界面设置 超时时间=0；交易设置 默认买卖价格/数量=空；
    4. 客户端不能最小化、不能用精简模式。

    用法
    ----
        broker = ThsBroker(exe_path=r"C:\\同花顺\\xiadan.exe")
        broker.connect()
        broker.submit(Order("600519.SH", "buy", 100, 1500.0))
    """

    def __init__(self, exe_path: str = None):
        import easytrader  # 延迟导入，避免未安装时报错
        self._easytrader = easytrader
        self.user = easytrader.use("ths")
        self.exe_path = exe_path
        self._connected = False

    def connect(self, exe_path: str = None):
        """连接到已登录的同花顺客户端交易窗口。"""
        path = exe_path or self.exe_path
        if path:
            self.user.connect(path)
        else:
            # 无 exe_path 时，easytrader 会尝试识别已登录的窗口
            self.user.connect()
        self._connected = True
        return self

    @staticmethod
    def _pure_code(code: str) -> str:
        """600519.SH -> 600519（同花顺下单用纯 6 位数字）。"""
        return code.split(".")[0]

    def submit(self, order: Order) -> Order:
        if not self._connected:
            raise RuntimeError("ThsBroker 未连接，请先调用 connect()")
        code = self._pure_code(order.code)
        price = round(order.price, 2)
        try:
            if order.side == "buy":
                self.user.buy(code, price=price, amount=int(order.qty))
            else:
                self.user.sell(code, price=price, amount=int(order.qty))
            order.status = "filled"
            order.filled_price = price
            order.filled_qty = int(order.qty)
            # 注：同花顺实际成交价/数量以委托回报为准，此处按限价记录，佣金由券商核算
            order.fee = 0.0
        except Exception as e:
            order.status = "rejected"
            order.reason = "下单失败: {}".format(e)
        return order

    # ---- 账户查询（可选，供状态同步）----
    @property
    def balance(self):
        return self.user.balance

    @property
    def position(self):
        return self.user.position

    @property
    def today_trades(self):
        return self.user.today_trades