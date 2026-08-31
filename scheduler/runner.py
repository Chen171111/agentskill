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
from trader.broker import ThsBroker
from storage.db import TradeDB

DEFAULT_FACTORS = ["rsi", "macd_hist", "bias20", "sma_gap", "momentum20", "vol_ratio"]


class DailyRunner:
    """单次交易日运行器。"""

    def __init__(self, codes, strategy="momentum", topk=5, rebalance=5, timing=None,
                 init_cash=None, broker=None):
        self.codes = codes
        self.strategy_name = strategy
        self.topk = topk
        self.rebalance = rebalance
        self.timing = timing
        self.store = DataStore()
        self.db = TradeDB()
        self.account = PortfolioAccount(init_cash=init_cash)
        # 恢复历史持仓与现金（若存在）
        saved = self.db.load_positions()
        if saved:
            self.account.positions = saved
        latest = self.db.load_latest_equity()
        if latest:
            self.account.cash = float(latest.get("cash", self.account.cash))
        self.strategy = create_strategy(strategy, topk=topk, rebalance_every=rebalance)
        # 券商：None 表示用 PaperBroker（本地模拟撮合）
        self.broker = broker

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

        # 第一步 选股：策略原始信号（每次 run_once 视为一个调仓评估日，强制触发）
        self.strategy._since = max(0, self.strategy.rebalance_every - 1)
        raw_weights = self.strategy.generate_weights(last_date, factors, panel) or {}

        mom_df = factors.get("momentum20")
        selection = []
        for c in panel.codes:
            w = raw_weights.get(c, 0.0)
            if w <= 0:
                continue
            mom = None
            if mom_df is not None and c in mom_df.columns:
                v = mom_df.loc[last_date, c]
                mom = round(float(v) * 100, 2) if v == v else None
            selection.append({"code": c, "name": config.ETF_NAMES.get(c, c),
                              "weight": round(w, 4), "momentum20": mom})
        selection.sort(key=lambda x: -x["weight"])

        # 第二步 评估：绝对动量质检（20日动量≤0 视为无合适，剔除）+ 风控修正
        qualified = {}
        evaluation = []
        for s in selection:
            ok = s["momentum20"] is not None and s["momentum20"] > 0
            if ok:
                qualified[s["code"]] = raw_weights[s["code"]]
            evaluation.append(dict(s, qualified=ok,
                                   reason="" if ok else "20日动量≤0，剔除"))

        risk = RiskManager()
        prices = {c: float(panel.get("close").loc[last_date, c]) for c in panel.codes}
        weights = risk.filter_weights(qualified, self.account.positions, prices)
        empty = not bool(weights)   # 无合格标的 → 空仓

        # 第三步 下单（空仓时 ExecutionEngine 会自然清掉旧持仓）
        executor = ExecutionEngine(self.broker) if self.broker else ExecutionEngine()
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
            "final_weights": {c: round(w, 4) for c, w in weights.items()},
            "selection": selection,
            "evaluation": evaluation,
            "empty": empty,
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