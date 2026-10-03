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
        sell_proceeds = 0.0          # 预估卖出回款（未扣费，保守）
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
                    sell_proceeds += sell_qty * px

        # 再买入需加仓的
        # ⚠️ cash_left 逐单扣减（2026-09-29）：真实券商 submit() 只返回
        # submitted、不当场成交，account.cash 在**整批订单生成期间不会变**——
        # 多笔买单都按交易前现金核算会**超配**（ broker 拒单/部分成交，
        # 策略目标失真）。卖出回款按 A 股当日可用计入预算（先卖后买的提交
        # 顺序也保证这一点）； PaperBroker 路径同样受益（宁少勿超）。
        cash_left = account.cash + sell_proceeds
        for code, tgt_mv in target_mv.items():
            cur_qty = account.position_qty(code)
            px = prices.get(code, 0.0)
            if px <= 0:
                continue
            cur_mv = cur_qty * px
            if tgt_mv > cur_mv:
                buy_mv = min(tgt_mv - cur_mv, cash_left)
                buy_qty = _round_lot(int(buy_mv / px))
                if buy_qty > 0:
                    # 现金不足时按整手收敛（1.001 含费用缓冲）
                    while buy_qty > 0 and buy_qty * px * 1.001 > cash_left:
                        buy_qty -= 100
                    if buy_qty > 0:
                        cash_left -= buy_qty * px * 1.001
                        orders.append(Order(code, "buy", buy_qty, px, reason="加仓/建仓"))

        # 提交执行（逐单隔离，2026-10-02 验收修复 P1-4）
        placed = []
        for o in orders:
            try:
                self.broker.submit(o)
            except Exception as e:
                # 逐单隔离：任一单 submit 抛异常不得中断整批 —— 否则已提交到券商的
                # 订单既不返回也不落库（runner 的 save_order 只保存返回的 placed），
                # 表现为「既丢单又可能在下次运行重复下单」。
                # ⚠️ submit 抛异常 ≠ 委托未报出：UiaThsBroker 是「先点确认、再读回执」，
                # 超时/读回执失败时委托可能已在券商侧成立 → 必须保留订单、标记 error，
                # 让台账留下「报过但状态未知」的痕迹，由 sync_fill/reconcile 事后澄清。
                o.status = "error"
                o.reason = "{} [submit异常:{}]".format(o.reason or "", str(e)[:150]).strip()
            if o.status == "filled":
                # 本地撮合（PaperBroker）：立即计入账户
                account.apply_fill(o, o.filled_price)
            # 无论终态如何一律返回（rejected/error 同样是发生过的事实，必须落台账）
            placed.append(o)
        return placed