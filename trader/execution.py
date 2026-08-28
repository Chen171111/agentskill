"""执行器：把目标权重/持仓差转成订单并提交给 broker。

自动化交易核心闭环：
    目标权重(target) --对比--> 当前持仓(actual) --差异--> 订单 --broker--> 成交 --apply_fill--> 账户
"""
from .broker import Broker, PaperBroker, Order


def _round_lot(qty: int) -> int:
    """A股按 100 股整手（向下取整）。"""
    return int(qty // 100 * 100)


class ExecutionEngine:
    def __init__(self, broker: Broker = None):
        self.broker = broker or PaperBroker()

    def rebalance(self, account, target_weights: dict, prices: dict) -> list:
        """根据目标权重与当前持仓，生成并执行订单。返回成交订单列表。

        参数
        ----
        account        : PortfolioAccount
        target_weights : {code: weight}（权重和 <= 1，其余现金）
        prices         : {code: 最新价}
        """
        self.broker.update_quotes(prices)
        equity = account.total_equity(prices)
        if equity <= 0:
            return []

        orders = []
        target_mv = {c: equity * w for c, w in target_weights.items() if w > 0}

        # 先卖出不在目标或需减仓的
        for code in list(account.positions.keys()):
            cur_qty = account.position_qty(code)
            if cur_qty <= 0:
                continue
            tgt_mv = target_mv.get(code, 0.0)
            px = prices.get(code, 0.0)
            cur_mv = cur_qty * px
            if cur_mv > tgt_mv:
                sell_mv = cur_mv - tgt_mv
                sell_qty = _round_lot(int(sell_mv / px)) if px > 0 else 0
                if sell_qty > 0:
                    orders.append(Order(code, "sell", sell_qty, px, reason="减仓/清仓"))

        # 再买入需加仓的
        for code, tgt_mv in target_mv.items():
            cur_qty = account.position_qty(code)
            px = prices.get(code, 0.0)
            if px <= 0:
                continue
            cur_mv = cur_qty * px
            if tgt_mv > cur_mv:
                buy_mv = min(tgt_mv - cur_mv, account.cash)
                buy_qty = _round_lot(int(buy_mv / px))
                if buy_qty > 0:
                    # 现金不足时按整手收敛
                    while buy_qty > 0 and buy_qty * px * 1.001 > account.cash:
                        buy_qty -= 100
                    if buy_qty > 0:
                        orders.append(Order(code, "buy", buy_qty, px, reason="加仓/建仓"))

        # 提交执行
        filled = []
        for o in orders:
            self.broker.submit(o)
            if o.status == "filled":
                account.apply_fill(o, o.filled_price)
                filled.append(o)
        return filled