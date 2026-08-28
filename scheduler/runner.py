"""调度层：每个交易日收盘后自动运行一次交易闭环。

DailyRunner.run_once() 是自动化交易的核心入口：
    数据 → 因子 → 策略信号 → 风控 → 执行订单 → 持久化
Scheduler 负责按 SCHEDULE.run_time 每日定时触发（收盘后）。
"""
import time
from datetime import datetime, date

import config
from dataprovider.store import DataStore
from dataprovider.panel import build_panel
from factors.engine import compute_factors
from strategies.registry import create_strategy
from risk.manager import RiskManager
from account.portfolio import PortfolioAccount
from trader.execution import ExecutionEngine
from storage.db import TradeDB

DEFAULT_FACTORS = ["rsi", "macd_hist", "bias20", "sma_gap", "momentum20", "vol_ratio"]


class DailyRunner:
    """单次交易日运行器。"""

    def __init__(self, codes, strategy="momentum", topk=5, rebalance=5, timing=None,
                 init_cash=None):
        self.codes = codes
        self.strategy_name = strategy
        self.topk = topk
        self.rebalance = rebalance
        self.timing = timing
        self.store = DataStore()
        self.db = TradeDB()
        self.account = PortfolioAccount(init_cash=init_cash)
        # 恢复历史持仓（若存在）
        saved = self.db.load_positions()
        if saved:
            self.account.positions = saved
        self.strategy = create_strategy(strategy, topk=topk, rebalance_every=rebalance)

    def run_once(self, end: str = None) -> dict:
        """执行一个交易日的完整闭环。end 传 None 则用最新数据。"""
        self.store.ensure(self.codes)
        end = end or datetime.now().strftime("%Y%m%d")
        # 取最近 200 个交易日用于计算因子
        start = _n_days_ago(end, 200)
        panel = build_panel(self.store, self.codes, start=start, end=end)
        if len(panel.dates) == 0:
            return {"status": "no_data"}

        factors = compute_factors(panel, DEFAULT_FACTORS)
        last_date = panel.dates[-1]

        # 生成策略目标权重（每次 run_once 视为一个调仓评估日，强制触发信号）
        self.strategy._since = max(0, self.strategy.rebalance_every - 1)
        weights = self.strategy.generate_weights(last_date, factors, panel)
        weights = weights or {}

        # 风控修正（基于当前持仓成本）
        risk = RiskManager()
        prices = {c: float(panel.get("close").loc[last_date, c]) for c in panel.codes}
        weights = risk.filter_weights(weights, self.account.positions, prices)

        # 执行订单
        executor = ExecutionEngine()
        orders = executor.rebalance(self.account, weights, prices)

        # 持久化
        for o in orders:
            self.db.save_order(o)
        snap = self.account.snapshot(prices)
        self.db.save_equity(last_date, snap["cash"], snap["market_value"],
                            snap["total_equity"])
        self.db.save_positions(self.account.positions)

        return {
            "status": "ok",
            "date": last_date,
            "strategy": self.strategy_name,
            "target_weights": {c: round(w, 4) for c, w in weights.items()},
            "orders": [o.to_dict() for o in orders],
            "account": snap,
        }


def _n_days_ago(end: str, n: int) -> str:
    from datetime import timedelta
    d = datetime.strptime(str(end), "%Y%m%d")
    return (d - timedelta(days=int(n * 1.6) + 20)).strftime("%Y%m%d")


class Scheduler:
    """按固定时间每日触发（收盘后）。

    用法（常驻进程）:
        sched = Scheduler(runner)
        sched.run_forever()
    """

    def __init__(self, runner: DailyRunner, run_time: str = None):
        self.runner = runner
        self.run_time = run_time or config.SCHEDULE["run_time"]
        self._last_run = None

    def _is_trading_day(self) -> bool:
        """A股：周一至周五（不含节假日，节假日判断从简，可替换为交易日历）。"""
        return date.today().weekday() < 5

    def run_forever(self):
        print("[Scheduler] 启动，每日 {} 触发（仅交易日）。Ctrl+C 退出。".format(self.run_time))
        while True:
            now = datetime.now()
            hhmm = now.strftime("%H:%M")
            today = now.date()
            if hhmm == self.run_time and self._is_trading_day() and self._last_run != today:
                try:
                    result = self.runner.run_once()
                    print("[Scheduler] {} 执行完成: {}".format(today, result.get("status")))
                except Exception as e:
                    print("[Scheduler] 执行失败: {}".format(e))
                self._last_run = today
            time.sleep(1)