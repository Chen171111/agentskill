"""验证：价值/红利因子(低PE/低PB)在近5年是否仍有超额（样本外检验）。

在纯动量20基础上，分别叠加"低PE(TTM)"和"低PE+低PB"，看是否提升风险调整后收益。
数据源：akshare 百度估值历史(市盈率TTM / 市净率)，缓存到 data/valuation/。
用法：python validate_value_factor.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

import config
from dataprovider.store import DataStore, fetch_index_cons_sample
from dataprovider.panel import build_panel
from factors import engine as fe
from factors.registry import register_factor
from strategies.registry import create_strategy
from backtest.engine import BacktestEngine
from analysis.metrics import compute_metrics, format_metrics

VALU_DIR = config.DATA_DIR / "valuation"
INDICATORS = {"pe_ttm": "市盈率(TTM)", "pb": "市净率"}

# 沪深300成分股抽样失败时的内置备选池（覆盖价值/红利股与成长股，跨行业）
_FALLBACK = [
    "600036.SH", "601318.SH", "600519.SH", "000858.SZ", "600276.SH",
    "300760.SZ", "000333.SZ", "600690.SH", "600900.SH", "601088.SH",
    "600028.SH", "600030.SH", "688981.SH", "300750.SZ", "601012.SH",
    "002475.SZ", "600048.SH", "000725.SZ", "601899.SH", "600104.SH",
    "601006.SH", "601633.SH",
]


def _fetch(code, indicator):
    import akshare as ak
    base = code.split(".")[0]
    df = ak.stock_zh_valuation_baidu(symbol=base, indicator=indicator, period="全部")
    if df is None or df.empty:
        return None
    df = df.rename(columns={"date": "date", "value": "val"})
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d")
    s = df.set_index("date")["val"]
    return s[~s.index.duplicated(keep="last")].sort_index()


def load_valuation(codes, key):
    VALU_DIR.mkdir(parents=True, exist_ok=True)
    frames = {}
    for code in codes:
        p = VALU_DIR / "{}_{}.csv".format(code, key)
        if p.exists():
            s = pd.read_csv(p, dtype={"date": str}).set_index("date")["val"]
        else:
            try:
                s = _fetch(code, INDICATORS[key])
            except Exception as e:
                print("[skip] {} {}: {}".format(code, key, str(e)[:80]))
                continue
            if s is None:
                print("[skip] {} {}: 无数据".format(code, key))
                continue
            s.rename("val").to_csv(p)
            time.sleep(0.15)
        if s is not None and len(s):
            frames[code] = s
    if not frames:
        return None
    return pd.DataFrame(frames).sort_index()


def run_one(codes, specs, label, start, end, topk=5, rebalance=5):
    store = DataStore()
    store.ensure(codes)
    panel = build_panel(store, codes, start=start, end=end)

    # 塞入估值字段（date × code），对齐交易日
    for key in INDICATORS:
        v = load_valuation(codes, key)
        if v is not None:
            panel.fields[key] = v.reindex(panel.dates)

    # 注册"原始值即因子"的价值因子（横截面排序由 weighted_score 完成）
    if "pe_ttm" not in fe._NEED_FIELDS:
        register_factor("pe_ttm", lambda px: px["pe_ttm"])
        register_factor("pb", lambda px: px["pb"])
        fe._NEED_FIELDS["pe_ttm"] = ["pe_ttm"]
        fe._NEED_FIELDS["pb"] = ["pb"]

    # 仅用面板里真正存在的因子（估值数据缺失时自动退化）
    avail = ["momentum20"] + [k for k in INDICATORS if k in panel.fields]
    specs = [sp for sp in specs if sp[0] in avail]

    factors = fe.compute_factors(panel, avail)
    strat = create_strategy("multifactor", topk=topk, rebalance_every=rebalance,
                            specs=specs)

    bench_code = "000300.SH"
    store.ensure([bench_code])
    bench_df = store.read(bench_code, start=start, end=end)
    bench_close = bench_df["close"].reindex(panel.dates)

    engine = BacktestEngine(panel, factors, strat, benchmark=bench_close,
                            dd_circuit=False, vol_target=False)
    result = engine.run()
    metrics = compute_metrics(result.equity, result.benchmark)

    print("\n===== {} =====".format(label))
    print("池子数量: {}，因子: {}".format(len(codes), [(s[0], s[1]) for s in specs]))
    print(format_metrics(metrics))
    return metrics


def main():
    start = "20200101"
    end = "20251231"
    try:
        codes = fetch_index_cons_sample(n=25)
        print("[pool] 自动抽样 {} 只沪深300成分股".format(len(codes)))
    except Exception as e:
        print("[warn] 自动拉成分股失败({})，改用内置备选池".format(str(e)[:80]))
        codes = list(_FALLBACK)
    codes = [c for c in codes if c]

    groups = [
        ("A 纯动量20", [("momentum20", 1, 1.0)]),
        ("B 动量+低PE", [("momentum20", 1, 1.0), ("pe_ttm", -1, 0.5)]),
        ("C 动量+低PE+低PB",
         [("momentum20", 1, 1.0), ("pe_ttm", -1, 0.4), ("pb", -1, 0.4)]),
    ]
    results = {}
    for label, specs in groups:
        results[label] = run_one(codes, specs, label, start, end)

    print("\n\n================ 汇总对比 ================")
    row_keys = ["年化收益", "最大回撤", "夏普比率", "卡玛比率", "基准年化收益", "超额收益"]
    print("{:<22}" + "".join("{:>16}".format(g[0]) for g in groups))
    for k in row_keys:
        line = "{:<22}".format(k)
        for label, _ in groups:
            v = results[label].get(k, float("nan"))
            line += "{:>16.2f}".format(v)
        print(line)


if __name__ == "__main__":
    main()