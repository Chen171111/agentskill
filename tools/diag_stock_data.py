"""个股数据体检：ETF 线踩过的「份额拆分未复权」坑，个股线必须先查一遍。

为什么需要
----------
`docs/ETF线_数据质量缺陷.md` 记录：ETF 数据是**不复权**的，4 处份额拆分被当成真实
涨跌（+248.6% / −80.5% / −74.5% / −49.2%），污染了全部 ETF 回测。
修完实盘配置真实表现 +0.98pp。教训：**先体检数据，再谈策略。**

个股线用的是另一份数据（腾讯 fqkline，`qfq` 前复权），但从未做过同等体检。
本脚本把它补上。

判据（为什么这些判据有效）
--------------------------
A 股有涨跌停（主板 ±10%、创业板/科创板 ±20%、ST ±5%、北交所 ±30%），
**所以任何 |日收益| 超过涨跌停幅度的行，都不可能是正常交易产生的** ——
它要么是①真实的「无涨跌幅限制」场景（恢复上市 / 长期停牌复牌首日 / 新股首日），
要么是②数据错误（未复权的除权跳变、错价）。两者必须区分开。

检查项
------
1. **不可能收益**：|ret1| > 涨跌停 + 2%（排除上市前 5 日）
   - 逐条标注是否紧跟 ≥20 个交易日的停牌 → 若如此，判为「复牌首日」而非数据错误
2. **非正价格**：close ≤ 0 / open ≤ 0（前复权锚定在最新价，高分红老股历史价会被压到 ≤0）
   - 关键看它们**有没有落进股票池**（池子要求 close ≥ min_price，通常会挡住）
3. **因子层污染**：8 个因子里的 inf / NaN 计数
4. **成交额口径**：amt = close × volume × 100 是否出现负值/零值
5. **前复权 vs 不复权**（`--bfq-sample N`，需联网）：抽样拉不复权数据对比，
   量化「用前复权价做 min_price / min_amount 门槛」造成的错杀比例

用法
----
    PY=.../python.exe
    $PY tools/diag_stock_data.py --bars data/stockbars/bars_all.parquet \
        --universe data/stockbars/universe_all.csv
    $PY tools/diag_stock_data.py --bars data/stockbars/bars_all.parquet \
        --universe data/stockbars/universe_all.csv --bfq-sample 60
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FACTORS = ["rev20", "rev60", "rev120", "rev5", "vol20", "max20", "turn20", "illiq20"]


def limit_of(codes: pd.Series) -> np.ndarray:
    """按板块返回涨跌停幅度：68/30 开头（科创/创业）±20%，其余 ±10%。"""
    return np.where(codes.str[2:5].str.startswith(("68", "30")), 0.20, 0.10)


def load(bars_path: str, universe_path: str | None):
    b = pd.read_parquet(bars_path)
    b["date"] = b.date.astype(str).str.replace("-", "", regex=False)
    b = b.sort_values(["code", "date"]).reset_index(drop=True)
    st = set()
    if universe_path and os.path.exists(universe_path):
        u = pd.read_csv(universe_path, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
    b["_st"] = b.code.isin(st)
    return b


# ------------------------------------------------------------ 1/2/4 表内检查
def check_table(b: pd.DataFrame, min_price: float, min_amount: float,
                min_listed: int) -> dict:
    g = b.groupby("code", sort=False)
    b["ret1"] = g.close.pct_change()
    b["listed"] = g.cumcount() + 1
    b["lim"] = limit_of(b.code)
    b["amt"] = b.close * b.volume * 100.0
    b["amt_ma20"] = b.groupby("code", sort=False).amt.transform(
        lambda s: s.rolling(20).mean())
    b["suspended"] = b.volume.fillna(0) <= 0
    b["_in"] = ((~b._st) & (b.listed >= min_listed) & (b.amt_ma20 >= min_amount)
                & (b.close >= min_price) & (~b.suspended))

    out: dict = {}
    ok = b[(b.listed > 5) & b.ret1.notna()]
    bad = ok[ok.ret1.abs() > ok.lim + 0.02].copy()

    # 判定「复牌首日」：与上一行间隔 ≥20 个交易日（长期停牌 / 恢复上市当日无涨跌幅限制）
    all_dates = np.array(sorted(b.date.unique()))
    pos = {d: i for i, d in enumerate(all_dates)}
    prev = b.groupby("code", sort=False).date.shift(1)
    gap = pd.Series([pos.get(d, 0) for d in b.date]) - pd.Series(
        [pos.get(d, 0) for d in prev.fillna(b.date)])
    b["_gap"] = gap.values
    b["_prev_close"] = b.groupby("code", sort=False).close.shift(1)
    bad = bad.merge(b[["code", "date", "_gap", "_prev_close"]],
                    on=["code", "date"], how="left")
    bad["复牌首日"] = bad._gap >= 20
    # 「复权口径产物」的**严格定义**要靠不复权数据核实（见 verify_extreme）。
    # 这里先给一个纯逻辑下界：A 股有涨跌停，非复牌的 |日收益| > 涨跌停
    # 只可能来自复权口径 —— 前复权价 = 真实价 − 累计分红，累计分红接近/超过
    # 真实价时分母趋零，收益率爆炸。故此处先标为「疑似」，由 --bfq-sample 核实。
    bad["疑似复权产物"] = ~bad.复牌首日

    out["不可能收益行"] = len(bad)
    out["不可能收益_涉及股票"] = int(bad.code.nunique())
    out["不可能收益_判为复牌"] = int(bad.复牌首日.sum())
    out["不可能收益_疑似复权"] = int(bad.疑似复权产物.sum())
    out["不可能收益_未解释"] = int(bad.疑似复权产物.sum())
    out["不可能收益_落池内"] = int(bad._in.sum()) if "_in" in bad else 0
    out["不可能收益_落池内股票数"] = int(bad.loc[bad._in, "code"].nunique())

    for nm, m in (("close<=0", b.close <= 0), ("open<=0", b.open <= 0),
                  ("amt<0", b.amt < 0), ("amt==0", b.amt == 0)):
        out[nm + "_行"] = int(m.sum())
        out[nm + "_股票"] = int(b.loc[m, "code"].nunique())
    out["池内close<=0行"] = int((b._in & (b.close <= 0)).sum())
    out["池内amt<=0行"] = int((b._in & (b.amt <= 0)).sum())
    out["池内行数"] = int(b._in.sum())
    out["池内股票数"] = int(b.loc[b._in, "code"].nunique())
    out["池内close最小值"] = float(b.loc[b._in, "close"].min())
    out["未解释明细"] = bad[bad.疑似复权产物][
        ["code", "date", "close", "ret1", "_gap"]].sort_values(
        "ret1", key=lambda s: s.abs(), ascending=False).head(20)
    return out, bad


# ------------------------------------------------------------ 3 因子层检查
def check_factors(b: pd.DataFrame, start: str, end: str,
                  min_price: float = 2.0, min_amount: float = 3e7,
                  min_listed: int = 120) -> pd.DataFrame:
    """统计**股票池内**的因子 inf / NaN。

    ⚠️ 必须真的做池子过滤（剔除 ST + listed/amt_ma20/close/suspended 四项门槛），
    否则统计的是"全表"而不是"进得了模型的行" —— 那样 inf 计数会被池外
    （前复权价 ≤0 的高分红老股）污染，答案就没意义了。
    """
    from tools.backtest_stock import build_features
    b2 = b[~b._st]                     # 与引擎一致：先剔 ST
    df = build_features(b2[["date", "code", "open", "close", "high", "low",
                            "volume"]].copy())
    d = df[(df.date >= start) & (df.date <= end)]
    in_pool = ((d.listed >= min_listed) & (d.amt_ma20 >= min_amount)
               & (d.close >= min_price) & (~d.suspended))
    d = d[in_pool]
    rows = []
    for f in FACTORS:
        s = d[f]
        rows.append({"因子": f, "inf": int(np.isinf(s).sum()),
                     "NaN": int(s.isna().sum()), "总行": len(s)})
    return pd.DataFrame(rows)


# ------------------------------------------------------------ 5 前复权 vs 不复权
def fetch_bfq(sym: str, start: str, end: str):
    from tools.fetch_stock_history import _get, to_symbol

    def dash(s: str) -> str:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s

    host = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get"
    sym = to_symbol(sym)
    start, end = dash(start), dash(end)          # 接口只认 YYYY-MM-DD
    rows, cursor, seen = [], end, set()
    for _ in range(6):
        txt = _get(f"{host}?param={sym},day,{start},{cursor},800,bfq", timeout=35)
        if not txt:
            break
        try:
            node = (json.loads(txt).get("data") or {}).get(sym) or {}
        except Exception:
            break
        arr = node.get("bfqday") or node.get("day") or []
        if not arr:
            break
        new = 0
        for x in arr:
            k = x[0].replace("-", "")
            if k in seen or k < start.replace("-", ""):
                continue
            seen.add(k)
            new += 1
            rows.append((k, float(x[2])))
        earliest = min(x[0] for x in arr)
        if new == 0 or earliest.replace("-", "") <= start.replace("-", ""):
            break
        cursor = (datetime.strptime(earliest, "%Y-%m-%d")
                  - timedelta(days=1)).strftime("%Y-%m-%d")
        time.sleep(0.04)
    return pd.DataFrame(rows, columns=["date", "bfq"]) if rows else None


def check_qfq_vs_bfq(b: pd.DataFrame, n: int, min_price: float,
                     start: str, end: str) -> pd.DataFrame:
    """随机抽样对比前复权/不复权，量化「价格门槛错杀」。"""
    pool_codes = b.loc[b._in, "code"].drop_duplicates()
    sample = list(pool_codes.sample(min(n, len(pool_codes)), random_state=7))
    recs = []
    for i, c in enumerate(sample):
        d = fetch_bfq(c, start, end)
        if d is None or d.empty:
            continue
        q = b.loc[b.code == c, ["date", "close"]].rename(columns={"close": "qfq"})
        m = q.merge(d, on="date", how="inner")
        if len(m) < 200:
            continue
        recs.append({"code": c, "n": len(m), "ratio_first": m.qfq.iloc[0] / m.bfq.iloc[0],
                     "ratio_median": float((m.qfq / m.bfq).median()),
                     "错杀行": int(((m.bfq >= min_price) & (m.qfq < min_price)).sum())})
        if (i + 1) % 20 == 0:
            print(f"    ...{i+1}/{len(sample)}", flush=True)
    return pd.DataFrame(recs)


def verify_extreme(b: pd.DataFrame, bad: pd.DataFrame, cap: int,
                   start: str, end: str) -> pd.DataFrame:
    """用**不复权**数据核实「疑似复权产物」：真实收益率是否也在涨跌停内。

    若真实收益 ≤ 涨跌停，则该行确为**复权口径产物**（既不是数据错误，也不是真实行情）。
    这是把「未解释」从"我猜的"变成"我查过的"的关键一步。
    """
    sus = bad[bad.疑似复权产物]
    codes = sus.code.value_counts().head(cap).index.tolist()
    recs = []
    for i, c in enumerate(codes):
        d = fetch_bfq(c, start, end)
        if d is None or d.empty:
            continue
        d = d.sort_values("date")
        d["bfq_ret"] = d.bfq.pct_change()
        m = sus[sus.code == c].merge(d, on="date", how="inner")
        if m.empty:
            continue
        lim = 0.20 if c[2:5].startswith(("68", "30")) else 0.10
        recs.append({"code": c, "核实行数": len(m),
                     "真实收益仍在限内": int((m.bfq_ret.abs() <= lim + 0.02).sum()),
                     "真实收益也超限": int((m.bfq_ret.abs() > lim + 0.02).sum()),
                     "前复权最大|收益|": float(m.ret1.abs().max()),
                     "真实最大|收益|": float(m.bfq_ret.abs().max())})
        if (i + 1) % 10 == 0:
            print(f"    ...{i+1}/{len(codes)}", flush=True)
    return pd.DataFrame(recs)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="个股数据体检")
    ap.add_argument("--bars", required=True)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260911")
    ap.add_argument("--min-price", type=float, default=2.0)
    ap.add_argument("--min-amount", type=float, default=3e7)
    ap.add_argument("--min-listed", type=int, default=120)
    ap.add_argument("--bfq-sample", type=int, default=0,
                    help="随机抽样对比前复权/不复权的股票数（0=跳过，需联网）")
    ap.add_argument("--bfq-verify", type=int, default=25,
                    help="用不复权数据核实「疑似复权产物」的股票数上限")
    ap.add_argument("--out", default="results/diag_stock_data.csv")
    args = ap.parse_args(argv)

    print("=" * 78)
    print("  个股数据体检")
    print("=" * 78)
    b = load(args.bars, args.universe)
    print(f"  数据: {args.bars}")
    print(f"  {len(b):,} 行 / {b.code.nunique():,} 只 / {b.date.nunique():,} 交易日"
          f" / {b.date.min()}~{b.date.max()}\n")

    r, bad = check_table(b, args.min_price, args.min_amount, args.min_listed)
    print("【1】不可能收益（|日收益| > 涨跌停+2%，已排除上市前 5 日）")
    print(f"    {r['不可能收益行']:,} 行 / {r['不可能收益_涉及股票']:,} 只"
          f"   （占全表 {r['不可能收益行']/len(b)*100:.4f}%）")
    print(f"    ① 长期停牌复牌首日（真实、无涨跌幅限制）: "
          f"{r['不可能收益_判为复牌']:,}")
    print(f"    ② 疑似复权口径产物（待不复权数据核实）  : "
          f"{r['不可能收益_疑似复权']:,}")
    print(f"    落在股票池内: {r['不可能收益_落池内']:,} 行 / "
          f"{r['不可能收益_落池内股票数']} 只\n")
    print("    ② 的前 20 条（按 |前复权收益| 降序）:")
    print(r["未解释明细"].to_string(index=False))
    print()

    print("【2】非正价格 / 成交额")
    print(f"    close<=0 {r['close<=0_行']:>6,} 行 ({r['close<=0_股票']} 只)"
          f"   open<=0 {r['open<=0_行']:>6,} 行 ({r['open<=0_股票']} 只)")
    print(f"    amt<0    {r['amt<0_行']:>6,} 行 ({r['amt<0_股票']} 只)"
          f"   amt==0  {r['amt==0_行']:>6,} 行")
    print(f"    → 落进股票池的: close<=0 {r['池内close<=0行']} 行, "
          f"amt<=0 {r['池内amt<=0行']} 行   ← 必须为 0")
    print(f"    池内 close 最小值 {r['池内close最小值']:.4f}（门槛 {args.min_price}）\n")

    print("【3】因子层污染（**股票池内**：剔 ST + listed/amt_ma20/close/suspended）")
    ft = check_factors(b, args.start, args.end, args.min_price,
                       args.min_amount, args.min_listed)
    ft.to_csv(args.out.replace(".csv", "_factors.csv"), index=False,
              encoding="utf-8-sig")
    for _, x in ft.iterrows():
        rate = x["inf"] / max(x["总行"], 1)
        # 只有占比超过 0.001% 才值得报警 —— 1/655万 这种量级报 ⚠️ 会让人对告警脱敏
        if rate > 1e-5:
            flag = "  ← ⚠️"
        elif x["inf"]:
            flag = "  ← 可忽略（1e-6 量级）"
        else:
            flag = ""
        print(f"    {x['因子']:<8} inf {x['inf']:>5}   NaN {x['NaN']:>8}{flag}")
    if ft["inf"].sum() == 0:
        print("    → 池内因子层无 inf")
    else:
        print("    → NaN 多为**暖机期**（如 rev120 需要 121 根 K 线，"
              "而 listed>=120 只保证 120 根）")
    print()

    if args.bfq_sample:
        print(f"【4】前复权 vs 不复权（随机抽样 {args.bfq_sample} 只，联网）")
        q = check_qfq_vs_bfq(b, args.bfq_sample, args.min_price,
                             b.date.min(), b.date.max())
        if len(q):
            q.to_csv(args.out.replace(".csv", "_qfq.csv"), index=False,
                     encoding="utf-8-sig")
            kill = q["错杀行"].sum()
            tot = q["n"].sum()
            print(f"    前复权/不复权 价格比: 中位 {q.ratio_median.median():.3f}"
                  f"  首日中位 {q.ratio_first.median():.3f}")
            print(f"    被 close>={args.min_price} 错杀的行: {kill:,} / {tot:,}"
                  f" = {kill/tot*100:.2f}%   涉及 {(q['错杀行']>0).sum()}/{len(q)} 只")
            print("    ⚠️ 该偏差无法从接口侧根治：fqkline 只返回 6 个字段，"
                  "无成交额、无不复权价")
        print()

        print(f"【5】核实「疑似复权产物」（不复权对照，最多 {args.bfq_verify} 只）")
        v = verify_extreme(b, bad, args.bfq_verify, b.date.min(), b.date.max())
        if len(v):
            v.to_csv(args.out.replace(".csv", "_verify.csv"), index=False,
                     encoding="utf-8-sig")
            ok = int(v["真实收益仍在限内"].sum())
            tot = int(v["核实行数"].sum())
            bad_real = int(v["真实收益也超限"].sum())
            print(f"    核实 {len(v)} 只 / {tot} 行")
            print(f"    真实收益仍在涨跌停内（→ 确认为复权口径产物）: {ok} "
                  f"({ok/tot*100:.2f}%)")
            print(f"    真实收益也超限（→ 才是真·数据错误）        : {bad_real}")
            print(f"    前复权最大 |收益| {v['前复权最大|收益|'].max():.3f}  vs  "
                  f"真实最大 |收益| {v['真实最大|收益|'].max():.3f}")
        print()

    r2 = {k: v for k, v in r.items() if k != "未解释明细"}
    pd.DataFrame([r2]).to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"结果已写出 {args.out}")
    print("\n判据速查：① 池内 close<=0 / amt<=0 = 0；② 因子 inf ≈ 0；"
          "\n          ③ 疑似复权产物必须能被不复权数据核实为『真实收益在限内』。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
