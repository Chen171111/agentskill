"""回测流水线：数据→因子→策略→回测→绩效，供 CLI 复用。"""
import config
from dataprovider.store import DataStore
from dataprovider.panel import build_panel
from factors.engine import compute_factors
from strategies.registry import create_strategy
from backtest.engine import BacktestEngine
from analysis.metrics import compute_metrics

DEFAULT_FACTORS = ["rsi", "macd_hist", "bias20", "sma_gap", "momentum20", "vol_ratio",
                   "zt_daily", "lianban"]


def run_backtest(codes, strategy="momentum", start=None, end=None,
                 init_cash=None, topk=5, rebalance=5, benchmark=None,
                 timing=None, timing_window=20, timing_scale_off=0.3,
                 dd_circuit=None, vol_target=None, strategy_params=None,
                 stability_min_overlap=None):
    # 风控参数未显式传入时，走 config 默认方案（默认为熔断+波动率目标15%）
    if dd_circuit is None:
        dd_circuit = config.DEFAULT_DD_CIRCUIT
    if vol_target is None:
        vol_target = config.DEFAULT_VOL_TARGET
    store = DataStore()
    store.ensure(codes)

    panel = build_panel(store, codes, start=start, end=end)
    if len(panel.dates) < 30:
        raise ValueError("有效交易日过少（{} 天），请检查标的或区间".format(len(panel.dates)))

    factors = compute_factors(panel, DEFAULT_FACTORS)

    kw = dict(strategy_params or {})
    kw.setdefault("topk", topk)
    kw.setdefault("rebalance_every", rebalance)
    strat = create_strategy(strategy, **kw)

    bench_code = benchmark or codes[0]
    bench_df = store.read(bench_code, start=start, end=end)
    bench_close = bench_df["close"].reindex(panel.dates)

    engine = BacktestEngine(
        panel, factors, strat, init_cash=init_cash,
        benchmark=bench_close, timing=timing, timing_window=timing_window,
        timing_scale_off=timing_scale_off,
        benchmark_high=bench_df["high"].reindex(panel.dates) if "high" in bench_df else None,
        benchmark_low=bench_df["low"].reindex(panel.dates) if "low" in bench_df else None,
        dd_circuit=dd_circuit, vol_target=vol_target,
        stability_min_overlap=stability_min_overlap,
    )
    result = engine.run()
    metrics = compute_metrics(result.equity, result.benchmark)

    return {
        "result": result,
        "metrics": metrics,
        "meta": {
            "strategy": strategy,
            "codes": codes,
            "start": panel.dates[0],
            "end": panel.dates[-1],
        },
    }