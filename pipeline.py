"""回测流水线：数据→因子→策略→回测→绩效，供 CLI 复用。"""
import config
from dataprovider.store import DataStore, validate_tradeable
from dataprovider.panel import build_panel
from factors.engine import compute_factors
from strategies.registry import create_strategy
from backtest.engine import BacktestEngine
from analysis.metrics import compute_metrics

DEFAULT_FACTORS = ["rsi", "macd_hist", "bias20", "sma_gap",
                   "momentum5", "momentum20", "momentum60", "momentum120", "momentum250",
                   "vol_ratio", "zt_daily", "lianban"]


def _shift_date(yyyymmdd, days):
    """'YYYYMMDD' 字符串偏移 days 自然日。"""
    from datetime import datetime, timedelta
    d = datetime.strptime(str(yyyymmdd), "%Y%m%d")
    return (d + timedelta(days=days)).strftime("%Y%m%d")


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
    validate_tradeable(codes)   # 指数不可直接交易，前置拦截
    store = DataStore()
    store.ensure(codes)

    # 因子 warmup：起点前预留 ~400 自然日（≈260 交易日），避免长周期因子(momentum250等)初期为 NaN
    warmup_start = _shift_date(start, -400) if start else None
    panel_full = build_panel(store, codes, start=warmup_start, end=end)
    if len(panel_full.dates) < 30:
        raise ValueError("有效交易日过少（{} 天），请检查标的或区间".format(len(panel_full.dates)))
    factors_full = compute_factors(panel_full, DEFAULT_FACTORS)
    panel = panel_full.slice(start, end)
    factors = {k: v.reindex(panel.dates) for k, v in factors_full.items()}

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