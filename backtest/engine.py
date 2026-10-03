"""回测引擎：逐日推进，调用策略产出目标权重并再平衡（含大盘择时 + 组合层风控）。"""
from typing import Optional

import numpy as np
import pandas as pd

import config
from .account import BacktestAccount
from dataprovider.store import classify_code
from risk.portfolio import PortfolioRisk

_TRADING_DAYS = getattr(config, "TRADING_DAYS_PER_YEAR", 244)

# 回撤熔断参数（带滞回的极端保险，只对深回撤反应，避免浅阈值反复"打脸"）
# 降仓档位：(回撤幅度阈值 → 仓位系数)，从深到浅排列
_CB_LEVELS = [(0.25, 0.0), (0.20, 0.40), (0.15, 0.65)]
_CB_TRIGGER = 0.15    # 进入熔断的回撤幅度（≥15% 才启动）
_CB_RECOVER = 0.10    # 退出熔断的回撤幅度（≤10% 才恢复，滞回带 5%）


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

    # ---- ETF 涨跌停分档（2026-10-02 验收修复 P1-1）----
    # 制度依据：docs/参考_A股ETF涨跌幅限制.md（沪深交易所交易规则一手梳理）。
    # ETF 默认 ±10%；「双创」ETF（跟踪指数成份股仅为创业板/科创板股票）±20%。
    # 深交所按「基金名单」公布适用 20% 的基金，代码前缀无法自动区分
    # （深市 ETF 全部 159 开头，创业板 ETF 与普通 ETF 混排）→ 显式名单维护；
    # 沪市科创板 ETF 统一 588/589 开头，按前缀判定。
    # ⚠️ 名单维护制：扩池纳入新的双创 ETF 若不在此名单，其 20% 涨停日会被按
    # 10% 档漏判（偏乐观）——扩池时必须同步补录（见 docs 同名文档 §2.2）。
    _ETF_LIMIT_20 = frozenset({
        "159915",   # 创业板ETF（易方达）
        "159780",   # 双创ETF（华夏）
        "159781",   # 双创ETF（招商）
    })
    _ETF_20_PREFIX = ("588", "589")   # 沪市科创板 ETF

    @classmethod
    def _limit_pct(cls, code):
        """涨跌停幅度：个股按板块分档；ETF 默认 10%、双创 20%（显式名单+前缀）。

        原实现对非个股一律返回 None（注释误写「ETF/基金无涨跌停」——与本项目
        自己的权威文档冲突），导致回测中 ETF 可在「一字涨停开盘」照常买入，
        可成交性被系统性高估、README 实测收益偏乐观。
        """
        kind = classify_code(code)
        base = code.upper().split(".")[0]
        if kind == "etf":
            if base.startswith(cls._ETF_20_PREFIX) or base in cls._ETF_LIMIT_20:
                return 0.20
            return 0.10
        if kind != "stock":
            return None   # 指数等不可交易标的（validate_tradeable 已前置拦截，双保险）
        cu = code.upper()
        if cu.startswith("688") or cu.startswith("300") or cu.startswith("301"):
            return 0.20   # 科创板/创业板
        return 0.10       # 主板

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
            # 涨跌停价四舍五入精度：股票 0.01 元、基金 0.001 元
            # （docs/参考_A股ETF涨跌幅限制.md §一：基金按 0.001 取整）。
            # 若 ETF 也按 2 位取整，门槛会被压低 → 9.9% 涨幅的合法开盘被误拦。
            rnd = 3 if classify_code(code) == "etf" else 2
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
                    pending = pending or {}
                    for c in stop_codes:
                        pending[c] = 0.0
            holdings[date] = self.acc.holding()

        return BacktestResult(self.acc.results(), holdings, bench_nav)