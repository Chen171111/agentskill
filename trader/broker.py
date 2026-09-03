"""券商（broker）抽象接口 + 模拟撮合实现 + 同花顺客户端接入。

设计目标：把「信号 → 下单 → 成交」与具体券商解耦。
- PaperBroker : 本地模拟撮合（按收盘价成交，计入滑点/佣金/印花税），用于模拟盘；
- ThsBroker   : 同花顺客户端（含模拟炒股），通过 easytrader 操控桌面界面下单。
  接入实盘时，实现新的 Broker 子类即可，无需改动上层执行逻辑。
"""
import abc
import itertools
import time
import uuid
from datetime import datetime

import config

_cost = config.TRADING_COST


def _is_a_stock(code: str) -> bool:
    """判断是否 A股个股（用于印花税：个股卖出收 0.1%，ETF/基金免征）。"""
    from dataprovider.store import classify_code
    return classify_code(code) == "stock"


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

    def update_quotes(self, prices: dict):
        """更新报价（供撮合类券商使用）。默认无操作，实盘/真实券商无需实现。"""
        pass

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
        # 佣金（滑点已体现在成交价里，不再重复计费）
        fee = notional * self.commission_rate
        fee = max(fee, self.min_commission if order.qty > 0 else 0.0)
        # 印花税：仅个股卖出收取（ETF/基金免征）
        if order.side == "sell" and _is_a_stock(order.code):
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

    def connect(self, exe_path: str = None, retries: int = 2):
        """连接到已登录的同花顺客户端交易窗口（失败重试 + 友好告警，不裸抛 pywinauto）。"""
        path = exe_path or self.exe_path
        last_err = None
        for attempt in range(retries + 1):
            try:
                if path:
                    self.user.connect(path)
                else:
                    # 无 exe_path 时，easytrader 会尝试识别已登录的窗口
                    self.user.connect()
                self._connected = True
                return self
            except Exception as e:
                last_err = e
                if attempt < retries:
                    time.sleep(1)
        raise RuntimeError(
            "同花顺连接失败：{}。请确认 1) 同花顺经典版已启动并登录到模拟炒股；"
            "2) 委托窗口可见（不可最小化/精简模式）；3) 以管理员权限运行。".format(last_err))

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
            order.fee = 0.0
            # 回读今日成交，用真实成交价/数量修正订单（替代限价记账，避免账本失真）
            self.sync_fill(order)
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

    # ---- 对账：回读同花顺真实持仓/资金/成交，消除双账本 ----

    @staticmethod
    def _as_records(data):
        """把 easytrader 返回的 DataFrame/dict/list 统一成 list[dict]。"""
        if data is None:
            return []
        if hasattr(data, "to_dict"):
            try:
                return data.to_dict("records")
            except Exception:
                return []
        if isinstance(data, dict):
            return [data]
        if isinstance(data, (list, tuple)):
            return [d for d in data if isinstance(d, dict)]
        return []

    @staticmethod
    def _pick(rec, *keys):
        for k in keys:
            if k in rec and rec[k] is not None:
                return rec[k]
        return None

    @staticmethod
    def _guess_full_code(code6: str) -> str:
        code6 = str(code6).zfill(6)
        if code6.startswith(("6", "9", "5")):
            return code6 + ".SH"
        if code6.startswith(("0", "3", "1")):
            return code6 + ".SZ"
        return code6

    def fetch_position(self) -> dict:
        """回读同花顺真实持仓 → {纯代码: {"qty", "cost", "price"}}。"""
        try:
            records = self._as_records(self.user.position)
        except Exception as e:
            print("[ThsBroker] 读取持仓失败: {}".format(e))
            return {}
        out = {}
        for r in records:
            raw = self._pick(r, "证券代码", "股票代码", "证券", "code")
            if raw is None:
                continue
            code = str(raw).zfill(6)
            qty = self._pick(r, "股票余额", "持仓数量", "当前持仓", "可用余额", "qty")
            try:
                qty = int(float(qty)) if qty is not None else 0
            except (TypeError, ValueError):
                qty = 0
            if qty <= 0:
                continue
            cost = self._pick(r, "成本价", "摊薄成本", "成本", "cost")
            price = self._pick(r, "市价", "最新价", "price")
            out[code] = {
                "qty": qty,
                "cost": float(cost) if cost is not None else 0.0,
                "price": float(price) if price is not None else 0.0,
            }
        return out

    def fetch_balance(self) -> dict:
        """回读同花顺真实资金 → {"cash", "market_value", "total"}（字段可得则返回）。"""
        try:
            records = self._as_records(self.user.balance)
        except Exception as e:
            print("[ThsBroker] 读取资金失败: {}".format(e))
            return {}
        if not records:
            return {}
        r = records[0]
        out = {}
        cash = self._pick(r, "可用金额", "可用资金", "资金余额", "enable_balance", "cash")
        mv = self._pick(r, "股票市值", "证券市值", "持仓市值", "market_value")
        total = self._pick(r, "总资产", "资产总值", "total_asset", "total")
        if cash is not None:
            out["cash"] = float(cash)
        if mv is not None:
            out["market_value"] = float(mv)
        if total is not None:
            out["total"] = float(total)
        return out

    def sync_fill(self, order: Order):
        """下单后回读今日成交，用真实成交价/数量修正订单（替代限价记账）。"""
        try:
            records = self._as_records(self.user.today_trades)
        except Exception:
            return
        code = self._pure_code(order.code)
        for r in records:
            rc = self._pick(r, "证券代码", "股票代码", "证券", "code")
            if rc is None or str(rc).zfill(6) != code:
                continue
            px = self._pick(r, "成交价格", "成交价", "价格", "price")
            qty = self._pick(r, "成交数量", "成交量", "数量", "qty")
            if px is not None:
                order.filled_price = float(px)
            if qty is not None:
                order.filled_qty = int(float(qty))
            return

    def reconcile(self, account) -> bool:
        """用同花顺真实持仓/资金校正本地账户，消除双账本。返回是否校正成功。"""
        pos = self.fetch_position()
        bal = self.fetch_balance()
        if not pos and not bal:
            return False
        old_pos = getattr(account, "positions", {}) or {}
        new_positions = {}
        for code, p in pos.items():
            full = self._guess_full_code(code)
            old = old_pos.get(full)
            old_peak = old.get("peak", 0.0) if old else 0.0
            new_positions[full] = {
                "qty": p["qty"],
                "cost": p["cost"],
                "peak": max(p["price"] or p["cost"] or 0.0, old_peak),  # 保留历史峰值，避免回撤止盈基线失真
            }
        account.positions = new_positions
        if "cash" in bal:
            account.cash = bal["cash"]
        elif "total" in bal and "market_value" in bal:
            account.cash = bal["total"] - bal["market_value"]
        return True