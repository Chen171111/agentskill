"""绩效分析：年化收益、夏普、最大回撤、胜率等。"""
import numpy as np
import pandas as pd

import config

_TRADING_DAYS = getattr(config, "TRADING_DAYS_PER_YEAR", 244)
_ANN = np.sqrt(_TRADING_DAYS)


def annualized_return(equity: pd.Series) -> float:
    """几何年化收益率：CAGR = (末值/初值)^(1/年数) - 1。"""
    if len(equity) < 2:
        return 0.0
    years = (len(equity) - 1) / float(_TRADING_DAYS)   # 交易日数 → 年数
    total = equity.iloc[-1] / equity.iloc[0]
    return total ** (1.0 / years) - 1.0 if years > 0 else 0.0


def max_drawdown(equity: pd.Series) -> float:
    """最大回撤（负值）。"""
    if len(equity) == 0:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def sharpe_ratio(returns: pd.Series, rf: float = 0.0) -> float:
    """年化夏普比率：(日均超额收益 / 日波动) × sqrt(年化交易日数)。"""
    if len(returns) < 2 or returns.std() == 0:
        return 0.0
    return float((returns.mean() - rf / _TRADING_DAYS) / returns.std() * _ANN)


def volatility(returns: pd.Series) -> float:
    """年化波动率：日收益标准差 × sqrt(年化交易日数)。"""
    if len(returns) < 2:
        return 0.0
    return float(returns.std() * _ANN)


def win_rate(returns: pd.Series) -> float:
    if len(returns) == 0:
        return 0.0
    return float((returns > 0).mean())


def compute_metrics(equity_df: pd.DataFrame, benchmark_nav: pd.Series = None) -> dict:
    equity = equity_df["equity"]
    value = equity_df["value"]
    returns = value.pct_change().dropna()

    total_return = (equity.iloc[-1] / equity.iloc[0] - 1.0) if len(equity) else 0.0
    m = {
        "累计收益": total_return * 100,
        "年化收益": annualized_return(equity) * 100,
        "最大回撤": max_drawdown(equity) * 100,
        "夏普比率": sharpe_ratio(returns),
        "年化波动": volatility(returns) * 100,
        "胜率": win_rate(returns) * 100,
    }
    if benchmark_nav is not None and len(benchmark_nav):
        bench_total = (benchmark_nav.iloc[-1] / benchmark_nav.iloc[0] - 1.0)
        bench_annual = annualized_return(benchmark_nav)
        bench_mdd = max_drawdown(benchmark_nav)
        m["基准累计收益"] = bench_total * 100
        m["基准年化收益"] = bench_annual * 100
        m["基准最大回撤"] = bench_mdd * 100
        m["超额收益"] = (total_return - bench_total) * 100
    return m


def format_metrics(m: dict) -> str:
    has_pct = ("收益", "回撤", "波动", "胜率")
    lines = []
    for k, v in m.items():
        suffix = "%" if any(t in k for t in has_pct) else ""
        lines.append("{:<10}: {:>8.2f}{}".format(k, v, suffix))
    return "\n".join(lines)