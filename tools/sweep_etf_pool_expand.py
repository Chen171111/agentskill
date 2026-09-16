"""ETF 池扩容研究：把池子从 11 只扩到 20+ 只，做行业/主题轮动。

背景
----
实盘池 `ETF全球` 只有 11 只，`data/stocks/` 里却有 27 只 ETF 数据。
主人要求「扩池做行业轮动」。

⚠️ 头号陷阱：**ETF 上市时间差异极大**
--------------------------------------------------------------------
- 早：510050(2005)、510880(2007)
- 中：510300/510500/518880/513100(2012~2013)
- 晚：588170「科创半导体ETF」、589010「人工智能ETF」**2025-04 才上市（仅 346 行）**

回测引擎 `build_panel` 对池子取**交集** → 只要池里有一只晚上市的，
整个回测区间就被压到那只 ETF 的起点，**"全区间 2019~2026"会悄悄变成 2025**。
（本项目在 `docs/ETF线_真实成本复核.md` 已踩过这个坑：`科技进攻` 池两列数字完全相同。）

因此本脚本按**上市日期**分层建池，每个池只用「在该区间内已有数据」的标的。

用法
----
    $PY tools/sweep_etf_pool_expand.py
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                              # noqa: E402
from pipeline import run_backtest                          # noqa: E402

STRATEGY = "etf_rotation"
TOP_K = 5
REBALANCE = 5
CASH = 100_000.0
THRESH = 0.20


def list_etfs(d='data/stocks'):
    """返回 {code: (start_date, end_date, nrows)}，只挑 ETF 代码。"""
    out = {}
    for f in sorted(os.listdir(d)):
        if not f.endswith('.csv'):
            continue
        code = f[:-4]
        num = code.split('.')[0]
        if num[:2] not in ('51', '56', '58', '15', '16'):
            continue
        try:
            df = pd.read_csv(os.path.join(d, f), dtype={'date': str})
            df['date'] = df.date.str.replace('-', '', regex=False)
            out[code] = (df.date.min(), df.date.max(), len(df))
        except Exception:
            pass
    return out


def scan_anomalies(code, d='data/stocks', thresh=THRESH):
    """扫描单日 |收益| > thresh 的复权跳变（份额拆分/折算未处理）。"""
    try:
        df = pd.read_csv(os.path.join(d, code + '.csv'), dtype={'date': str})
        df = df.set_index('date').sort_index()
        r = df.close.pct_change()
        hits = r[r.abs() > thresh]
        return [(dt, float(r[dt])) for dt in hits.index]
    except Exception:
        return []


def main() -> int:
    info = list_etfs()
    print(f"data/stocks 里共 {len(info)} 只 ETF\n")

    # ---- 数据质量：复权跳变扫描 ----
    print("=== 1. 数据质量：单日 |收益| > 20% 的复权跳变 ===")
    bad = {}
    for c in sorted(info):
        hits = scan_anomalies(c)
        if hits:
            bad[c] = hits
            nm = config.ETF_NAMES.get(c, '')
            for dt, r in hits:
                print(f"  {c} {nm:<14} {dt}  {r:+.1%}")
    if not bad:
        print("  ✓ 未发现异常")
    print()

    # ---- 按上市日期分层建池 ----
    all_codes = sorted(info)
    live = config.RECOMMENDED_POOLS["ETF全球"]

    def by_start(before):
        return [c for c in all_codes if info[c][0] <= before]

    pools = {
        "★ 现有 ETF全球(实盘)": live,
        "A 上市≤2013 (长期)": by_start("20131231"),
        "B 上市≤2016": by_start("20161231"),
        "C 上市≤2019": by_start("20191231"),
        "D 上市≤2021": by_start("20211231"),
        "E 全部27只": all_codes,
    }

    print("=== 2. 候选池构成 ===")
    for name, cs in pools.items():
        starts = sorted(info[c][0] for c in cs if c in info)
        print(f"  {name:<24} {len(cs):>2} 只   最早 {starts[0] if starts else '-'}   "
              f"最晚 {starts[-1] if starts else '-'}")
    print()

    # ---- 回测对照 ----
    periods = [("全区间 2019~2026", "20190101", "20260911"),
               ("近三年 2024~2026", "20240101", "20260911")]

    print("=== 3. 回测对照（策略 etf_rotation / topk=5 / 5日调仓 / 10万 / 万5）===")
    header = "  {:<24}{:>8}".format("池子", "标的数")
    for tag, _, _ in periods:
        header += "{:>30}".format(tag)
    print(header)
    print("-" * 108)

    rows = []
    for name, cs in pools.items():
        line = "  {:<24}{:>8}".format(name, len(cs))
        for tag, s, e in periods:
            try:
                r = run_backtest(codes=cs, strategy=STRATEGY, start=s, end=e,
                                 topk=TOP_K, rebalance=REBALANCE, init_cash=CASH)
                m = r["metrics"]
                meta = r["meta"]
                line += "{:>30}".format(
                    f"{m.get('年化收益',0):>6.2f}% /{m.get('夏普比率',0):>5.2f} "
                    f"/{m.get('最大回撤',0):>6.1f}%")
                rows.append({"池子": name, "标的数": len(cs), "区间": tag,
                             "实际起": meta.get("start"), "实际止": meta.get("end"),
                             "年化": m.get("年化收益", 0), "夏普": m.get("夏普比率", 0),
                             "回撤": m.get("最大回撤", 0), "卡玛": m.get("卡玛比率", 0)})
            except Exception as ex:
                line += "{:>30}".format(f"失败 {type(ex).__name__}")
        print(line, flush=True)

    print("-" * 108)
    print("  格式：年化% / 夏普 / 最大回撤%")
    print("  ⚠️ 必须核对「实际起」列——若池里有晚上市 ETF，实际区间会被压缩。")

    if rows:
        pd.DataFrame(rows).to_csv("results/etf_pool_expand.csv", index=False,
                                  encoding="utf-8-sig")
        print("\n结果已写出 results/etf_pool_expand.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
