"""回测引擎：逐日推进，调用策略产出目标权重并再平衡（含大盘择时 + 组合层风控）。"""
from typing import Optional

import numpy as np
import pandas as pd

import config
from .account import BacktestAccount
from dataprovider.store import classify_code
from risk.portfolio import PortfolioRisk
from risk.timing import timing_scale
from strategies.eligibility import filter_positive_momentum

_TRADING_DAYS = getattr(config, "TRADING_DAYS_PER_YEAR", 244)

# 回撤熔断常量已收敛到 risk/portfolio.py（2026-10-03）：此处原有一份逐字
# 副本，且经实测是死代码（本文件内无引用；真源 risk/portfolio.py，
# selftest 的熔断滞回检查也直接针对那边）。README 声称「回测与实盘共享
# 同一套」，常量必须只有一份。


class BacktestResult:
    def __init__(self, equity: pd.DataFrame, holdings: dict,
                 benchmark: Optional[pd.Series] = None):
        self.equity = equity
        self.holdings = holdings
        self.benchmark = benchmark


class BacktestEngine:
    def __init__(self, panel, factors, strategy, init_cash=None, benchmark=None,
                 timing=None, timing_window=20, timing_scale_off=0.3,
                 benchmark_high=None, benchmark_low=None,
                 dd_circuit=False, vol_target=None, stability_min_overlap=None):
        self.panel = panel
        self.factors = factors
        self.strategy = strategy
        self.acc = BacktestAccount(init_cash=init_cash)
        self.benchmark = benchmark
        self.timing = timing
        self.timing_window = timing_window
        self.timing_scale_off = timing_scale_off
        # 组合层风控（回撤熔断 + 波动率目标，与实盘共享 PortfolioRisk）
        self._p_risk = PortfolioRisk(dd_circuit=dd_circuit, vol_target=vol_target)
        # 个股级风控（止损/止盈/回撤止盈，与实盘 RiskManager 同构）
        _risk = config.RISK
        self.stop_loss = float(_risk.get("stop_loss", -0.08))
        self.take_profit = float(_risk.get("take_profit", 0.30))
        self.trailing_stop = float(_risk.get("trailing_stop", 0.15))
        # 信号稳定性过滤：本/上期 TopK 重叠度 < 阈值 → 空仓（BigQuant 信号稳定性思想）
        self.stability_min_overlap = (float(stability_min_overlap)
                                      if stability_min_overlap else None)
        self._last_codes = None                     # 上一调仓期的标的集合
        self._nav_history = []                      # 组合净值历史（用于组合层风控）
        self.benchmark_high = benchmark_high
        self.benchmark_low = benchmark_low

    def _timing_scale(self, date) -> float:
        close = self.panel.get("close_full")
        if close is None:
            close = self.panel.get("close")
        high = self.benchmark_high
        low = self.benchmark_low
        if high is None:
            high = self.panel.get("high_full")
            if high is None:
                high = self.panel.get("high")
        if low is None:
            low = self.panel.get("low_full")
            if low is None:
                low = self.panel.get("low")
        return timing_scale(self.timing, date, close, high, low,
                            benchmark=self.benchmark, window=self.timing_window,
                            scale_off=self.timing_scale_off)

    def _portfolio_risk_scale(self) -> float:
        """组合层风控（调仓日应用）：回撤熔断 + 波动率目标，返回 0~1 仓位系数（共享 PortfolioRisk）。"""
        return self._p_risk.scale(self._nav_history)

    def _row(self, df, date):
        """取某字段当日行，过滤 NaN，返回 {code: 值}。"""
        if df is None or date not in df.index:
            return {}
        r = df.loc[date]
        return {c: r[c] for c in r.index if r[c] == r[c]}

    # ---- 涨跌停分档已收敛到 tools/limit_rules.py（2026-10-03 单一来源）----
    # 本类不再持有分档字面量；ETF 双创名单/前缀、北交所、精度规则见该模块。

    @staticmethod
    def _limit_pct(code):
        """涨跌停幅度（委托单一来源 tools/limit_rules.limit_of）。"""
        from tools.limit_rules import limit_of
        return limit_of(code)

    def _blocked(self, date, open_px, prev_close, volume):
        """当日不可成交集合：停牌 + 一字涨停(买不进) / 一字跌停(卖不出)。"""
        buy_blocked, sell_blocked = set(), set()
        for code in self.panel.codes:
            vol = None
            if volume is not None and date in volume.index and code in volume.columns:
                vol = volume.loc[date, code]
            is_suspend = vol is None or vol != vol or vol == 0
            if is_suspend:
                buy_blocked.add(code)
                sell_blocked.add(code)
                continue
            limit = self._limit_pct(code)
            if limit is None:
                continue  # 指数等不可交易标的（ETF 现已按 10%/20% 分档建模）
            # 涨跌停价取整精度：股票 0.01 元、基金 0.001 元（单一来源 limit_rules）
            from tools.limit_rules import limit_price_round
            rnd = limit_price_round(code)
            pc = prev_close.loc[date, code] if prev_close is not None and date in prev_close.index else None
            if pc is None or pc != pc or pc <= 0:
                continue
            oc = open_px.get(code)
            if oc is None:
                continue
            if oc >= round(pc * (1 + limit), rnd):
                buy_blocked.add(code)
            if oc <= round(pc * (1 - limit), rnd):
                sell_blocked.add(code)
        return buy_blocked, sell_blocked

    def run(self) -> BacktestResult:
        dates = self.panel.dates
        close = self.panel.get("raw_close")
        if close is None:
            close = self.panel.get("close")
        open_ = self.panel.get("raw_open")
        if open_ is None:
            open_ = self.panel.get("open")
        volume = self.panel.get("volume")
        splits = self.panel.get("split_ratio")
        prev_close = close.shift(1) if close is not None else None
        if prev_close is not None and splits is not None:
            # Today's exchange limit reference is expressed in post-split units.
            prev_close = prev_close / splits.reindex_like(close).fillna(1.)

        bench_nav = None
        if self.benchmark is not None:
            bc = self.benchmark
            b0 = bc.loc[dates].dropna()
            bench_nav = b0 / b0.iloc[0]

        holdings = {}
        pending = None  # 上一交易日收盘决策的目标权重，今日开盘成交
        for date in dates:
            # Corporate actions precede next-open orders, including stale marks.
            self.acc.apply_splits(self._row(splits, date))
            close_px = self._row(close, date)
            open_px = self._row(open_, date)
            buy_blocked, sell_blocked = self._blocked(date, open_px, prev_close, volume)

            # 1) 今日开盘执行昨日决策（避免「当日收盘信号 + 当日收盘成交」的前视偏差）
            if pending is not None:
                self.acc.trade(pending, open_px, buy_blocked, sell_blocked)
                pending = None

            # 2) 今日收盘记账
            px = close_px or open_px
            if not px:
                holdings[date] = self.acc.holding()
                self._nav_history.append(self._nav_history[-1] if self._nav_history else self.acc.total({}))
                continue
            self.acc.mark_to_close(date, px)
            self._nav_history.append(self.acc.total(px))

            # 3) 今日收盘后决策（生成下一开盘的调仓目标）
            weights = self.strategy.generate_weights(date, self.factors, self.panel)
            if getattr(self.strategy, "positive_momentum_filter", False):
                weights = filter_positive_momentum(weights, self.factors, date)
            # 【单标的上限】2026-10-02 验收修复（P1-2）：策略的 max_weight 此前
            # 赋值后全仓无读取（死参数）——README §四 把「单标的上限」列为风控
            # 组件，但回测链路完全没有单标的约束，风险平价路径在标的少/低波动
            # 时单标的权重可远超 30%。实盘侧由 RiskManager.filter_weights 同款
            # clip（config.RISK.max_position_weight），此处对齐同一口径（单一来源）。
            if weights:
                _mw = float(config.RISK.get("max_position_weight", 0.30))
                weights = {c: min(w, _mw) for c, w in weights.items()}
            # None=非调仓日不动作；空 dict=调仓日清仓（趋势过滤全破位/情绪门空仓）
            if weights is not None:
                if self.stability_min_overlap is not None:
                    cur_codes = set(weights.keys())
                    if self._last_codes is not None:
                        overlap = len(cur_codes & self._last_codes) / max(len(cur_codes), 1)
                        if overlap < self.stability_min_overlap:
                            self._last_codes = cur_codes
                            pending = {}  # 信号不稳 → 下一开盘清仓
                            holdings[date] = self.acc.holding()
                            continue
                    self._last_codes = cur_codes
                scale = self._timing_scale(date) * self._portfolio_risk_scale()
                if scale < 1.0:
                    weights = {c: w * scale for c, w in weights.items()}
                pending = weights
            # 个股级止损/止盈/回撤止盈（与实盘 RiskManager 同构）：触发则下一开盘强平
            # 进攻型策略(stops_enabled=False)不做个股止损，仅靠组合级防守
            if getattr(self.strategy, "stops_enabled", True):
                stop_codes = self.acc.stop_loss_codes(px, self.stop_loss, self.take_profit, self.trailing_stop)
                if stop_codes:
                    # 2026-10-15 修复（P0：单只止损误清其他持仓）：
                    # 原实现在非调仓日只传 {止损标的: 0}，而 account.trade 把
                    # **未列出**的持仓也当成零目标卖出（target_mv.get(code, 0.0)）
                    # → 一只触发止损、整组清仓（内存复现：2 只持仓只清 1 只的
                    # 意图变成 2 只全清）。默认 etf_rotation 也启用个股止损。
                    # 现与实盘 runner 非调仓日止损检查同构（scheduler/runner.py
                    # 273-295 行：目标权重 = 持仓各标的市值权重、止损标的置 0）：
                    #   · 非调仓日：以**当日市值权重**为底 → 非止损标的
                    #     tgt_mv == cur_mv，整手化后不动它们，只强平止损标的；
                    #   · 调仓日：以当日调仓目标为底，止损标的置 0（原行为）。
                    if pending is not None:
                        base = dict(pending)
                    else:
                        equity = self.acc.total(px)
                        base = {}
                        if equity > 0:
                            for _c, _p in self.acc.positions.items():
                                _q = _p.get("qty", 0)
                                if _q <= 0:
                                    continue
                                # 用 _px_of（停牌兜底最后已知价）与 market_value
                                # 同口径，避免停牌持仓因缺席 px 被当成零目标卖出
                                base[_c] = _q * self.acc._px_of(_c, px) / equity
                    for c in stop_codes:
                        base[c] = 0.0
                    pending = base
            holdings[date] = self.acc.holding()

        return BacktestResult(self.acc.results(), holdings, bench_nav)