"""调度层：每个交易日收盘后自动运行一次交易闭环。

DailyRunner.run_once() 是自动化交易的核心入口：
    数据 → 因子 → 策略信号 → 风控 → 执行订单 → 持久化
Scheduler 负责按 SCHEDULE.run_time 每日定时触发（收盘后）。
"""
import time
from datetime import datetime, date
from pathlib import Path

import config
from dataprovider.store import DataStore, validate_tradeable
from dataprovider.panel import build_panel
from factors.engine import compute_factors
from strategies.registry import create_strategy
from risk.manager import RiskManager
from account.portfolio import PortfolioAccount
from trader.execution import ExecutionEngine
from trader.broker import ThsBroker
from storage.db import TradeDB

DEFAULT_FACTORS = ["rsi", "macd_hist", "bias20", "sma_gap",
                   "momentum5", "momentum10", "momentum20",
                   "momentum60", "momentum120", "momentum250",
                   "vol_ratio", "zt_daily", "lianban"]


def _filter_kwargs(strategy_name: str, **kwargs) -> dict:
    """只保留目标策略构造函数真正接受的参数。

    为什么需要：`trend_window` 只有动量族（`momentum` / `etf_rotation` / …）支持，
    其它策略（`mean_reversion` 等）没有。硬传会 `TypeError`。
    """
    import inspect
    from strategies.registry import STRATEGIES
    cls = STRATEGIES.get(strategy_name)
    if cls is None:
        return kwargs
    try:
        params = set(inspect.signature(cls.__init__).parameters)
    except (TypeError, ValueError):
        return kwargs
    if any(p.kind == inspect.Parameter.VAR_KEYWORD
           for p in inspect.signature(cls.__init__).parameters.values()):
        return kwargs                      # 有 **kw，全收
    return {k: v for k, v in kwargs.items() if k in params}


