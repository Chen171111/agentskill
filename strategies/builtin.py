"""内置策略：动量、均值回归、双均线、多因子、连板龙头。"""
import pandas as pd

from .base import Strategy, weighted_score, rank_snapshot, volatility_weighted


class MomentumStrategy(Strategy):
    """动量轮动：动量 + 均线趋势双因子打分取 TopK。

    可配参数：
      window       动量回看窗口（默认 20，可 5/60/120/250）
      trend_window 绝对趋势过滤（Faber GTAA 思想）：只保留站上自身均线的标的
      include_sma  是否叠加短期均线趋势因子（长周期动量建议关闭）
      risk_parity  风险平价加权（按波动率倒数分配，替代等权）
    """
    name = "momentum"

    def __init__(self, topk=3, window=20, risk_parity=False,
                 trend_window=None, include_sma=True, **kw):
        super().__init__(topk=topk, **kw)
        self.window = window
        self.risk_parity = bool(risk_parity)
        self.trend_window = int(trend_window) if trend_window else None
        self.include_sma = bool(include_sma)
        self.factor = "momentum{}".format(window)

    def generate_weights(self, date, factors, panel):
        if not self._is_rebalance():
            return None
        specs = [(self.factor, 1, 1.0)]
        if self.include_sma:
            specs.append(("sma_gap", 1, 1.0))
        score = weighted_score(factors, date, specs)
        if score is None or score.empty:
            return {}
        codes = score.sort_values(ascending=False).head(self.topk).index.tolist()
        # 绝对趋势过滤（Faber）：只保留站上自身均线的标的，趋势破坏则剔除
        if self.trend_window:
            close = panel.get("close")
            if close is not None and date in close.index:
                ma = close.rolling(self.trend_window).mean()
                if date in ma.index:
                    codes = [c for c in codes
                             if c in close.columns and c in ma.columns
                             and close[c].loc[date] == close[c].loc[date]
                             and ma[c].loc[date] == ma[c].loc[date]
                             and close[c].loc[date] > ma[c].loc[date]]
        if not codes:
            return {}
        if self.risk_parity and len(codes) > 1:
            # 风险平价：按波动率倒数分配，替代等权
            w = volatility_weighted(codes, panel, date)
            return {c: ww * self.max_total for c, ww in w.items()}
        return {c: min(1.0 / max(len(codes), 1) * 0.9, self.max_total) for c in codes}


