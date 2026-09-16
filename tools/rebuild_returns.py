"""用**不复权价 + 分红/送转回放**重建正确的总收益序列。

为什么需要
----------
腾讯的 `qfq` 是**仿射复权**（`bfq = k·qfq + m`），**不保持日收益**：

| 股票 | 期间 | k | m | 真实日收益 | qfq 口径 |
|---|---|---|---|---|---|
| 中国神华 SH601088 | 2019-01-04 | 1.0 | 15.57 | **+2.15%** | **+17.92%** |
| 兖矿能源 SH600188 | 2021-11-04 | ≈1.96 | ≈10.98 | — | 被放大约 2× |

池内放大倍数分布：中位 1.017、**>1.05 占 28.2%**、**>1.10 占 14.9%**、>1.25 占 4.2%。
且 `放大倍数 = 1 + m/qfq` → **qfq 越小（跌得越惨）放大越狠**，
恰好是反转策略要买的标的 → 必须修正。

做法（标准总收益构造）
----------------------
对每只股票、每个交易日 t：

- **非除权日**：`ret(t) = bfq_close(t) / bfq_close(t−1) − 1`
- **除权日**（当日每股派现 `D`、每股送转 `r`）：
  `ret(t) = (bfq_close(t) × (1+r) + D) / bfq_close(t−1) − 1`

再累乘成一条**连续的价格路径** `P(t) = P(t−1) × (1 + ret(t))`，
`open/high/low` 按当日 `close_adj/close_bfq` 同比例缩放（保持日内关系），
`volume` 不变，并保留**真实成交额**。

输出 `data/stockbars/bars_total.parquet`，列与 `bars_all.parquet` 兼容，
另多一列 `amount`（元）—— 引擎的 `build_features` 会优先用它，
从而顺带修掉「用 `close×volume×100` 估算成交额」的旧限制。

用法
----
    PY=.../python.exe
    $PY tools/rebuild_returns.py --bfq data/stockbars/bars_bfq.parquet \
        --dividends data/dividends/bonus_all.parquet \
        --out data/stockbars/bars_total.parquet
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _num(x) -> float:
    """NaN 安全的取数。

    ⚠️ 不能写 `float(x or 0)` —— **NaN 在 Python 里是真值**，
    `nan or 0` 会返回 nan，然后污染整条累乘链（本次实测踩过）。
    """
    return 0.0 if pd.isna(x) else float(x)


def load_events(div_path: str) -> dict[str, dict[str, tuple[float, float]]]:
    """{code: {ex_date: (每股派现 D, 每股送转 r)}}。"""
    d = pd.read_parquet(div_path)
    d = d[d.EX_DIVIDEND_DATE.notna()].copy()
    if "ASSIGN_PROGRESS" in d.columns:
        # 只保留**已实施**的方案（未实施的除权日只是预案）
        d = d[d.ASSIGN_PROGRESS.astype(str).str.contains("实施", na=False)]
    out: dict[str, dict[str, tuple[float, float]]] = {}
    for _, r in d.iterrows():
        D = _num(r.PRETAX_BONUS_RMB) / 10.0
        rr = _num(r.BONUS_IT_RATIO) / 10.0
        if D == 0 and rr == 0:
            continue
        ev = out.setdefault(str(r.code), {})
        d0, r0 = ev.get(str(r.EX_DIVIDEND_DATE), (0.0, 0.0))
        ev[str(r.EX_DIVIDEND_DATE)] = (d0 + D, r0 + rr)
    return out


def rebuild(bars: pd.DataFrame, events, tax_rate: float = 0.0) -> pd.DataFrame:
    """重建总收益路径。

    `tax_rate`：**现金分红的红利税率**（0~0.2）。送转不征税，只有派现部分打折。

    A 股红利税是**差别化个人所得税**，按持股期限：

    | 持股期限 | 税率 |
    |---|---|
    | ≤ 1 个月 | **20%** |
    | 1 个月 ~ 1 年 | **10%** |
    | > 1 年 | **0%（免征）** |

    实际扣缴在**卖出时**按先进先出补扣。本项目的股息率策略年单边换手 2.9x
    → 平均持有期 ≈ 1/2.9 年 ≈ **4 个月** → 落在「1 个月~1 年」档 → **10%**。
    故 `--tax-rate 0.1` 是本策略的**合理估计（且略偏保守/上界）**：
    少数连续持有超过 1 年的仓位实际可免税，这里也按 10% 扣了。

    ⚠️ 这是**平坦税率近似**，不是逐笔按真实持股期计算。要精确就得在回测引擎里
    跟踪每个 lot 的建仓日 —— 本项目的结论对 0% / 10% 的差别不敏感（见 `docs/个股线_红利税.md`），
    故先用平坦近似，把**边界（0% / 10%）夹住真值**。
    """
    bars = bars.sort_values(["code", "date"]).reset_index(drop=True)
    frames = []
    n_ev = n_hit = 0
    div_gross = div_net = 0.0
    for code, g in bars.groupby("code", sort=False):
        g = g.copy()
        ev = events.get(code, {})
        n_ev += len(ev)
        prev = g.close.shift(1)
        D = g.date.map(lambda d: ev.get(d, (0.0, 0.0))[0]).astype(float).values
        rr = g.date.map(lambda d: ev.get(d, (0.0, 0.0))[1]).astype(float).values
        n_hit += int((D != 0).sum() + (rr != 0).sum())
        Dn = D * (1.0 - tax_rate)                 # 税后每股派现
        # ⚠️ 统计口径必须是**对日收益的贡献**（D / prev_close），不能是 D × close
        # （后者单位是「元²/股」，没有意义 —— 首版就是这么打印的）。
        with np.errstate(divide="ignore", invalid="ignore"):
            div_gross += float(np.nansum(D / prev.values))
            div_net += float(np.nansum(Dn / prev.values))
        # 总收益：除权日把**税后**派现与送转加回来
        ret = (g.close.values * (1.0 + rr) + Dn) / prev.values - 1.0
        ret = np.where(np.isfinite(ret), ret, 0.0)
        ret[0] = 0.0
        path = np.cumprod(1.0 + ret)
        # ⚠️ 必须**锚定到最后一个交易日的真实价**。
        # 不锚定的话路径从 1.0 起算（中位 0.85），而引擎的 `min_price >= 2.0`
        # 是拿**绝对价格**比的 → 池子会从 74.6% 塌到 5.1%，整个回测失真。
        # 实测踩过：未锚定时得出「年化 16.31% → 0.12%」，差点写成结论。
        if len(path) and path[-1] != 0:
            path = path * (g.close.values[-1] / path[-1])
        scale = path / np.where(g.close.values == 0, np.nan, g.close.values)
        scale = pd.Series(scale).ffill().bfill().fillna(1.0).values
        g["close"] = path
        g["open"] = g.open.values * scale
        g["high"] = g.high.values * scale
        g["low"] = g.low.values * scale
        g["ret_true"] = ret
        frames.append(g)
    out = pd.concat(frames, ignore_index=True)
    print(f"  分红事件 {n_ev:,} 个，其中实际命中交易日 {n_hit:,} 次")
    if tax_rate:
        # 「分红对日收益的累计贡献」= Σ(D/prev_close)，单位为「收益点数」
        drag = div_gross - div_net
        print(f"  红利税率 {tax_rate*100:.0f}%：全样本分红对日收益的累计贡献 "
              f"{div_gross*100:,.0f} 点 → {div_net*100:,.0f} 点"
              f"（被税吃掉 {drag*100:,.0f} 点，占 {drag/max(div_gross,1e-9)*100:.1f}%）")
        print(f"     注：这是**未加权、未复利**的原始点数，仅供量级参考；"
              f"对策略年化的真实影响以回测为准")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="重建正确总收益序列")
    ap.add_argument("--bfq", default="data/stockbars/bars_bfq.parquet")
    ap.add_argument("--dividends", default="data/dividends/bonus_all.parquet")
    ap.add_argument("--out", default="data/stockbars/bars_total.parquet")
    ap.add_argument("--tax-rate", type=float, default=0.0,
                    help="现金分红红利税率（0~0.2）。股息率策略（年换手 2.9x，"
                         "平均持有约 4 个月）应取 0.1")
    args = ap.parse_args(argv)

    print("=" * 78)
    print("  重建总收益序列（不复权价 + 分红/送转回放）")
    print("=" * 78)
    if args.tax_rate:
        print(f"  ⚠️ 红利税率 = {args.tax_rate*100:.0f}%（送转不征税）")
    bars = pd.read_parquet(args.bfq)
    bars["date"] = bars.date.astype(str).str.replace("-", "", regex=False)
    print(f"  不复权日线 {len(bars):,} 行 / {bars.code.nunique():,} 只 / "
          f"{bars.date.min()} ~ {bars.date.max()}")

    ev = load_events(args.dividends)
    print(f"  分红数据 {len(ev):,} 只股票有除权事件")

    out = rebuild(bars, ev, tax_rate=args.tax_rate)

    # 真实成交额：万元 → 元
    if "amount_wan" in out.columns:
        out["amount"] = out.amount_wan.astype(float) * 1e4
    keep = ["code", "date", "open", "close", "high", "low", "volume"]
    if "amount" in out.columns:
        keep.append("amount")
    if "turnover" in out.columns:
        keep.append("turnover")
    out = out[[c for c in keep if c in out.columns]]

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    out.to_parquet(args.out, index=False)
    print(f"\n  已写出 {args.out}")
    print(f"  {len(out):,} 行 / {out.code.nunique():,} 只")

    # 快速体检：日收益是否还在涨跌停内
    r = out.sort_values(["code", "date"]).groupby("code", sort=False).close.pct_change()
    lim = np.where(out.sort_values(["code", "date"]).code.str[2:5]
                   .str.startswith(("68", "30")), 0.20, 0.10)
    bad = int((r.abs() > pd.Series(lim, index=r.index) + 0.02).sum())
    print(f"  重建后 |日收益| 超涨跌停的行: {bad:,}（原 qfq 数据是 15,975）")

    # ⚠️ 归一化自检：重建后的**价格量级**必须与不复权价一致，
    # 否则 `min_price` 这类**绝对价格**门槛会失效（实测踩过一次）。
    med_bfq = float(bars.close.median())
    med_new = float(out.close.median())
    ratio = med_new / med_bfq if med_bfq else float("nan")
    print(f"\n  归一化自检：不复权价中位 {med_bfq:.3f} → 重建后中位 {med_new:.3f}"
          f"（比值 {ratio:.4f}）")
    if not (0.5 < ratio < 2.0):
        print("  ❌ 比值偏离 1 太远 → 价格路径没锚定到最新真实价，"
              "`min_price` 门槛会失效！")
        return 2
    print("  ✅ 量级一致 → `min_price` / `min_amount` 门槛可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
