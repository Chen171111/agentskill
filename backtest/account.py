"""回测账户：数量式组合账户（对齐实盘 PortfolioAccount 口径）。

按开盘价撮合到目标权重，计入整手(100股)、手续费/滑点/印花税，每日按收盘价记账；
持仓记录成本价与持仓期峰值，供个股级止损/止盈/回撤止盈（与实盘 RiskManager 同构）。
"""
import pandas as pd

import config


def _is_stock(code: str) -> bool:
    """判断是否 A股个股（ETF/基金卖出免征印花税）。"""
    from dataprovider.store import classify_code
    return classify_code(code) == "stock"


def _round_lot(qty: int) -> int:
    """A股按 100 股整手（向下取整）。"""
    return int(qty // 100 * 100)


class BacktestAccount:
    def __init__(self, init_cash=None, cost: dict = None):
        cost = cost or config.TRADING_COST
        self.init_cash = float(init_cash if init_cash is not None else config.INIT_CASH)
        self.cash = self.init_cash
        self.commission = float(cost.get("commission_rate", 0.0003))
        self.min_commission = float(cost.get("min_commission", 5.0))
        self.sell_tax = float(cost.get("sell_tax_rate", 0.001))
        self.slippage = float(cost.get("slippage_rate", 0.0005))
        self.positions = {}   # {code: {"qty": int, "cost": float, "peak": float}}
        self._dates = []
        self._equity = []

    def _qty(self, code: str) -> int:
        p = self.positions.get(code)
        return p["qty"] if p else 0

    # ---- 市值 / 权益 ----
    def market_value(self, prices: dict) -> float:
        return sum(p["qty"] * prices.get(c, 0.0) for c, p in self.positions.items())

    def total(self, prices: dict = None) -> float:
        return self.cash + self.market_value(prices or {})

    def mark_to_close(self, date, prices: dict):
        """每日收盘：更新持仓峰值 + 记录总权益。"""
        for code, p in self.positions.items():
            px = prices.get(code)
            if px and px > p["peak"]:
                p["peak"] = px
        self._dates.append(date)
        self._equity.append(self.cash + self.market_value(prices))

    def _fee(self, notional: float, is_sell: bool, code: str) -> float:
        fee = notional * self.commission
        fee = max(fee, self.min_commission if notional > 0 else 0.0)
        if is_sell and _is_stock(code):
            fee += notional * self.sell_tax
        return fee

    def trade(self, target_weights: dict, open_prices: dict,
              buy_blocked=(), sell_blocked=()):
        """按开盘价撮合到目标权重。

        target_weights : {code: 目标权重}（权重和 ≤1，其余现金）
        open_prices    : {code: 当日开盘价}
        buy_blocked    : 当日买不进的 code 集合（停牌/一字涨停）
        sell_blocked   : 当日卖不出的 code 集合（停牌/一字跌停）
        """
        buy_blocked = set(buy_blocked or ())
        sell_blocked = set(sell_blocked or ())
        total = self.cash + self.market_value(open_prices)
        if total <= 0:
            self.positions = {}
            self.cash = 0.0
            return
        target_mv = {c: total * w for c, w in target_weights.items() if w > 0}

        # 1) 先卖：减仓/清仓（跌停/停牌卖不出）
        for code in list(self.positions.keys()):
            qty = self._qty(code)
            px = open_prices.get(code, 0.0)
            if qty <= 0 or px <= 0 or code in sell_blocked:
                continue
            if qty * px > target_mv.get(code, 0.0):
                sell_qty = min(_round_lot(int((qty * px - target_mv.get(code, 0.0)) / px)), qty)
                if sell_qty <= 0:
                    continue
                fill = px * (1 - self.slippage)
                notional = fill * sell_qty
                self.cash += notional - self._fee(notional, True, code)
                new_qty = qty - sell_qty
                if new_qty <= 0:
                    del self.positions[code]
                else:
                    self.positions[code]["qty"] = new_qty

        # 2) 后买：建仓/加仓（涨停/停牌买不进，现金不足按整手收敛）
        for code, tgt_mv in target_mv.items():
            px = open_prices.get(code, 0.0)
            if px <= 0 or code in buy_blocked:
                continue
            cur_qty = self._qty(code)
            if tgt_mv > cur_qty * px:
                fill = px * (1 + self.slippage)
                buy_qty = _round_lot(int(min(tgt_mv - cur_qty * px, self.cash) / fill))
                while buy_qty > 0 and (fill * buy_qty + self._fee(fill * buy_qty, False, code)) > self.cash:
                    buy_qty -= 100
                if buy_qty > 0:
                    notional = fill * buy_qty
                    fee = self._fee(notional, False, code)
                    self.cash -= notional + fee
                    old = self.positions.get(code)
                    if old:
                        tot_cost = old["cost"] * old["qty"] + notional + fee
                        old["qty"] += buy_qty
                        old["cost"] = tot_cost / old["qty"]
                        old["peak"] = max(old["peak"], fill)
                    else:
                        self.positions[code] = {"qty": buy_qty,
                                                "cost": (notional + fee) / buy_qty,
                                                "peak": fill}

    def stop_loss_codes(self, prices: dict, stop_loss=-0.08,
                        take_profit=0.30, trailing_stop=0.15):
        """返回触发个股级止损/止盈/回撤止盈的 code 集合（与实盘 RiskManager 同构）。"""
        out = set()
        for code, p in self.positions.items():
            px = prices.get(code)
            if not px or p["cost"] <= 0:
                continue
            ret = px / p["cost"] - 1.0
            if ret <= stop_loss:
                out.add(code)
            elif ret >= take_profit:
                out.add(code)
            elif ret > 0 and p["peak"] > 0 and (px / p["peak"] - 1.0) <= -trailing_stop:
                out.add(code)
        return out

    def holding(self) -> list:
        return [c for c, p in self.positions.items() if p["qty"] > 0]

    def results(self) -> pd.DataFrame:
        df = pd.DataFrame({"date": self._dates, "value": self._equity})
        rate = df["value"].pct_change().fillna(0.0)
        equity = (rate + 1).cumprod()
        if len(df):
            equity.iloc[0] = 1.0
        df = pd.DataFrame({"date": df["date"], "value": df["value"],
                           "rate": rate, "equity": equity})
        df.set_index("date", inplace=True)
        return df