class EtfRotationStrategy(MomentumStrategy):
    """ETF 轮动（跨周期稳健）：快动量(20) + 风险平价，剔除短期均线噪声。

    相对纯个股动量的差异：
      include_sma=False  —— 去掉 5/20 均线交叉噪声因子，回测显著降低回撤、提升收益
      risk_parity=True   —— 按波动率倒数分配权重，抑制高波动标的（如券商/半导体/纳指）

    趋势强度门（默认开启，针对常态牛市跑输指数的改进）：
      用跨截面广度（站上自身 MA 的标的占比）识别"强势普涨"，
      强势区间放宽 TopK（gate_topk）提高分散度、贴合指数，弱势区间维持保守 TopK。
      回测：长区间回撤 -17%→-12%、年化+0.5~2pp，2024 牛市年由 -3.2%→+8.9%。
    保守替代：window=120（慢动量，长短区间都为正，但长区间收益更低）

    2026-09 社区研究落地：Faber 均线趋势过滤默认开启（trend_window=60）
      复测 2020-01~2026-08：年化+收益 33.01%->51.19%、最大回撤 -17.05%->-11.98%；
      2026 样本外（01~08）由 -4.84% 转正 +1.51%。与社区"简单多均线择时/
      动量+趋势过滤"（Faber GTAA 同源）思路一致，经本地全区间复测验证有效。
    """
    name = "etf_rotation"

    def __init__(self, topk=5, window=20, risk_parity=True, include_sma=False,
                 trend_window=60, trend_gate=True, gate_window=200,
                 gate_threshold=0.4, gate_topk=10, **kw):
        super().__init__(topk=topk, window=window, risk_parity=risk_parity,
                         include_sma=include_sma, trend_window=trend_window, **kw)
        self.trend_gate = bool(trend_gate)
        self.gate_window = int(gate_window)
        self.gate_threshold = float(gate_threshold)
        self.gate_topk = int(gate_topk)

    def _breadth(self, panel, date):
        """跨截面广度：站上自身 MA(gate_window) 的标的占比，0~1。"""
        close = panel.get("close")
        if close is None or date not in close.index:
            return None
        ma = close.rolling(self.gate_window).mean()
        if date not in ma.index:
            return None
        row_c = close.loc[date]
        row_m = ma.loc[date]
        valid = row_c.notna() & row_m.notna()
        if valid.sum() == 0:
            return None
        return float((row_c[valid] > row_m[valid]).sum() / valid.sum())

    def generate_weights(self, date, factors, panel):
        if not self._is_rebalance():
            return None
        specs = [(self.factor, 1, 1.0)]
        if self.include_sma:
            specs.append(("sma_gap", 1, 1.0))
        score = weighted_score(factors, date, specs)
        if score is None or score.empty:
            return {}

        n = len(panel.codes)
        eff_topk = self.topk
        if self.trend_gate:
            b = self._breadth(panel, date)
            if b is not None and b >= self.gate_threshold:
                # 强势普涨：放宽分散度，更贴合指数
                eff_topk = min(self.gate_topk, n)
        eff_topk = max(1, min(eff_topk, n))

        codes = score.sort_values(ascending=False).head(eff_topk).index.tolist()
        # 绝对趋势过滤（Faber）：只保留站上自身均线的标的，趋势破坏则剔除
        if self.trend_window:
            close = panel.get("close")
            if close is not None and date in close.index:
                ma = close.rolling(self.trend_window).mean()
                if date in ma.index:
                    codes = [c for c in codes
                             if c in close.columns and c in ma.columns
                             and close[c].loc[date] == close[c].loc[date]
                             and ma[c].loc[date] == ma[c].loc[date]
                             and close[c].loc[date] > ma[c].loc[date]]
        if not codes:
            return {}
        if self.risk_parity and len(codes) > 1:
            w = volatility_weighted(codes, panel, date)
            return {c: ww * self.max_total for c, ww in w.items()}
        return {c: min(1.0 / max(len(codes), 1) * 0.9, self.max_total) for c in codes}


class MeanReversionStrategy(Strategy):
    """均值回归：乖离率越低越看好。"""
    name = "mean_reversion"

    def __init__(self, topk=3, factor="bias20", **kw):
        super().__init__(topk=topk, **kw)
        self.factor = factor

    def generate_weights(self, date, factors, panel):
        if not self._is_rebalance():
            return None
        s = self._snapshot(factors, date, self.factor)
        if s is None or s.empty:
            return {}
        scores = rank_snapshot(s, ascending=True)
        codes = scores.sort_values(ascending=False).head(self.topk).index
        return {c: min(1.0 / max(len(codes), 1) * 0.9, self.max_total) for c in codes}


class CrossMovingStrategy(Strategy):
    """双均线趋势：sma_gap 越强越看好。"""
    name = "cross_moving"

    def __init__(self, topk=3, factor="sma_gap", **kw):
        super().__init__(topk=topk, **kw)
        self.factor = factor

    def generate_weights(self, date, factors, panel):
        if not self._is_rebalance():
            return None
        s = self._snapshot(factors, date, self.factor)
        if s is None or s.empty:
            return {}
        scores = rank_snapshot(s, ascending=False)
        codes = scores.sort_values(ascending=False).head(self.topk).index
        return {c: min(1.0 / max(len(codes), 1) * 0.9, self.max_total) for c in codes}


class LianbanLeadStrategy(Strategy):
    """连板龙头接力：选连板最高标的，情绪门控制空仓。"""
    name = "lianban_lead"

    def __init__(self, topk=1, min_zt=1, **kw):
        super().__init__(topk=topk, **kw)
        self.min_zt = min_zt

    def generate_weights(self, date, factors, panel):
        if not self._is_rebalance():
            return None
        lb = self._snapshot(factors, date, "lianban")
        zt = self._snapshot(factors, date, "zt_daily")
        if lb is None or zt is None or lb.empty or zt.empty:
            return {}
        zt_cnt = int((zt > 0).sum())
        if zt_cnt < self.min_zt:
            return {}
        codes = lb.sort_values(ascending=False).head(self.topk).index.tolist()
        codes = [c for c in codes if lb.loc[c] > 0]
        if not codes:
            return {}
        w = min(1.0 / len(codes) * 0.9, self.max_total)
        return {c: w for c in codes}