class DailyRunner:
    """单次交易日运行器。

    profile：账本实例名。传入后使用独立 DB（state/trading_{profile}.db），
    用于并行运行多套互不干扰的策略组合（如防守 etf_rotation + 进攻 tech_offensive）。
    """

    def __init__(self, codes, strategy="momentum", topk=5, rebalance=5, timing=None,
                 init_cash=None, broker=None, profile=None, dry_run=False):
        validate_tradeable(codes)   # 指数不可直接交易，前置拦截
        self.codes = codes
        self.strategy_name = strategy
        self.topk = topk
        self.rebalance = rebalance
        self.timing = timing
        self.profile = profile
        self.dry_run = dry_run      # True 时不落库、不下单（配合 DryRunBroker 试算）
        self.store = DataStore()
        if profile:
            db_path = str(Path(config.DB_PATH).with_name(
                "trading_{}.db".format(profile)))
        else:
            db_path = None
        self.db = TradeDB(db_path=db_path)
        self.account = PortfolioAccount(init_cash=init_cash)
        # 恢复历史持仓与现金（若存在）
        saved = self.db.load_positions()
        if saved:
            self.account.positions = saved
        latest = self.db.load_latest_equity()
        if latest:
            self.account.cash = float(latest.get("cash", self.account.cash))
        self.strategy = create_strategy(strategy, **_filter_kwargs(
            strategy, topk=topk, rebalance_every=rebalance,
            trend_window=config.TREND_LIVE))
        self._shadow = None          # 影子策略（并行验证用，惰性创建）
        # 恢复调仓计数（跨运行持久化，与回测「每 N 日调仓」口径一致）
        saved_since = self.db.get_state("strategy_since")
        if saved_since is not None:
            try:
                self.strategy._since = int(saved_since)
            except (TypeError, ValueError):
                pass
        # 券商：None 表示用 PaperBroker（本地模拟撮合）
        self.broker = broker

    # ---------------- 并行验证（shadow）----------------
    def _shadow_weights(self, date, factors, panel, since_before):
        """用「关闭 Faber 趋势过滤」的策略在同一份面板上算一遍目标权重。

        ⚠️ 为什么要在**同一个调仓日**重算，而不是事后回放：
        策略的调仓计数器 `_since` 是跨运行持久化的。影子策略必须从**与实盘相同的
        计数器值**出发，否则两边的调仓相位会错开，比较就失去意义
        （这正是 `tools/README.md` 铁律 7② 的坑）。
        """
        if not getattr(config, "SHADOW_ENABLED", False):
            return None, ""
        try:
            if self._shadow is None:
                self._shadow = create_strategy(self.strategy_name, **_filter_kwargs(
                    self.strategy_name,
                    topk=getattr(self.strategy, "topk", config.DEFAULT_TOP_K),
                    rebalance_every=getattr(self.strategy, "rebalance_every",
                                            config.DEFAULT_REBALANCE),
                    trend_window=config.TREND_SHADOW))
            self._shadow._since = since_before
            w = self._shadow.generate_weights(date, factors, panel)
            return (w or {}), "trend_window={}".format(config.TREND_SHADOW)
        except Exception as e:
            print("[runner] 影子策略计算失败（不影响实盘）：{}".format(e))
            return None, ""

    def _record_shadow(self, date, factors, panel, since_before, live_weights):
        """把「实盘配置」与「影子配置」的目标权重都落库，供事后比较真实前向表现。"""
        if not getattr(config, "SHADOW_ENABLED", False):
            return
        try:
            sh, note = self._shadow_weights(date, factors, panel, since_before)
            if sh is None:
                return
            self.db.save_shadow_weights(
                date, "live", live_weights,
                "trend_window={}".format(config.TREND_LIVE))
            self.db.save_shadow_weights(date, "shadow", sh, note)
            print("[runner] 并行验证已记录 {}：实盘 {} 只 / 影子 {} 只".format(
                date, len(live_weights), len(sh)))
        except Exception as e:
            print("[runner] 影子记录失败（不影响实盘）：{}".format(e))

    def run_once(self, end: str = None) -> dict:
        """执行一个交易日的完整闭环。end 传 None 则用最新数据。"""
        # 先补缺，再强制增量刷新缓存到「最近已收盘交易日」，杜绝用陈旧数据下单
        self.store.ensure(self.codes)
        self.store.refresh(self.codes)
        end = end or datetime.now().strftime("%Y%m%d")
        # 取最近 200 个交易日用于计算因子
        start = _n_days_ago(end, 200)
        panel = build_panel(self.store, self.codes, start=start, end=end)
        if len(panel.dates) == 0:
            return {"status": "no_data"}

        factors = compute_factors(panel, DEFAULT_FACTORS)
        last_date = panel.dates[-1]
        prices = {c: float(panel.get("close").loc[last_date, c]) for c in panel.codes}

        # 【数据新鲜度守卫】行情必须已刷新到最近一个「已收盘交易日」。
        # 为什么要守：32 位 Python 装不了 akshare，刷数据必须由 64 位 Python 前置完成
        # （tools/refresh_data.py）。若前置步骤失败，缓存会停在旧日期，而 refresh() 只会
        # 静默跳过下载 —— 结果是拿过期信号下单。宁可拒单，也不能用陈旧数据交易。
        try:
            from dataprovider.calendar import latest_closed_trading_day
            expected = latest_closed_trading_day()
            if str(last_date) < str(expected):
                raise RuntimeError(
                    "行情数据陈旧：最新 {}，应为 {}。请先用 64 位 Python 执行 "
                    "tools/refresh_data.py 刷新数据。拒绝在陈旧数据上下单。".format(
                        last_date, expected))
        except RuntimeError:
            raise
        except Exception as e:
            print("[runner] 数据新鲜度校验跳过：{}".format(e))

        # 【下单前必做】用真实券商对账，校正本地账户。
        # 放在算出行情价之后，是为了把 prices 交给 reconcile 做持仓校验——
        # 持仓表是 OCR 读的，会读串列（实测出现过成本价 8950 其实是 8.950），
        # 只有用可靠的行情价交叉核对才能识别坏数据。
        # 历史坑：本地 DB 可能残留早先 PaperBroker 的「幽灵持仓」，
        # 若不对账就下单，会拿不存在的持仓去卖 → 废单/误判。
        if self.broker is not None and hasattr(self.broker, "reconcile"):
            try:
                ok = self.broker.reconcile(self.account, prices)
            except TypeError:
                ok = self.broker.reconcile(self.account)
            except Exception as e:
                raise RuntimeError("下单前对账异常，拒绝在未知账本上下单: {}".format(e))
            if not ok:
                raise RuntimeError("下单前对账失败（持仓/资金读取未通过校验），拒绝下单")
            print("[runner] 下单前对账完成：现金 {:.2f} 冻结 {:.2f} 持仓 {}".format(
                self.account.cash, self.account.frozen,
                {k: v.get("qty") for k, v in self.account.positions.items()}))

        # 第一步 选股：仅在调仓日触发（与回测「每 N 日调仓」口径一致，非调仓日返回 None）
        since_before = self.strategy._since      # 影子策略必须从同一计数器出发
        raw_weights = self.strategy.generate_weights(last_date, factors, panel)
        if not self.dry_run:
            # 试算（dry_run）不得推进调仓计数器，否则几次试算就把「调仓日」提前了
            self.db.set_state("strategy_since", str(self.strategy._since))

        if raw_weights is None:
            # 非调仓日：不重新选股、不清仓，仅记录当日净值
            snap = self.account.snapshot(prices)
            if not self.dry_run:
                self.db.set_state("strategy_since", str(self.strategy._since))
                self.db.save_equity(last_date, snap["cash"], snap["market_value"],
                                    snap["total_equity"])
                self.db.save_positions(self.account.positions)
            return {
                "status": "ok", "date": last_date, "strategy": self.strategy_name,
                "rebalanced": False, "target_weights": {}, "final_weights": {},
                "selection": [], "evaluation": [], "empty": True,
                "orders": [], "account": snap, "dry_run": self.dry_run,
            }

        raw_weights = raw_weights or {}   # 调仓日（空 dict 表示清仓）

        # 【并行验证】同一调仓日记录「实盘配置」与「影子配置」两套目标权重。
        # 只记录、不执行 —— 实盘行为完全不变（HANDOFF §三 第 2 项：Faber 开/关）。
        if not self.dry_run:
            self._record_shadow(last_date, factors, panel, since_before, raw_weights)

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
        # 组合级风控净值历史：DB 历史净值 + 当日总资产（接入回撤熔断 + 波动率目标）
        nav_history = self.db.load_equity_history()
        nav_history.append(self.account.total_equity(prices))
        weights = risk.filter_weights(qualified, self.account.positions, prices,
                                     nav_history=nav_history,
                                     enforce_stops=getattr(self.strategy, "stops_enabled", True))
        empty = not bool(weights)   # 无合格标的 → 空仓

        # 第三步 下单（空仓时 ExecutionEngine 会自然清掉旧持仓）
        executor = ExecutionEngine(self.broker) if self.broker else ExecutionEngine()
        orders = executor.rebalance(self.account, weights, prices)

        # 账本对账（同花顺模式）：回读真实持仓/资金校正本地账户，杜绝双账本
        if self.broker is not None and hasattr(self.broker, "reconcile"):
            try:
                try:
                    self.broker.reconcile(self.account, prices)
                except TypeError:
                    self.broker.reconcile(self.account)
            except Exception as e:
                print("[runner] 对账失败: {}".format(e))

        # 回读真实成交，修正订单状态。
        # 真实券商（同花顺）submit() 只返回 submitted，实际成交/部分成交/未成交
        # 必须回读当日委托才能知道，否则台账里全是「已报 0 股」。
        if self.broker is not None and hasattr(self.broker, "sync_fill"):
            for o in orders:
                try:
                    self.broker.sync_fill(o)
                except Exception:
                    pass

        # 持久化（dry_run 时全部跳过，保证试算零副作用）
        if not self.dry_run:
            for o in orders:
                self.db.save_order(o)
            self.db.set_state("strategy_since", str(self.strategy._since))
            self.db.save_positions(self.account.positions)
        snap = self.account.snapshot(prices)
        if not self.dry_run:
            self.db.save_equity(last_date, snap["cash"], snap["market_value"],
                                snap["total_equity"])

        return {
            "status": "ok",
            "date": last_date,
            "strategy": self.strategy_name,
            "rebalanced": True,
            "dry_run": self.dry_run,
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
        """A股交易日：接入 akshare 交易日历，识别节假日（替代粗糙的 weekday 判断）。"""
        from dataprovider.calendar import is_trading_day
        return is_trading_day(date.today())

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