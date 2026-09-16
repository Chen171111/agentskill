"""通达信社区选股策略：批量回测筛选。

做什么
------
把 `tools/tdx_strategies.py` 里的社区策略逐条翻译后回测，与等权全池基准对比，
筛出**真正有效**的，剔除"看着像能赚钱"的。

口径
----
- 数据：`bars_all.parquet` + `universe_all.csv`（修正生存者偏差后的 5260 只）
- 区间：2019-01-01 ~ 2026-09-11
- 机制：复用 `backtest_stock.run()` 的 cond 模式 —— T+1 开盘成交、
  一字涨跌停不可成交、停牌不可成交、含佣金/印花税/滑点
- 持有期：信号触发后持有 `--hold` 个交易日（默认 5），到期换手

⚠️ 翻译偏差：部分公式含 CAPITAL / FINANCE / COST / INDEXC 等本机数据无法复现的
条件，已在 `tdx_strategies.py` 的 note 里逐条标注。回测结果只在**标注后的口径**下成立。

用法
----
    PY="C:/Users/XiaoQi/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
    $PY tools/sweep_tdx.py --bars data/stockbars/bars_all.parquet \
        --universe data/stockbars/universe_all.csv --hold 5
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import build_features, run, metrics  # noqa: E402
from tools.tdx_strategies import STRATEGIES, build_signal  # noqa: E402


def bench_metrics(df, start, end, min_listed=120, min_amount=3e7, min_price=2.0):
    rows = []
    for dt, g in df[(df.date >= start) & (df.date <= end)].groupby("date"):
        u = ((g.listed >= min_listed) & (g.amt_ma20 >= min_amount)
             & (g.close >= min_price) & (~g.suspended))
        rows.append((dt, float(g.loc[u, "ret1"].mean())))
    b = pd.DataFrame(rows, columns=["date", "r"]).set_index("date").dropna()
    return metrics((1 + b.r).cumprod())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260911")
    ap.add_argument("--hold", type=int, default=5)
    ap.add_argument("--keys", default=None,
                    help="只跑指定策略（逗号分隔的 key），默认全部")
    ap.add_argument("--min-amount", type=float, default=3e7,
                    help="20 日均成交额下限（元）。提高它 = 排除小盘股，"
                         "用于检验社区公式里 CAPITAL 流通盘限制的影响")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    out_path = args.out or f"results/tdx_hold{args.hold}.csv"

    bars = pd.read_parquet(args.bars)
    bars["date"] = bars.date.astype(str).str.replace("-", "", regex=False)
    if args.universe and os.path.exists(args.universe):
        u = pd.read_csv(args.universe, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
        bars = bars[~bars.code.isin(st)]
    print(f"日线: {len(bars):,} 行, {bars.code.nunique()} 只", flush=True)

    print("计算基础列（交易机制所需）…", flush=True)
    df = build_features(bars)

    ndays = df[(df.date >= args.start) & (df.date <= args.end)].date.nunique()
    years = ndays / 244.0
    print(f"区间 {args.start}~{args.end}  {ndays} 交易日 ({years:.1f} 年)\n", flush=True)

    bench = bench_metrics(df, args.start, args.end)
    print(f"基准 等权全池  年化 {bench['年化收益']:+.2f}%  "
          f"夏普 {bench['夏普比率']:.2f}  回撤 {bench['最大回撤']:.2f}%\n", flush=True)

    specs = STRATEGIES
    if args.keys:
        want = set(args.keys.split(","))
        specs = [s for s in specs if s["key"] in want]

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    rows = []
    print("=" * 126)
    print("  {:<22} {:<8} {:>8} {:>7} {:>7} {:>9} {:>6} {:>7} {:>9} {:>9}".format(
        "策略", "类别", "年化%", "波动%", "夏普", "回撤%", "卡玛", "持仓数",
        "交易笔数", "超额pp"))
    print("=" * 126)

    for spec in specs:
        t0 = time.time()
        key, name, cat = spec["key"], spec["name"], spec["cat"]
        try:
            sig = build_signal(df, key, hold=args.hold)
            col = f"_sig_{key}"
            df[col] = sig
            n_sig = int(sig.sum())
            eq, tr, meta = run(df, [], start=args.start, end=args.end,
                               hold=1, cond_col=col,
                               min_amount=args.min_amount)
            m = metrics(eq.equity)
        except Exception as e:
            print(f"  {name:<22} {cat:<8} 失败: {type(e).__name__}: {e}", flush=True)
            df.drop(columns=[col], inplace=True, errors="ignore")
            continue
        excess = m.get("年化收益", 0) - bench.get("年化收益", 0)
        print("  {:<22} {:<8} {:>8.2f} {:>7.2f} {:>7.2f} {:>9.2f} {:>6.2f} "
              "{:>7.0f} {:>9,} {:>9.2f}".format(
                  name, cat, m.get("年化收益", 0), m.get("年化波动", 0),
                  m.get("夏普比率", 0), m.get("最大回撤", 0),
                  m.get("卡玛比率", 0), meta.get("avg_hold", 0), len(tr), excess),
              flush=True)
        rows.append({
            "key": key, "策略": name, "类别": cat, "hold": args.hold,
            "年化收益": m.get("年化收益", 0), "年化波动": m.get("年化波动", 0),
            "夏普比率": m.get("夏普比率", 0), "最大回撤": m.get("最大回撤", 0),
            "卡玛比率": m.get("卡玛比率", 0),
            "平均持仓": meta.get("avg_hold", 0), "交易笔数": len(tr),
            "信号数": n_sig, "年均信号数": n_sig / years,
            "超额年化pp": excess,
            "夏普差": m.get("夏普比率", 0) - bench.get("夏普比率", 0),
            "回撤差pp": m.get("最大回撤", 0) - bench.get("最大回撤", 0),
            "翻译偏差": spec["note"], "来源": spec["src"],
            "耗时秒": round(time.time() - t0, 1),
        })
        pd.DataFrame(rows).to_csv(out_path, index=False, encoding="utf-8-sig")
        df.drop(columns=[col], inplace=True, errors="ignore")

    print("=" * 126)
    if rows:
        r = pd.DataFrame(rows)
        print(f"\n基准：年化 {bench['年化收益']:.2f}%  夏普 {bench['夏普比率']:.2f}  "
              f"回撤 {bench['最大回撤']:.2f}%")
        print("\n按夏普排序（夏普差 > 0 才算跑赢基准的风险调整收益）：")
        print("  {:<22} {:>7} {:>7} {:>9} {:>10}".format(
            "策略", "夏普", "夏普差", "超额pp", "回撤差pp"))
        for _, x in r.sort_values("夏普比率", ascending=False).iterrows():
            print("  {:<22} {:>7.2f} {:>7.2f} {:>9.2f} {:>10.2f}".format(
                x.策略, x.夏普比率, x.夏普差, x.超额年化pp, x.回撤差pp))
        print(f"\n结果已写出 {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