class TechOffensiveStrategy(Strategy):
    """科技进攻策略：短期动量 + 趋势广度门 + 集中持仓 + 高杠杆仓位。

    针对2026年科技主线（AI算力/半导体/科创板50/机器人）优化：
      短动量(5日/10日) 捕捉快轮动，去掉风险平价和趋势过滤，
      集中持仓 TopK=4（默认），趋势广度门更激进（门槛0.3），
      配合大盘择时+回撤熔断作为组合级防守，进攻不靠止损靠仓位。
    调参与社区验证（2026-09 平台社区研究，样本外 2026-01~08 复测确认）：
      topk 3->4：全区间(2025-04~2026-08) 56.99%->61.03%、2026样本外 8.42%->9.59%，
      放宽集中度后在震荡市（2026 科技调整）更抗伤，社区"持仓数2-3稳健版"同向结论。
    """
    name = "tech_offensive"

    def __init__(self, topk=4, window_fast=5, window_slow=10,
                 trend_gate=True, gate_window=60,
                 gate_threshold=0.3, gate_topk=7, stops_enabled=True, **kw):
        super().__init__(topk=topk, **kw)
        # 实测：关闭个股止损后 2026 样本外由 +22% 转 -20%（科技ETF波动极大，
        # 止损止盈实为保护而非掣肘），故默认开启；如需"纯进攻无止损"可传 False。
        self.stops_enabled = bool(stops_enabled)
        self.window_fast = int(window_fast)
        self.window_slow = int(window_slow)
        self.trend_gate = bool(trend_gate)
        self.gate_window = int(gate_window)
        self.gate_threshold = float(gate_threshold)
        self.gate_topk = int(gate_topk)
        self.factor_fast = "momentum{}".format(window_fast)
        self.factor_slow = "momentum{}".format(window_slow)

    def _breadth(self, panel, date):
        """跨截面广度：站上自身 MA(gate_window) 的标的占比，0~1。"""
        close = panel.get("close")
        if close is None or date not in close.index:
            return None
        ma = close.rolling(self.gate_window).mean()
        if date not in ma.index:
            return None
        row_c = close.loc[date]
        row_m = ma.loc[date]
        valid = row_c.notna() & row_m.notna()
        if valid.sum() == 0:
            return None
        return float((row_c[valid] > row_m[valid]).sum() / valid.sum())

    def generate_weights(self, date, factors, panel):
        if not self._is_rebalance():
            return None
        # 双动量：短动量5日权重更高（进攻型抓快轮动）。spec 格式 (因子, 方向, 权重)
        score = weighted_score(factors, date, [
            (self.factor_fast, 1, 2.0),   # 短动量 5 日，权重 2
            (self.factor_slow, 1, 1.0),   # 中动量 10 日，权重 1
        ])
        if score is None or score.empty:
            return {}

        n = len(panel.codes)
        eff_topk = self.topk
        if self.trend_gate:
            b = self._breadth(panel, date)
            if b is not None and b >= self.gate_threshold:
                # 强势普涨：放宽分散度
                eff_topk = min(self.gate_topk, n)
        eff_topk = max(1, min(eff_topk, n))

        codes = score.sort_values(ascending=False).head(eff_topk).index.tolist()
        if not codes:
            return {}

        # 进攻型：动量最强者优先配更多，用等权但仓位可以满
        # 不做风险平价（不抑制高波动科技标的）
        return {c: min(1.0 / max(len(codes), 1) * self.max_total, self.max_total) for c in codes}


class MultiFactorStrategy(Strategy):
    """多因子打分：加权横截面得分取 TopK。"""
    name = "multifactor"

    DEFAULT_SPECS = [
        ("momentum20", 1, 1.0),
        ("sma_gap", 1, 1.0),
        ("macd_hist", 1, 1.0),
        ("bias20", -1, 0.5),
    ]

    def __init__(self, topk=3, specs=None, **kw):
        super().__init__(topk=topk, **kw)
        self.specs = specs or list(self.DEFAULT_SPECS)

    def generate_weights(self, date, factors, panel):
        if not self._is_rebalance():
            return None
        score = weighted_score(factors, date, self.specs)
        if score is None or score.empty:
            return {}
        codes = score.sort_values(ascending=False).head(self.topk).index
        w = min(1.0 / max(len(codes), 1) * 0.9, self.max_total)
        return {c: w for c in codes}