"""绩效分析：年化收益、夏普、最大回撤、胜率等。"""
import numpy as np
import pandas as pd


def annualized_return(equity: pd.Series) -> float:
    if len(equity) < 2:
        return 0.0
    years = len(equity) / 252.0
    total = equity.iloc[-1] / equity.iloc[0]
    return total ** (1.0 / years) - 1.0 if years > 0 else 0.0


def max_drawdown(equity: pd.Series) -> float:
    if len(equity) == 0:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def sharpe_ratio(returns: pd.Series, rf: float = 0.0) -> float:
    if len(returns) < 2 or returns.std() == 0:
        return 0.0
    return float((returns.mean() - rf) / returns.std() * np.sqrt(252))


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
        "年化波动": (returns.std() * np.sqrt(252) * 100) if len(returns) else 0.0,
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
    lines = []
    for k, v in m.items():
        lines.append("{:<10}: {:>8.2f}".format(k, v))
    return "\n".join(lines)