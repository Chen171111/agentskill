"""Faber 趋势过滤「并行验证」报告 —— 用**真实前向表现**比较 开/关。

背景（HANDOFF §三 第 2 项）
---------------------------
`strategies/builtin.py` 里 Faber 趋势过滤（`trend_window=60`）默认**开启**，
依据是 2026-09 的社区研究复测（2020-01~2026-08：年化 33.01% → 51.19%）。
但**修复份额折算数据后重跑，它是负贡献**（−1.81pp / 夏普 −0.27）。

两边都有理，**因为它只有一个 12 年样本**，且分区间结果剧烈波动
（见 `docs/ETF线_收益归因.md` 第四节）。回测已经吵不出结果了 ——
**唯一能定论的是真实前向数据。**

于是采取「并行验证」：
- **实盘配置不动**（Faber 仍开着），照常下单
- `scheduler/runner.py` 在**同一个调仓日**额外算一套「关闭 Faber」的影子权重，
  落库到 `shadow_weights` 表（**只记录、不执行**）
- 本脚本比较两套的**前向表现**

为什么必须"同一个调仓日重算"而不是事后回放：策略的调仓计数器 `_since` 跨运行持久化，
影子策略必须从同一计数器出发，否则两边调仓相位错开，比较无意义
（`tools/README.md` 铁律 7② 的坑）。

用法
----
    $PY tools/faber_shadow_report.py
    $PY tools/faber_shadow_report.py --min-weeks 8
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                              # noqa: E402
from dataprovider.store import DataStore                   # noqa: E402
from storage.db import TradeDB                             # noqa: E402


def _forward_returns(store, codes, rows):
    """按调仓序列算每个变体的前向收益。

    约定（与策略一致）：T 日出信号 → T+1 日执行 → 持有到下一个调仓日。
    权重向量在持有期内**不再平衡**（与实盘一致）。
    """
    closes = {}
    for c in codes:
        try:
            closes[c] = store.read(c)["close"]
        except Exception:
            continue
    px = pd.DataFrame(closes).sort_index()

    dates = [r["date"] for r in rows]
    out = {}
    for variant in ("live", "shadow"):
        w_by_date = {r["date"]: r["weights"] for r in rows if r["variant"] == variant}
        curve, wsum_prev = [], None
        for i, dt in enumerate(dates[:-1]):
            nxt = dates[i + 1]
            w = w_by_date.get(dt) or {}
            if not w:
                curve.append((dt, 0.0, 0.0))
                wsum_prev = None
                continue
            # 执行日 = 下一个交易日
            fut = px.index[px.index > dt]
            if len(fut) == 0:
                continue
            t1 = fut[0]
            seg = px.loc[(px.index >= t1) & (px.index <= nxt)]
            if len(seg) < 2:
                curve.append((dt, 0.0, 0.0))
                continue
            r = seg.iloc[-1] / seg.iloc[0] - 1.0
            # 等权口径：权重已在策略内归一化到 ~0.9，这里按权重直接加权
            port_r = float((r.reindex(list(w.keys())) * pd.Series(w)).sum(skipna=True))
            invested = float(sum(w.values()))
            curve.append((dt, port_r, invested))
            wsum_prev = invested
        out[variant] = curve
    return out


def _stats(curve):
    if not curve:
        return {}
    r = np.array([c[1] for c in curve], dtype=float)
    cum = float(np.prod(1 + r) - 1)
    n = len(r)
    # 每个调仓期约 rebalance 个交易日 → 年化
    per_year = 244.0 / max(config.DEFAULT_REBALANCE, 1)
    years = n / per_year if per_year else 0
    ann = (1 + cum) ** (1 / years) - 1 if years > 0 else 0.0
    vol = float(np.std(r, ddof=1) * np.sqrt(per_year)) if n > 1 else 0.0
    sharpe = (float(np.mean(r)) * per_year / vol) if vol > 1e-12 else 0.0
    eq = np.cumprod(1 + r)
    mdd = float((eq / np.maximum.accumulate(eq) - 1).min()) if n else 0.0
    return {"期数": n, "累计%": cum * 100, "年化%": ann * 100,
            "波动%": vol * 100, "夏普": sharpe, "回撤%": mdd * 100,
            "平均仓位%": float(np.mean([c[2] for c in curve])) * 100}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Faber 趋势过滤 并行验证报告")
    ap.add_argument("--min-weeks", type=int, default=getattr(
        config, "SHADOW_MIN_WEEKS", 8))
    ap.add_argument("--out", default="results/faber_shadow_report.csv")
    args = ap.parse_args(argv)

    db = TradeDB()
    rows = db.load_shadow_weights()
    print("=" * 96)
    print("  Faber 趋势过滤 · 并行验证报告")
    print("=" * 96)
    print("  实盘 trend_window = {}   影子 trend_window = {}".format(
        config.TREND_LIVE, config.TREND_SHADOW))

    if not rows:
        print("\n  ⚠️ 还没有任何并行记录（shadow_weights 表为空）。")
        print("  记录只在**调仓日**产生 —— `etf_rotation` 每 {} 个交易日调仓一次。".format(
            config.DEFAULT_REBALANCE))
        print("  下一次调仓日运行 `main.py simulate --ths` 后就会有记录。")
        return 0

    d0, d1 = rows[0]["date"], rows[-1]["date"]
    try:
        weeks = (pd.to_datetime(d1) - pd.to_datetime(d0)).days / 7.0
    except Exception:
        weeks = float("nan")
    print("  观察期 {} ~ {}（约 {:.1f} 周，{} 条记录）".format(
        d0, d1, weeks, len(rows)))

    codes = list(config.RECOMMENDED_POOLS.get("ETF全球") or [])
    store = DataStore()
    curves = _forward_returns(store, codes, rows)

    print("\n  ── 前向表现（真实数据，非回测）──")
    print("  " + "{:<10}{:>8}{:>11}{:>11}{:>11}{:>9}{:>11}{:>12}".format(
        "变体", "期数", "累计%", "年化%", "波动%", "夏普", "回撤%", "平均仓位%"))
    print("  " + "-" * 84)
    stats = {}
    for v, lbl in (("live", "实盘(Faber开)"), ("shadow", "影子(Faber关)")):
        st = _stats(curves.get(v, []))
        stats[v] = st
        if st:
            print("  " + "{:<10}{:>8}{:>11.2f}{:>11.2f}{:>11.2f}{:>9.2f}{:>11.2f}{:>12.1f}"
                  .format(lbl, st["期数"], st["累计%"], st["年化%"], st["波动%"],
                          st["夏普"], st["回撤%"], st["平均仓位%"]))

    # 两套权重的差异率
    lw = {r["date"]: r["weights"] for r in rows if r["variant"] == "live"}
    sw = {r["date"]: r["weights"] for r in rows if r["variant"] == "shadow"}
    common = sorted(set(lw) & set(sw))
    same = sum(1 for d in common if lw[d] == sw[d])
    print("\n  ── 两套权重的差异 ──")
    print("    共同调仓日 {} 个，其中权重**完全相同** {} 个（{:.0f}%）".format(
        len(common), same, (same / len(common) * 100) if common else 0))
    if len(common) == same:
        print("    ⚠️ 两套权重一直相同 → Faber 在观察期内**从未生效**"
              "（门槛不 binding），当前数据对决策没有信息量。")

    if len(stats) == 2 and stats["live"] and stats["shadow"]:
        dl = stats["shadow"]["年化%"] - stats["live"]["年化%"]
        ds = stats["shadow"]["夏普"] - stats["live"]["夏普"]
        print("\n  ── 判定 ──")
        print("    影子(关) − 实盘(开)：年化 {:+.2f}pp、夏普 {:+.2f}".format(dl, ds))
        if weeks == weeks and weeks < args.min_weeks:
            print("    ⏳ 观察期仅 {:.1f} 周 < 建议 {} 周 → **还不能下结论**，继续并行".format(
                weeks, args.min_weeks))
        else:
            verdict = ("关闭 Faber 更好" if dl > 0 and ds > 0 else
                       "开启 Faber 更好" if dl < 0 and ds < 0 else
                       "互有优劣（年化与夏普方向不一致）→ 看回撤与主观偏好")
            print("    ✅ 观察期已达 {:.1f} 周 → {}".format(weeks, verdict))

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    pd.DataFrame([{"变体": k, **v} for k, v in stats.items()]).to_csv(
        args.out, index=False, encoding="utf-8-sig")
    print("\n  结果已写出 {}".format(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
