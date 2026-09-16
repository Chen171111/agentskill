"""个股数据体检（补充）：把「不可能的日收益」截断到涨跌停，策略结论会不会变？

动机
----
`tools/diag_stock_data.py` 查出：全表有 **15,975 行**「不可能收益」（|日收益| 超过涨跌停），
其中 102 行是真实的无涨跌幅限制场景（长期停牌复牌首日），其余 **15,873 行是前复权口径
产物**（腾讯 qfq = 真实价 − 累计分红，累计分红逼近真实价时分母趋零，收益率爆炸），
**6,210 行落在股票池内**。

体检报告里有一句"影响很小"，但那是**估的**。本脚本把它变成**实跑的数字**：
把 |ret1| 截断到当日涨跌停、重建一条干净的合成价格路径，再跑同一个回测，
看结论动不动。

做法
----
1. `build_features` 得到原始特征（含 `limit`：68/30 开头 ±20%，其余 ±10%）
2. `ret1_clip = clip(ret1, −limit, +limit)`，逐只按 `close_syn = 首日收盘 × cumprod(1+ret1_clip)`
   重建合成价格路径 —— **没有触发的行完全不变**，只在极端行上"抹平"跳变
3. `open/high/low` 按同一比例缩放（保持日内关系），`volume` 不变
4. 用合成路径重跑 `build_features` → 8 个因子全部由"干净收益"推导
5. 对 `topk=800 / 1200 @ hold=20` 跑回测，与原始数据对照

已知副作用（必须披露）
----------------------
合成路径会让 `amt = close × volume × 100` 偏离真实成交额，
故 `illiq20` 与池子的 `min_amount` 过滤会受**轻微**扰动。
但截断只在极少数行上生效（见输出），且本脚本只回答"结论是否翻转"，
这个副作用可接受。**严格的根治做法**是改用不复权价 + 分红/送转回放
（腾讯 fqkline 只返回 6 个字段，无成交额、无不复权价，接口侧修不了）。

用法
----
    PY=.../python.exe
    $PY tools/diag_stock_ret_clip.py --bars data/stockbars/bars_all.parquet \
        --universe data/stockbars/universe_all.csv --topks 800,1200 --hold 20
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import DEFAULT_FACTORS, build_features, metrics, run


def load_bars(bars_path: str, universe_path: str | None) -> pd.DataFrame:
    b = pd.read_parquet(bars_path)
    b["date"] = b.date.astype(str).str.replace("-", "", regex=False)
    if universe_path and os.path.exists(universe_path):
        u = pd.read_csv(universe_path, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
        n0 = b.code.nunique()
        b = b[~b.code.isin(st)]
        n1 = b.code.nunique()
        if n0 == n1:
            # 正常情况：bars_all 里本就不含 ST/退市股 → 过滤退化为 no-op
            print(f"  ST/退市名单 {len(st)} 只均不在数据中 → 过滤为 no-op：{n0} 只")
        else:
            print(f"  剔除 ST/退市：{n0} → {n1} 只（名单 {len(st)} 只）")
    return b.sort_values(["code", "date"]).reset_index(drop=True)


def build_clipped_bars(bars: pd.DataFrame, tol: float = 0.02) -> tuple[pd.DataFrame, dict]:
    """把 |ret1| 截断到涨跌停（+容差），重建合成 OHLC 路径。

    `tol` 不能省。涨跌停价是**四舍五入到分**的，故正常涨停日的实际收益
    可能略超名义涨跌停（如 prev=2.09 → 涨停价 2.30 → +10.05%）。
    若按 `|ret1| > limit` 截断，会误伤约 12 万行**合法涨停日**（实测 1.55% 全表），
    把测试变成"顺手改了一堆正常数据"。故用与体检一致的判据 `limit + tol`。
    """
    d = bars.copy()
    d["_prev"] = d.groupby("code", sort=False).close.shift(1)
    d["_ret1"] = d.close / d._prev - 1.0
    d["_lim"] = np.where(d.code.str[2:5].str.startswith(("68", "30")), 0.20, 0.10)
    d["_cap"] = d._lim + tol
    # 上市前 5 日无涨跌幅限制（新股首日可涨几倍）→ 不参与截断
    d["_listed"] = d.groupby("code", sort=False).cumcount() + 1

    hit = (d._ret1.abs() > d._cap) & (d._listed > 5)
    n_clip = int(hit.sum())
    n_clip_inf = int(np.isinf(d._ret1).sum())
    n_codes = int(d.loc[hit, "code"].nunique())

    # 截断 → 首行 NaN 视作 0（保持首日价格锚定）
    # ⚠️ Series.where(cond, other) 的语义是「cond 为 True 处**保留原值**，False 处用 other」。
    # 故要"只截断 hit 行"，必须写 `raw.where(~hit, clipped)` ——
    # 写成 `raw.where(hit, clipped)` 会**反过来**：极端行原封不动、
    # 正常行被换成 clipped（而正常行的 clip 是恒等），等于什么都没做。
    clipped = d._ret1.clip(lower=-d._cap, upper=d._cap)
    d["_r"] = d._ret1.where(~hit, clipped).fillna(0.0)
    d["_cum"] = d.groupby("code", sort=False)._r.transform(lambda s: (1.0 + s).cumprod())
    d["_first"] = d.groupby("code", sort=False).close.transform("first")
    d["close_syn"] = d._first * d._cum

    # open/high/low 按同一比例缩放；比例在 close<=0 处无定义 → 前向填充
    d["_scale"] = (d.close_syn / d.close.replace(0, np.nan)).replace(
        [np.inf, -np.inf], np.nan)
    d["_scale"] = d.groupby("code", sort=False)._scale.transform(
        lambda s: s.ffill().bfill()).fillna(1.0)

    out = bars.copy()
    out["close"] = d.close_syn.values
    out["open"] = (d.open * d._scale).values
    out["high"] = (d.high * d._scale).values
    out["low"] = (d.low * d._scale).values
    # volume 不动（截断只改价格口径）

    info = {"截断行数": n_clip, "其中原为 inf": n_clip_inf,
            "涉及股票": n_codes, "全表行数": len(d), "容差": tol,
            "占比": n_clip / len(d) * 100}
    return out, info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="涨跌停截断对照（数据体检补充）")
    ap.add_argument("--bars", required=True)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260911")
    ap.add_argument("--topks", default="800,1200")
    ap.add_argument("--hold", type=int, default=20)
    ap.add_argument("--weight-mode", default="rank")
    ap.add_argument("--tilt-min", type=float, default=0.45)
    ap.add_argument("--min-amount", type=float, default=3e7)
    ap.add_argument("--min-listed", type=int, default=120)
    ap.add_argument("--min-price", type=float, default=2.0)
    ap.add_argument("--tol", type=float, default=0.02,
                    help="截断阈值 = 涨跌停 + tol（涨跌停价四舍五入到分，"
                         "正常涨停日可能略超名义幅度，故必须留容差）")
    ap.add_argument("--out", default="results/diag_stock_ret_clip.csv")
    args = ap.parse_args(argv)
    topks = [int(x) for x in args.topks.split(",") if x.strip()]

    print("=" * 92)
    print("  涨跌停截断对照：极端收益是否影响策略结论")
    print("=" * 92)
    bars = load_bars(args.bars, args.universe)
    print(f"  日线 {len(bars):,} 行 / {bars.code.nunique():,} 只 / "
          f"{bars.date.nunique()} 交易日 ({bars.date.min()} ~ {bars.date.max()})\n")

    print("  重建截断后的合成价格路径…", flush=True)
    bars_syn, info = build_clipped_bars(bars, args.tol)
    print(f"    阈值 = 涨跌停 + {info['容差']:.2f} → 截断 {info['截断行数']:,} 行 / "
          f"{info['涉及股票']:,} 只（占全表 {info['占比']:.4f}%，"
          f"其中原本为 inf 的 {info['其中原为 inf']:,} 行）\n")

    print("  计算因子（原始）…", flush=True)
    df_raw = build_features(bars)
    print("  计算因子（截断后）…", flush=True)
    df_syn = build_features(bars_syn)

    common = dict(start=args.start, end=args.end, hold=args.hold,
                  weight_mode=args.weight_mode, tilt_min=args.tilt_min,
                  min_amount=args.min_amount, min_listed=args.min_listed,
                  min_price=args.min_price)

    rows = []
    print("\n" + "=" * 92)
    for tk in topks:
        for tag, dfx in (("原始", df_raw), ("截断", df_syn)):
            eq, tr, meta = run(dfx, DEFAULT_FACTORS, topk=tk, **common)
            m = metrics(eq.equity)
            rows.append({"数据": tag, "topk": tk, "hold": args.hold,
                         "权重": args.weight_mode,
                         "年化": m["年化收益"], "夏普": m["夏普比率"],
                         "最大回撤": m["最大回撤"], "累计": m["累计收益"],
                         "平均持仓": meta.get("avg_hold", np.nan),
                         "交易笔数": meta.get("n_trades", 0),
                         "跳过调仓": meta.get("n_skip", 0)})
            print(f"  [{tag}] topk={tk:<5} 年化 {m['年化收益']:>6.2f}%  "
                  f"夏普 {m['夏普比率']:.2f}  回撤 {m['最大回撤']:>7.2f}%  "
                  f"累计 {m['累计收益']:>7.2f}%", flush=True)

    res = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    res.to_csv(args.out, index=False, encoding="utf-8-sig")

    print("\n" + "=" * 92)
    print("  对照汇总（Δ = 截断 − 原始）")
    print("=" * 92)
    for tk in topks:
        a = res[(res.数据 == "原始") & (res.topk == tk)].iloc[0]
        b = res[(res.数据 == "截断") & (res.topk == tk)].iloc[0]
        print(f"  topk={tk:<5} 年化 {a.年化:>6.2f}% → {b.年化:>6.2f}%  "
              f"Δ {b.年化-a.年化:+.2f}pp   "
              f"夏普 {a.夏普:.2f} → {b.夏普:.2f}  Δ {b.夏普-a.夏普:+.2f}   "
              f"回撤 {a.最大回撤:>7.2f}% → {b.最大回撤:>7.2f}%  "
              f"Δ {b.最大回撤-a.最大回撤:+.2f}pp")
    print(f"\n  结果已写出 {args.out}")
    print("  读法：Δ 远小于策略本身的标准误 → 极端收益**不影响策略结论**，"
          "数据体检的『影响很小』是被实跑确认的，不是估的。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
