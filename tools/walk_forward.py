"""Purged + Embargo Walk-Forward，含 **DSR / PBO**（M3）。

> 规格：`docs/需求规格_自优化闭环模块.md` §5.1。

为什么需要它
------------
现有样本内外是**按日历硬切**（2019-2022 / 2023-2026），但标签是 **60 日前向收益** ——
切分点附近的训练样本，其标签**伸进了测试段**，这就是信息泄露。
后果：IC 的 t 值被系统性高估，而本项目已经用 28+ 个配置做过多次网格搜索却**从未做多重检验校正**。

本脚本做三件事：
1. **purged + embargo** 切分：训练段末尾剔除 `label_horizon` 天（purge）＋ 额外 `embargo` 天
2. **DSR**（Deflated Sharpe Ratio，Bailey & López de Prado）：把"搜了多少个配置"折算成对夏普的折扣
3. **PBO**（Probability of Backtest Overfitting，CSCV）：样本内最优在样本外落到后 50% 的概率

⚠️ venv 只有 pandas/numpy/pyarrow → 正态 CDF 用 `math.erf` 自实现，**不得引入 scipy**
（规格 §2.1）。

用法
----
    $PY tools/walk_forward.py --adj-mode correct --label-horizon 60 --embargo 60 \
        --n-folds 5 --n-splits 16 --n-trials 28 --json results/walkforward.json

退出码：0 正常；2 参数/数据错误。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.panel_cache import build_panel  # noqa: E402
from tools.stats_lite import EULER, ncdf, nppf, spearman  # noqa: E402
from tools.progress import flush_partial  # noqa: E402
from tools.sweep_dividend_into_mf import ALL8  # noqa: E402
from tools.test_dividend_factor import require_adj_mode  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 正态分布与 Spearman 统一来自 tools/stats_lite.py（单一来源；venv 无 scipy）


# ---------------------------------------------------------------- purged + embargo
def make_folds(dates: list[str], *, n_folds: int, label_horizon: int,
               embargo: int, min_train_days: int) -> list[dict]:
    """返回 `[{train_start, train_end, test_start, test_end, ...}]`（按日期字符串）。

    训练段 = `[dates[0], test_start)` **再往前退 `label_horizon + embargo` 个交易日**；
    这样训练样本的 60 日标签不会伸进测试段。
    """
    n = len(dates)
    usable = n - min_train_days - label_horizon - embargo
    if usable <= n_folds:
        raise SystemExit(f"❌ 数据不足以切 folds：n={n} min_train={min_train_days} "
                         f"purge={label_horizon} embargo={embargo}")
    test_len = usable // n_folds
    folds = []
    first_test = n - usable
    for k in range(n_folds):
        ts = first_test + k * test_len
        te = n if k == n_folds - 1 else ts + test_len
        train_hi = ts - label_horizon - embargo          # 训练段右端（不含）
        if train_hi < min_train_days:
            continue
        folds.append({
            "fold": k + 1,
            "train": (dates[0], dates[train_hi - 1]),
            "test": (dates[ts], dates[te - 1]),
            "purged_days": len(dates[train_hi:ts]),
            "train_days": train_hi, "test_days": te - ts,
        })
    return folds


# ---------------------------------------------------------------- IC 计算
def cs_ic(dy: np.ndarray, fwd: np.ndarray) -> float:
    # ⚠️ 不用 pandas.corr("spearman")：本 venv 无 scipy，pandas 内部会 import 它。
    return spearman(dy, fwd, min_n=50)


def ic_matrix(df, dates, by_date, factors: list[str], *, fwd_col: str,
              step: int) -> pd.DataFrame:
    """逐日横截面 IC 矩阵：index=日期（抽样），columns=因子。"""
    fwd = df[fwd_col].values
    data = {}
    for f in factors:
        if f not in df.columns:
            continue
        v = pd.to_numeric(df[f], errors="coerce").values
        data[f] = [cs_ic(v[rows], fwd[rows]) if (rows := by_date.get(t)) is not None
                   else np.nan for t in dates[::step]]
    idx = dates[::step]
    return pd.DataFrame(data, index=idx)


# ---------------------------------------------------------------- DSR
def deflated_sharpe(ic_series: np.ndarray, *, n_trials: int,
                    var_sr: float | None = None) -> dict:
    """Bailey & López de Prado 的 DSR（Deflated Sharpe Ratio）。

    `ic_series` = 逐期 IC（把 IC 序列当作"策略收益"），SR 用它的 Sharpe；
    `n_trials` = 一共试过多少个配置（**必须由调用方如实给出**）；
    `var_sr` = **跨试验的 SR 方差** `Var(SR_n)`。

    ⚠️ `var_sr` 不能拍脑袋：DSR 里的 `SR0`（"纯运气能达到的最高 SR"）正比于
       `sqrt(Var(SR_n))`。本实现用**同一批被检验因子的实际 SR 方差**估计它
       （9 个因子 → 8 自由度），而不是假设 1.0 —— 假设 1.0 会让 SR0 虚高、
       DSR 恒为 0，从而**丧失区分度**。
    """
    x = np.asarray([v for v in ic_series if np.isfinite(v)], dtype=float)
    if x.size < 20 or x.std(ddof=1) == 0:
        return {"sr": float("nan"), "sr0": float("nan"), "dsr": float("nan"),
                "n_obs": int(x.size)}
    sr = float(x.mean() / x.std(ddof=1))                    # 每期 Sharpe
    skew = float(pd.Series(x).skew())
    kurt = float(pd.Series(x).kurt()) + 3.0                 # pandas 给超额峰度
    n = x.size
    sig = math.sqrt(1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr ** 2)
    if not math.isfinite(sig) or sig <= 0:
        sig = 1.0
    t = max(int(n_trials), 2)
    v = float(var_sr) if (var_sr is not None and np.isfinite(var_sr)
                          and var_sr > 0) else 1.0
    sr0 = math.sqrt(v) * (
        (1 - EULER) * nppf(1 - 1.0 / t) + EULER * nppf(1 - 1.0 / (t * math.e)))
    z = (sr - sr0) * math.sqrt(max(n - 1, 1)) / sig
    return {"sr": round(sr, 6), "sr0": round(sr0, 6), "dsr": round(ncdf(z), 6),
            "n_obs": int(n), "skew": round(skew, 4), "kurt": round(kurt, 4),
            "var_sr_used": round(v, 6)}


# ---------------------------------------------------------------- PBO (CSCV)
def pbo_cscv(mat: np.ndarray, *, n_splits: int, max_combos: int = 2000,
             seed: int = 0) -> dict:
    """CSCV（组合对称交叉验证）：样本内最优在样本外落到后 50% 的频率。

    `mat` = (n_obs, n_trials) 绩效矩阵（这里用逐期 IC）。
    ⚠️ `C(16,8)=12870` 组合会偏慢 → 超过 `max_combos` 时**随机抽样**并记录实际组合数。
    """
    n_obs, n_trials = mat.shape
    if n_obs < n_splits or n_trials < 2:
        return {"pbo": float("nan"), "n_combos": 0, "reason": "样本或试验数不足"}
    s = n_splits if n_splits % 2 == 0 else n_splits - 1
    s = max(2, min(s, n_obs))
    bounds = np.linspace(0, n_obs, s + 1).astype(int)
    groups = [np.arange(bounds[i], bounds[i + 1]) for i in range(s)]

    from itertools import combinations
    combos = list(combinations(range(s), s // 2))
    rng = np.random.default_rng(seed)
    if len(combos) > max_combos:
        pick = rng.choice(len(combos), max_combos, replace=False)
        combos = [combos[i] for i in pick]
    logits = []
    for is_idx in combos:
        is_rows = np.concatenate([groups[i] for i in is_idx])
        oos_rows = np.concatenate([groups[i] for i in range(s) if i not in is_idx])
        if is_rows.size == 0 or oos_rows.size == 0:
            continue
        with np.errstate(invalid="ignore"):
            is_perf = np.nanmean(mat[is_rows], axis=0)
            oos_perf = np.nanmean(mat[oos_rows], axis=0)
        best = int(np.nanargmax(is_perf))
        rank = float((oos_perf < oos_perf[best]).sum()) / max((~np.isnan(oos_perf)).sum() - 1, 1)
        w = min(max(rank, 1e-6), 1 - 1e-6)
        logits.append(math.log(w / (1 - w)))
    if not logits:
        return {"pbo": float("nan"), "n_combos": 0, "reason": "无有效组合"}
    lg = np.array(logits)
    return {"pbo": round(float((lg <= 0).mean()), 6),
            "n_combos": len(logits),
            "logit_median": round(float(np.median(lg)), 4)}


# ---------------------------------------------------------------- 主流程
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="purged+embargo walk-forward + DSR/PBO")
    ap.add_argument("--bars", default="data/stockbars/bars_total.parquet")
    ap.add_argument("--bfq", default="data/stockbars/bars_bfq.parquet")
    ap.add_argument("--dividends", default="data/dividends/bonus_all.parquet")
    ap.add_argument("--universe", default="data/stockbars/universe_all.csv")
    ap.add_argument("--price-col", default="px_real")
    ap.add_argument("--adj-mode", default=None, choices=["legacy", "correct"])
    ap.add_argument("--dy-col", default="dy_ttm")
    ap.add_argument("--min-price", type=float, default=2.0)
    ap.add_argument("--min-amount", type=float, default=3e7)
    ap.add_argument("--min-listed", type=int, default=120)
    ap.add_argument("--label-horizon", type=int, default=60)
    ap.add_argument("--embargo", type=int, default=60)
    ap.add_argument("--n-folds", type=int, default=5)
    ap.add_argument("--min-train-days", type=int, default=500)
    ap.add_argument("--n-trials", type=int, default=28,
                    help="**如实**填写一共试过多少个配置（DSR 用它做折扣）")
    ap.add_argument("--n-splits", type=int, default=16)
    ap.add_argument("--factor", default=None, help="指定评估因子；默认取全样本 IC 最高者")
    ap.add_argument("--ic-step", type=int, default=2)
    ap.add_argument("--out", default="results/walkforward.csv")
    ap.add_argument("--json", default="results/walkforward.json")
    args = ap.parse_args(argv)

    t0 = time.time()
    print("=" * 104)
    print("  Purged + Embargo Walk-Forward（含 DSR / PBO）")
    print("=" * 104)
    df, dates, by_date, _ = build_panel(args)
    print(f"  面板 {len(df):,} 行｜{len(dates):,} 交易日｜adj_mode="
          f"{require_adj_mode(args)}｜标签 {args.label_horizon} 日", flush=True)

    # 前向收益（标签）：总收益序列的前 60 日收益
    g = df.groupby("code", sort=False).close
    df["_fwd"] = g.shift(-args.label_horizon) / df.close - 1.0

    factors = [f for f, _ in ALL8] + [args.dy_col]
    print(f"  因子 {len(factors)} 个：{factors}", flush=True)
    icm = ic_matrix(df, dates, by_date, factors, fwd_col="_fwd", step=args.ic_step)
    print(f"  IC 矩阵 {icm.shape[0]} 期 × {icm.shape[1]} 因子", flush=True)

    # ---------- folds ----------
    folds = make_folds(dates, n_folds=args.n_folds, label_horizon=args.label_horizon,
                       embargo=args.embargo, min_train_days=args.min_train_days)
    fcol = args.factor or icm.mean().idxmax()
    if fcol not in icm.columns:
        print(f"❌ 因子 `{fcol}` 不在 IC 矩阵里", file=sys.stderr)
        return 2
    print(f"\n  评估因子：{fcol}（默认取全样本 IC 最高）")

    # ---------- 逐折 ----------
    rows = []
    for f in folds:
        tr = icm.loc[(icm.index >= f["train"][0]) & (icm.index <= f["train"][1]), fcol]
        te = icm.loc[(icm.index >= f["test"][0]) & (icm.index <= f["test"][1]), fcol]
        rows.append({"fold": f["fold"], "train_start": f["train"][0],
                     "train_end": f["train"][1], "test_start": f["test"][0],
                     "test_end": f["test"][1], "purged_days": f["purged_days"],
                     "train_days": f["train_days"], "test_days": f["test_days"],
                     "IC_train": tr.mean(), "IC_test": te.mean(),
                     "IC_test_n": int(te.notna().sum())})
        print(f"    fold {f['fold']}: train {f['train'][0]}~{f['train'][1]}"
              f"（{f['train_days']}日）｜purge+embargo {f['purged_days']}日"
              f"｜test {f['test'][0]}~{f['test'][1]}（{f['test_days']}日）"
              f"｜IC_train {tr.mean():+.4f}  IC_test {te.mean():+.4f}", flush=True)
        flush_partial(rows, args.out, tag=f"fold{f['fold']}")   # 增量落盘（被掐断不丢）
    fr = pd.DataFrame(rows)
    if fr.empty:
        print("❌ 没有有效 fold", file=sys.stderr)
        return 2
    oos_part = fr.loc[fr.test_end >= dates[len(dates) // 2], "IC_test"]
    print(f"\n  OOS IC：全折均值 {fr.IC_test.mean():+.4f}｜后半段均值 "
          f"{oos_part.mean():+.4f}｜折间一致性 "
          f"{(fr.IC_test > 0).sum()}/{len(fr)} 为正")

    # ---------- DSR / PBO ----------
    # 跨试验 SR 方差：用同一批被检验因子的实际 SR（比假设 1.0 有区分度）
    _srs = [(icm[c].mean() / icm[c].std(ddof=1)) for c in icm.columns
            if icm[c].std(ddof=1) and np.isfinite(icm[c].std(ddof=1))]
    _var_sr = float(np.var(_srs, ddof=1)) if len(_srs) >= 3 else None
    print(f"  跨试验 SR 方差 Var(SR_n) = {_var_sr}（由 {len(_srs)} 个因子估计）")
    dsr = deflated_sharpe(icm[fcol].values, n_trials=args.n_trials, var_sr=_var_sr)
    pb = pbo_cscv(icm.values, n_splits=args.n_splits)
    print(f"\n  DSR：SR={dsr['sr']}  SR0={dsr['sr0']}  **DSR={dsr['dsr']}**"
          f"（n_obs={dsr['n_obs']}, n_trials={args.n_trials}）")
    print(f"  PBO：**{pb['pbo']}**（{pb['n_combos']} 个 CSCV 组合，"
          f"logit 中位 {pb.get('logit_median')}）")
    print("  读法：DSR 越接近 1 越好（已扣除多重检验）；"
          "PBO > 0.5 表示『样本内最优』在样本外大概率落到后半，即大概率过拟合。")

    pd.DataFrame(rows).to_csv(args.out, index=False, encoding="utf-8-sig")
    payload = {"factor": fcol, "label_horizon": args.label_horizon,
               "embargo": args.embargo, "n_folds": len(folds),
               "n_trials": args.n_trials, "n_splits": args.n_splits,
               "var_sr": _var_sr, "factor_srs": {c: round(float(icm[c].mean()
               / icm[c].std(ddof=1)), 6) for c in icm.columns
               if icm[c].std(ddof=1) and np.isfinite(icm[c].std(ddof=1))},
               "ic_full": float(icm[fcol].mean()),
               "ic_oos_mean": float(fr.IC_test.mean()),
               "ic_folds_positive": int((fr.IC_test > 0).sum()),
               "folds": rows, "dsr": dsr, "pbo": pb,
               "factors": factors, "elapsed_sec": round(time.time() - t0, 1)}
    with open(args.json, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
    flush_partial(rows, args.out, tag="final")
    print(f"\n  产物：{args.out} / {args.json}｜用时 {(time.time()-t0)/60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
