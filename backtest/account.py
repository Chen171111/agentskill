"""回测账户：权重式组合账户，计入手续费/滑点/印花税。"""
import pandas as pd

import config


class BacktestAccount:
    def __init__(self, init_cash=None, cost: dict = None):
        cost = cost or config.TRADING_COST
        self.init_cash = float(init_cash if init_cash is not None else config.INIT_CASH)
        self.commission = float(cost.get("commission_rate", 0.0003))
        self.min_commission = float(cost.get("min_commission", 5.0))
        self.sell_tax = float(cost.get("sell_tax_rate", 0.001))
        self.slippage = float(cost.get("slippage_rate", 0.0005))

        self.cash = self.init_cash
        self.positions = {}
        self._equity_dates = []
        self._equity = []

    def total(self) -> float:
        return sum(self.positions.values()) + self.cash

    def update(self, date, rates: dict):
        new_pos = {}
        for code, mv in self.positions.items():
            new_pos[code] = mv * (1 + rates.get(code, 0.0))
        self.positions = new_pos
        self._equity_dates.append(date)
        self._equity.append(self.total())

    def _trade_cost(self, notional: float, is_sell: bool) -> float:
        fee = notional * (self.commission + self.slippage)
        fee = max(fee, self.min_commission if notional > 0 else 0.0)
        if is_sell:
            fee += notional * self.sell_tax
        return fee

    def rebalance(self, weights: dict):
        total = self.total()
        if total <= 0:
            self.positions = {}
            self.cash = 0
            return
        target = {c: total * w for c, w in weights.items() if w > 0}
        current = dict(self.positions)
        cost = 0.0
        for code, mv in current.items():
            if mv > target.get(code, 0.0):
                cost += self._trade_cost(mv - target.get(code, 0.0), is_sell=True)
        for code, tgt in target.items():
            have = current.get(code, 0.0)
            if tgt > have:
                cost += self._trade_cost(tgt - have, is_sell=False)
        self.positions = target
        self.cash = max(total - sum(target.values()) - cost, 0.0)

    def results(self) -> pd.DataFrame:
        df = pd.DataFrame({"date": self._equity_dates, "value": self._equity})
        rate = df["value"].pct_change().fillna(0.0)
        equity = (rate + 1).cumprod()
        if len(df):
            equity.iloc[0] = 1.0
        df = pd.DataFrame({"date": df["date"], "value": df["value"],
                           "rate": rate, "equity": equity})
        df.set_index("date", inplace=True)
        return df

    def holding(self):
        return list(self.positions.keys())