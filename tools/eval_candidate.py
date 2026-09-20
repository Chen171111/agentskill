"""候选因子**五闸门**评估器（M1）。

> 规格：`docs/需求规格_自优化闭环模块.md` §3。**本文档是实现的唯一依据**，
> 五闸门判据在此逐条落地，实现方不得改判据。

为什么需要它
------------
本项目已证伪 5 条"自优化"路线，每一次都是**手工**跑几个脚本、人眼看几张表、
然后凭印象下结论。代价是：
- 容易漏项（邻域补了没？成本扣了没？秩相关算了没？）
- 不可复现（"我上次好像跑过"）
- **同一个已被否决的候选，过一阵又被重新提出来**

→ 本工具把评估变成**一条命令 + 一个 PASS/FAIL + 一张证据表**，
并把 FAIL 的候选写进 `state/refuted.jsonl`（**下次不许重走**）。

五闸门（判据精确、不可改）
--------------------------
| 闸门 | 判据 |
|---|---|
| ① 样本内外同号 | `Δ = 年化%(候选) − 年化%(基线)`，样本内 2019~2022 与样本外 2023~2026 **同时 > 0** |
| ② 邻域同向 | `--neighbors` 给的 ≥3 个点上，**样本外 Δ 全部 > 0** |
| ③ 净成本后为正 | **税后 + 10 万真实年化%** 的 Δ > 0（已含最低佣金 5 元惩罚） |
| ④ 增量非冗余 | 与 8 因子 + `dy_ttm` 的 max|Spearman ρ| **< 0.5**，且样本外 Δ > 0 |
| ⑤ 机制可解释 | 自动只输出**暴露差异表**；存在 ≥1 维度相对变化 >10% 且方向与 Δ 同向 → `REVIEW`，否则 `FAIL` |

总判定：①②③④ 全 PASS 且 ⑤ = `REVIEW` → **PASS**（进影子期，**仍需人工确认**）；
任一 FAIL → **FAIL** 并记入 `state/refuted.jsonl`。

用法
----
    $PY tools/eval_candidate.py --factor-col dy_fwd --name dyfwd_score \
        --usage score --weight 1.0 --adj-mode correct
    $PY tools/eval_candidate.py --factor-csv results/cand.csv --name c1 \
        --usage filter --threshold 0.06 --adj-mode correct

退出码：0 = PASS/REVIEW；1 = FAIL；2 = 参数/数据错误。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_dividend import prepare as prepare_div  # noqa: E402
from tools.backtest_stock import metrics, run  # noqa: E402
from tools.build_div_tax import load_div_tax, DEFAULT_DIV_TAX  # noqa: E402
from tools.panel_cache import build_panel  # noqa: E402
from tools.stats_lite import spearman  # noqa: E402
from tools.progress import flush_partial  # noqa: E402
from tools.sweep_dividend_into_mf import ALL8  # noqa: E402
from tools.test_dividend_factor import require_adj_mode  # noqa: E402
from tools.test_industry_neutral import (industry_exposure,  # noqa: E402
                                         mask_from_plan, plan_selections,
                                         real_ann)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADING_DAYS = 244.0

PERIODS = [("全区间", "20190101", "20260911"),
           ("样本内", "20190101", "20221231"),
           ("样本外", "20230101", "20260911")]
NEIGHBOR_PERIOD = ("样本外", "20230101", "20260911")

# 闸门④ 的参照因子（8 因子 + 股息率本身）
REF_FACTORS = [f for f, _ in ALL8] + ["dy_ttm"]


# ================================================================ 候选列
def load_candidate(df: pd.DataFrame, args) -> tuple[np.ndarray, dict]:
    """把候选因子对齐到面板，返回 `(values, 统计)`。"""
    if args.factor_col:
        if args.factor_col not in df.columns:
            raise SystemExit(f"❌ 面板里没有列 `{args.factor_col}`。"
                             f"可用列示例：dy_ttm / dy_fwd / vol20 / illiq20")
        v = pd.to_numeric(df[args.factor_col], errors="coerce").values.astype(float)
        src = f"面板列 {args.factor_col}"
    else:
        c = pd.read_csv(args.factor_csv)
        need = {"date", "code", args.name}
        if not need.issubset(c.columns):
            raise SystemExit(f"❌ --factor-csv 需含列 {sorted(need)}，实得 {list(c.columns)}")
        c = c[["date", "code", args.name]].copy()
        c["date"] = c.date.astype(str).str.replace("-", "", regex=False)
        c[args.name] = pd.to_numeric(c[args.name], errors="coerce")
        v = df[["date", "code"]].merge(c, on=["date", "code"], how="left")[args.name].values
        v = v.astype(float)
        src = f"CSV {args.factor_csv}"

    n_all, n_nan = len(v), int(np.isnan(v).sum())
    stat = {"source": src, "rows": n_all, "nan": n_nan,
            "nan_ratio": round(n_nan / max(n_all, 1), 6)}
    if args.na_fill == "zero":
        v = np.nan_to_num(v, nan=0.0)
    elif args.na_fill == "median":
        med = pd.Series(v, index=df.index).groupby(df.date.values).transform("median")
        v = np.where(np.isnan(v), med.values, v)
    # na_fill=none：保持 NaN，由后续按用途处理（filter → 视为不合格；score → 加 0）
    stat["dropped_after_fill"] = int(np.isnan(v).sum())
    return v, stat


def _daily_rank(df: pd.DataFrame, v: np.ndarray) -> np.ndarray:
    """逐日横截面百分位（0~1），NaN → 0。"""
    s = pd.Series(v, index=df.index)
    r = s.groupby(df.date.values).rank(pct=True)
    return r.fillna(0.0).values


# ================================================================ 回测
def run_plan(df, by_date, plan, s: str, e: str, args) -> dict:
    """跑一个选股计划，返回指标 dict（含 10 万真实年化）。"""
    df["_sig"] = mask_from_plan(df, plan, by_date)
    eq, tr, meta = run(df, [], start=s, end=e, hold=args.hold, cond_col="_sig",
                       min_price=args.min_price, min_amount=args.min_amount,
                       min_listed=args.min_listed,
                       div_tax=(load_div_tax(args.div_tax)
                                if args.tax_rate else None),
                       tax_rate=args.tax_rate)
    m = metrics(eq.equity)
    nh = meta.get("avg_hold", 0) or 1
    yrs = len(eq) / TRADING_DAYS
    turn = (int((tr.side == "buy").sum()) / yrs / nh) if len(tr) else 0.0
    return {"年化%": m["年化收益"], "夏普": m["夏普比率"], "回撤%": m["最大回撤"],
            "平均持仓": nh, "年单边换手": turn, "交易笔数": len(tr),
            "n_skip": meta.get("n_skip", 0),
            "10万真实年化%": real_ann(m["年化收益"], turn, nh, 10.0),
            "_eq": eq, "_meta": meta}


def make_plan(df, dates, by_date, args, *, topn: int, extra_ok=None,
              extra_score=None, mode: str = "indpct") -> dict:
    return plan_selections(df, dates, by_date, mode=mode, topn=topn, hold=args.hold,
                           dy_col=args.dy_col, min_dy=args.min_dy, max_dy=args.max_dy,
                           min_div3=args.min_div3, extra_ok=extra_ok,
                           extra_score=extra_score)


# ================================================================ 闸门④ 秩相关
def rank_corr(df, dates, by_date, cand: np.ndarray, args) -> tuple[float, list]:
    """候选 vs 参照因子的逐日横截面 Spearman，取时间中位。返回 (max|ρ|, top3)。"""
    step = max(1, len(dates) // 60)          # 最多抽 60 个交易日，控制耗时
    sample_dates = dates[::step]
    cols = [c for c in REF_FACTORS if c in df.columns]
    rho: dict[str, list] = {c: [] for c in cols}
    cs = pd.Series(cand, index=df.index)
    for t in sample_dates:
        rows = by_date.get(t)
        if rows is None or len(rows) < 30:
            continue
        a = cs.iloc[rows]
        if a.notna().sum() < 30 or a.nunique() < 5:
            continue
        for c in cols:
            b = pd.to_numeric(df[c].iloc[rows], errors="coerce")
            # ⚠️ 不用 `pandas.corr("spearman")` —— 它会转调 scipy（本 venv 无 scipy）。
            #    用 tools/stats_lite.spearman（rank + Pearson，等价实现）。
            r = spearman(a.values, b.values, min_n=30)
            if np.isfinite(r):
                rho[c].append(r)
    med = {c: float(np.nanmedian(v)) for c, v in rho.items() if v}
    top3 = sorted(med.items(), key=lambda kv: -abs(kv[1]))[:3]
    mx = max((abs(x) for _, x in med.items()), default=float("nan"))
    return (float(mx) if med else float("nan")), \
           [{"factor": k, "rho": round(v, 4)} for k, v in top3]


# ================================================================ 闸门⑤ 暴露
def exposure_profile(df, dates, by_date, plan: dict, args, *, topn: int) -> dict:
    """组合暴露画像（逐调仓日算，取中位）。"""
    exp = industry_exposure(df, plan, topn=topn)
    # HHI 与行业数直接由行业权重算（`concentration()` 的键是 组合HHI/前3 等，不含这两个）
    if len(exp) and "组合权重" in exp.columns:
        w = exp.组合权重.values
        ind_hhi = float((w ** 2).sum())
        ind_cnt = float((w > 0).sum())
    else:
        ind_hhi = ind_cnt = float("nan")

    # 因子暴露：持仓在**当日横截面百分位**上的均值（跨日可比）
    out = {"行业HHI": ind_hhi, "行业数": ind_cnt}
    for f in ("vol20", "illiq20"):
        if f not in df.columns:
            continue
        pct = pd.Series(df[f].values, index=df.index) \
            .groupby(df.date.values).rank(pct=True)
        vals = []
        for t, sel in plan.items():
            rows = by_date.get(t)
            if rows is None or not sel:
                continue
            codes = df.code.values[rows]
            m = np.isin(codes, sel)
            if m.sum() == 0:
                continue
            vals.append(float(np.nanmean(pct.values[rows][m])))
        out[f"{f}暴露"] = float(np.nanmedian(vals)) if vals else float("nan")
    dy_means = []
    if args.dy_col in df.columns:
        for t, sel in plan.items():
            rows = by_date.get(t)
            if rows is None or not sel:
                continue
            codes = df.code.values[rows]
            m = np.isin(codes, sel)
            if m.sum() == 0:
                continue
            dy_means.append(float(np.nanmean(df[args.dy_col].values[rows][m])))
    out["平均dy"] = float(np.nanmedian(dy_means)) if dy_means else float("nan")
    return out


def gate5(base_exp: dict, cand_exp: dict, d_oos: float) -> tuple[str, list]:
    """⑤：只输出证据 + REVIEW/FAIL，**不定论 PASS**。"""
    rows = []
    ok_dir = False
    for k in base_exp:
        b, c = base_exp.get(k), cand_exp.get(k)
        if b is None or c is None or not np.isfinite(b) or not np.isfinite(c) or b == 0:
            continue
        rel = (c - b) / abs(b)
        rows.append({"维度": k, "基线": round(float(b), 4), "候选": round(float(c), 4),
                     "相对变化": round(float(rel), 4)})
        if abs(rel) > 0.10 and np.sign(rel) == np.sign(d_oos):
            ok_dir = True
    return ("REVIEW" if ok_dir else "FAIL"), rows


# ================================================================ 主流程
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="候选因子五闸门评估器")
    ap.add_argument("--name", required=True, help="候选名（用于产物命名与证伪清单）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--factor-col", help="面板里已有的列名")
    g.add_argument("--factor-csv", help="CSV，需含列 date,code,<name>")
    ap.add_argument("--usage", required=True, choices=["filter", "score"])
    ap.add_argument("--threshold", type=float, default=None,
                    help="usage=filter：因子 ≥ 该值才算候选")
    ap.add_argument("--weight", type=float, default=1.0,
                    help="usage=score：并入权重（0~1 建议，量纲与行业内百分位一致）")
    ap.add_argument("--na-fill", default="none", choices=["none", "zero", "median"])
    ap.add_argument("--bars", default="data/stockbars/bars_total.parquet")
    ap.add_argument("--tax-rate", type=float, default=0.1,
                    help="红利税率（0~0.2）。**默认 0.1 = 保持原有「税后」语义**；"
                         "传 0 得无税。引擎内在**除权日**按持仓扣税、价格路径不变 → "
                         "税后与无税**组合恒等**（D4）")
    ap.add_argument("--div-tax", default=DEFAULT_DIV_TAX,
                    help="引擎内扣税用的每股派现表（tools/build_div_tax.py 生成）")
    ap.add_argument("--bfq", default="data/stockbars/bars_bfq.parquet")
    ap.add_argument("--dividends", default="data/dividends/bonus_all.parquet")
    ap.add_argument("--universe", default="data/stockbars/universe_all.csv")
    ap.add_argument("--price-col", default="px_real")
    ap.add_argument("--adj-mode", default=None, choices=["legacy", "correct"])
    ap.add_argument("--topn", type=int, default=20)
    ap.add_argument("--hold", type=int, default=60)
    ap.add_argument("--dy-col", default="dy_ttm")
    ap.add_argument("--min-dy", type=float, default=0.5)
    ap.add_argument("--max-dy", type=float, default=10.0)
    ap.add_argument("--min-div3", type=int, default=2)
    ap.add_argument("--min-price", type=float, default=2.0)
    ap.add_argument("--min-amount", type=float, default=3e7)
    ap.add_argument("--min-listed", type=int, default=120)
    ap.add_argument("--neighbors", default="topn=15,20,30",
                    help="② 邻域，形如 topn=15,20,30")
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args(argv)

    args.out = args.out or f"results/eval_{args.name}.csv"
    args.json = args.json or f"results/eval_{args.name}.json"
    if args.usage == "filter" and args.threshold is None:
        print("❌ usage=filter 必须给 --threshold", file=sys.stderr)
        return 2

    t_start = time.time()
    print("=" * 104)
    print(f"  候选因子五闸门评估 —— {args.name}（usage={args.usage}）")
    print("=" * 104)
    df, dates, by_date, cache_hit = build_panel(args, use_cache=not args.no_cache)
    print(f"  面板 {len(df):,} 行 / {df.code.nunique():,} 只 / {len(dates):,} 交易日"
          f"  adj_mode={require_adj_mode(args)}", flush=True)

    cand, cand_stat = load_candidate(df, args)
    print(f"  候选来源 {cand_stat['source']}｜NaN {cand_stat['nan']:,}"
          f"（{cand_stat['nan_ratio']*100:.2f}%）", flush=True)

    cand_rank = _daily_rank(df, cand)
    if args.usage == "filter":
        extra_ok = cand >= args.threshold
        extra_ok = np.where(np.isnan(cand), False, extra_ok)
        print(f"  附加过滤：{args.factor_col or args.name} ≥ {args.threshold} → "
              f"合格行占比 {extra_ok.mean()*100:.2f}%", flush=True)
    else:
        extra_score = args.weight * cand_rank
        print(f"  并入打分：weight={args.weight}（与行业内百分位同量纲 0~1）", flush=True)

    # ---------- 逐区间跑 基线 / 候选 ----------
    detail = []
    per = {}
    for ptag, s, e in PERIODS:
        print(f"\n  ── {ptag}（{s}~{e}）" + "─" * 46, flush=True)
        base_plan = make_plan(df, dates, by_date, args, topn=args.topn)
        cand_plan = make_plan(df, dates, by_date, args, topn=args.topn,
                              extra_ok=(extra_ok if args.usage == "filter" else None),
                              extra_score=(extra_score if args.usage == "score" else None))
        rb = run_plan(df, by_date, base_plan, s, e, args)
        rc = run_plan(df, by_date, cand_plan, s, e, args)
        per[ptag] = (rb, rc, base_plan, cand_plan)
        for tag, r, plan in (("基线 indpct", rb, base_plan), ("候选", rc, cand_plan)):
            detail.append({"区间": ptag, "构造": tag, "邻域点": f"topn={args.topn}",
                           **{k: r[k] for k in ("年化%", "夏普", "回撤%", "平均持仓",
                                                "年单边换手", "交易笔数", "n_skip",
                                                "10万真实年化%")}})
            print(f"    {tag:<12} 年化 {r['年化%']:>7.2f}%  夏普 {r['夏普']:>5.2f}  "
                  f"回撤 {r['回撤%']:>7.2f}%  持仓 {r['平均持仓']:>5.1f}  "
                  f"换手 {r['年单边换手']:>4.2f}x  10万真实 {r['10万真实年化%']:>7.2f}%",
                  flush=True)
        d = rc["年化%"] - rb["年化%"]
        print(f"    Δ年化 = {d:>+7.2f}pp ｜ Δ10万真实 = "
              f"{rc['10万真实年化%'] - rb['10万真实年化%']:>+7.2f}pp", flush=True)
        flush_partial(detail, f"results/_eval_{args.name}_detail_partial.csv", tag=ptag)

    # ---------- ① 样本内外同号 ----------
    d_in = per["样本内"][1]["年化%"] - per["样本内"][0]["年化%"]
    d_oos = per["样本外"][1]["年化%"] - per["样本外"][0]["年化%"]
    gate1 = bool(d_in > 0 and d_oos > 0)
    print(f"\n  ① 样本内外同号：Δ内 {d_in:+.2f}pp / Δ外 {d_oos:+.2f}pp → "
          f"{'✓ PASS' if gate1 else '✗ FAIL'}")

    # ---------- ② 邻域同向 ----------
    neigh = []
    for spec in args.neighbors.split(","):
        spec = spec.strip()
        k, _, vv = spec.partition("=")
        if not vv:
            continue
        n = int(vv)
        ptag, s, e = NEIGHBOR_PERIOD
        params = {"topn": n} if k == "topn" else {"topn": args.topn}
        b = make_plan(df, dates, by_date, args, **params)
        if k == "hold":
            a2 = SimpleNamespace(**vars(args))
            a2.hold = n
            cb = make_plan(df, dates, by_date, a2, topn=args.topn)
            cc = make_plan(df, dates, by_date, a2, topn=args.topn,
                           extra_ok=(extra_ok if args.usage == "filter" else None),
                           extra_score=(extra_score if args.usage == "score" else None))
            rbb = run_plan(df, by_date, cb, s, e, a2)
            rcc = run_plan(df, by_date, cc, s, e, a2)
        else:
            rbb = run_plan(df, by_date, b, s, e, args)
            cc = make_plan(df, dates, by_date, args, topn=n,
                           extra_ok=(extra_ok if args.usage == "filter" else None),
                           extra_score=(extra_score if args.usage == "score" else None))
            rcc = run_plan(df, by_date, cc, s, e, args)
        dd = rcc["年化%"] - rbb["年化%"]
        neigh.append({"点": spec, "Δ年化pp": dd})
        detail.append({"区间": ptag, "构造": "基线 indpct", "邻域点": spec,
                       **{k2: rbb[k2] for k2 in ("年化%", "夏普", "回撤%", "平均持仓",
                                                 "年单边换手", "交易笔数", "n_skip",
                                                 "10万真实年化%")}})
        detail.append({"区间": ptag, "构造": "候选", "邻域点": spec,
                       **{k2: rcc[k2] for k2 in ("年化%", "夏普", "回撤%", "平均持仓",
                                                 "年单边换手", "交易笔数", "n_skip",
                                                 "10万真实年化%")}})
        print(f"  ② 邻域 {spec:<12} Δ外 {dd:>+7.2f}pp", flush=True)
    gate2 = bool(len(neigh) >= 3 and all(x["Δ年化pp"] > 0 for x in neigh))

    # ---------- ③ 净成本（10 万真实） ----------
    d_real = per["样本外"][1]["10万真实年化%"] - per["样本外"][0]["10万真实年化%"]
    gate3 = bool(d_real > 0)

    # ---------- ④ 增量非冗余 ----------
    if args.usage == "score":
        # "把 dy 自己再加一遍"这类必然冗余的候选，必须被抓住
        mx, top3 = rank_corr(df, dates, by_date, cand, args)
        gate4 = bool(np.isfinite(mx) and mx < 0.5 and d_oos > 0)
    else:
        mx, top3 = rank_corr(df, dates, by_date, cand, args)
        gate4 = bool(np.isfinite(mx) and mx < 0.5 and d_oos > 0)

    # ---------- ⑤ 机制（只给证据） ----------
    be = exposure_profile(df, dates, by_date, per["样本外"][2], args, topn=args.topn)
    ce = exposure_profile(df, dates, by_date, per["样本外"][3], args, topn=args.topn)
    g5, exp_rows = gate5(be, ce, d_oos)

    verdict = "PASS" if (gate1 and gate2 and gate3 and gate4 and g5 == "REVIEW") else "FAIL"
    reason = []
    if not gate1:
        reason.append(f"样本内外不同号(Δ内{d_in:+.2f}/Δ外{d_oos:+.2f})")
    if not gate2:
        reason.append("邻域非全同向")
    if not gate3:
        reason.append(f"净成本后不为正({d_real:+.3f}pp)")
    if not gate4:
        reason.append(f"冗余或与参照因子过相关(max|ρ|={mx:.3f})" if np.isfinite(mx)
                      else "秩相关不可算")
    if g5 == "FAIL":
        reason.append("暴露无同向显著差异")

    # ---------- 输出 ----------
    print("\n" + "=" * 104)
    print(f"  闸门  ① {'✓' if gate1 else '✗'}   ② {'✓' if gate2 else '✗'}   "
          f"③ {'✓' if gate3 else '✗'}   ④ {'✓' if gate4 else '✗'}   ⑤ {g5}")
    print(f"  Δ年化：样本内 {d_in:+.2f}pp ｜ 样本外 {d_oos:+.2f}pp ｜ "
          f"10万真实(样本外) {d_real:+.3f}pp ｜ max|ρ| {mx:.3f}")
    print(f"\n  >>> verdict = {verdict}" + (f"   原因：{'；'.join(reason)}" if reason else ""))
    print("\n  ⑤ 暴露差异（样本外，中位）：")
    for r in exp_rows:
        print("    {维度:<12} 基线 {基线:>8.4f}  候选 {候选:>8.4f}  相对变化 {相对变化:>+8.2%}"
              .format(**r))
    print("=" * 104)

    os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)
    pd.DataFrame([{"name": args.name, "usage": args.usage,
                   "gate1": gate1, "gate2": gate2, "gate3": gate3, "gate4": gate4,
                   "gate5": g5, "verdict": verdict,
                   "d_in_pp": round(d_in, 4), "d_oos_pp": round(d_oos, 4),
                   "d_real10_oos_pp": round(d_real, 4),
                   "max_abs_rho": round(mx, 4) if np.isfinite(mx) else None,
                   "top_rho": json.dumps(top3, ensure_ascii=False),
                   "reason": "；".join(reason), "adj_mode": args.adj_mode,
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
                  ]).to_csv(args.out, index=False, encoding="utf-8-sig")
    pd.DataFrame(detail).to_csv(args.out.replace(".csv", "_detail.csv"),
                               index=False, encoding="utf-8-sig")
    payload = {"summary": {"name": args.name, "usage": args.usage, "verdict": verdict,
                           "gate1": gate1, "gate2": gate2, "gate3": gate3,
                           "gate4": gate4, "gate5": g5,
                           "d_in_pp": d_in, "d_oos_pp": d_oos,
                           "d_real10_oos_pp": d_real,
                           "max_abs_rho": (None if not np.isfinite(mx) else mx),
                           "top_rho": top3, "reason": reason},
               "neighbors": neigh, "exposure_base": be, "exposure_cand": ce,
               "exposure_diff": exp_rows, "candidate": cand_stat,
               "cache_hit": cache_hit, "elapsed_sec": round(time.time() - t_start, 1)}
    with open(args.json, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    if verdict == "FAIL":
        with open(os.path.join(ROOT, "state", "refuted.jsonl"), "a",
                  encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "name": args.name, "usage": args.usage,
                                 "verdict": verdict, "reason": "；".join(reason),
                                 "evidence_file": args.out},
                                ensure_ascii=False) + "\n")
        print(f"  已记入证伪清单 state/refuted.jsonl")

    print(f"\n  产物：{args.out} / {args.out.replace('.csv','_detail.csv')} / {args.json}")
    print(f"  总用时 {(time.time()-t_start)/60:.1f} min")
    return 1 if verdict == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
