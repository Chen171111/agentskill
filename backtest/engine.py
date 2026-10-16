"""回测引擎：逐日推进，调用策略产出目标权重并再平衡（含大盘择时 + 组合层风控）。"""
from typing import Optional

import numpy as np
import pandas as pd

import config
from .account import BacktestAccount
from dataprovider.store import classify_code
from risk.portfolio import PortfolioRisk

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
        self._timing_ma = None
        self._timing_mom = None
        self._timing_rsrs = None
        self._timing_bias = None
        if timing and benchmark is not None and len(benchmark) > timing_window:
            bc = benchmark.dropna()
            if timing == "ma20":
                self._timing_ma = bc.rolling(timing_window).mean()
            elif timing == "abs_mom":
                self._timing_mom = bc / bc.shift(timing_window) - 1
            elif timing == "rsrs":
                self._timing_rsrs = self._build_rsrs(
                    benchmark, benchmark_high, benchmark_low, timing_window)
            elif timing == "bias":
                # BIAS 温度计：大盘 20 日乖离率，用于多档仓位调度
                ma = bc.rolling(timing_window).mean()
                self._timing_bias = (bc - ma) / ma

    def _build_rsrs(self, close, high, low, window):
        if high is None or low is None:
            return None
        bh, bl = high.dropna(), low.dropna()
        if len(bh) < window + 2:
            return None
        hi, lo = bh.values, bl.values
        N = max(5, min(window, len(bh) - 2))
        beta = [np.nan] * len(bh)
        for i in range(N, len(bh)):
            beta[i] = np.polyfit(lo[i - N:i], hi[i - N:i], 1)[0]
        alpha = pd.Series(beta, index=bh.index)
        M = max(N * 2, 20)
        return (alpha - alpha.rolling(M).mean()) / alpha.rolling(M).std()

    def _timing_scale(self, date) -> float:
        if (self._timing_ma is None and self._timing_mom is None
                and self._timing_rsrs is None and self._timing_bias is None):
            return 1.0
        if self._timing_rsrs is not None:
            z = self._timing_rsrs.get(date)
            if z is None or z != z:
                return 1.0
            return 1.0 if z > 0 else self.timing_scale_off
        if self._timing_bias is not None:
            # BIAS 温度计多档仓位（聚宽"坦克300"思想）：
            # 大盘极端深跌→空仓；均线下方→减仓；均线上方→满仓
            b = self._timing_bias.get(date)
            if b is None or b != b:
                return 1.0
            if b <= -0.08:
                return 0.0
            if b <= -0.05:
                return 0.3
            if b <= 0:
                return 0.5
            return 1.0
        if self._timing_ma is not None:
            c = self._timing_ma.get(date)
            if c is None or c != c:
                return 1.0
            return 1.0 if self.benchmark.get(date, 0) > c else self.timing_scale_off
        if self._timing_mom is not None:
            c = self._timing_mom.get(date)
            if c is None or c != c:
                return 1.0
            return 1.0 if c > 0 else self.timing_scale_off
        return 1.0

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
        close = self.panel.get("close")
        open_ = self.panel.get("open")
        volume = self.panel.get("volume")
        prev_close = close.shift(1) if close is not None else None

        bench_nav = None
        if self.benchmark is not None:
            bc = self.benchmark
            b0 = bc.loc[dates].dropna()
            bench_nav = b0 / b0.iloc[0]

        holdings = {}
        pending = None  # 上一交易日收盘决策的目标权重，今日开盘成交
        for date in dates:
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