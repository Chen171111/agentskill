"""特征面板构建与**指纹缓存**（共享工具）。

> 抽出为共享模块的原因（规格偏差说明，见 `docs/需求规格_自优化闭环模块.md` §八 备注）：
> `eval_candidate`（M1）与 `monitor_factors`（M2）都要"构建/读取面板"。
> 若各写一份，就是 `tools/README.md` 铁律 14 说的「同一判据在多处实现＝迟早分叉」；
> 若互相 import，又违反规格里"两模块不互相依赖"的约定。→ 因此抽成共享工具。

缓存设计（对应规格 §六-6）
--------------------------
- 缓存 key = sha1(数据文件 mtime+size ＋ 全部影响面板的参数)[:12]
- 输出 `data/cache/panel_<key>.parquet`
- ⚠️ **禁止只用"文件存在"当命中条件** —— 数据一更新就必须失效，
  否则会拿旧面板算出"新数据下的结论"（本项目吃过多次"数据更新了但结论没更新"的亏）。
"""
from __future__ import annotations

import hashlib
import os
import time
from types import SimpleNamespace

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_INDUSTRY = "data/industry/industry_all.parquet"


def fingerprint(args) -> str:
    parts = []
    ind_p = getattr(args, "industry", DEFAULT_INDUSTRY)
    for p in (args.bars, args.bfq, args.dividends, args.universe, ind_p):
        f = os.path.join(ROOT, p)
        if os.path.exists(f):
            st = os.stat(f)
            parts.append(f"{p}:{int(st.st_mtime)}:{st.st_size}")
        else:
            parts.append(f"{p}:missing")
    parts += [f"adj_mode={args.adj_mode}", f"px={args.price_col}",
              f"min_price={args.min_price}", f"min_amount={args.min_amount}",
              f"min_listed={args.min_listed}"]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def attach_industry(df: "pd.DataFrame", path: str) -> "pd.DataFrame":
    """把行业一级分类并入面板（列名 `ind_l1`）。

    ⚠️ `backtest_dividend.prepare()` **不含**行业列，而 `plan_selections` / `industry_exposure`
    需要 `ind_l1`（缺列会 `KeyError`）。做法与 `test_industry_neutral.py:414-418` 完全一致：
    读 `code_full` → 改名为 `code` → left merge → 缺失填 `"未分类"`。
    """
    if not os.path.exists(path):
        raise SystemExit(f"❌ 缺少行业分类文件 {path}（plan_selections 需要 ind_l1）")
    ind = pd.read_parquet(path)[["code_full", "ind_l1", "em2016"]] \
        .rename(columns={"code_full": "code"})
    df = df.merge(ind[["code", "ind_l1"]], on="code", how="left")
    df["ind_l1"] = df.ind_l1.fillna("未分类")
    return df


def build_panel(args, *, use_cache: bool = True, verbose: bool = True):
    """返回 `(df, dates, by_date, cache_hit)`。`df.date` 统一为 `YYYYMMDD` 字符串。"""
    from tools.backtest_dividend import prepare as prepare_div
    from tools.test_dividend_factor import require_adj_mode

    ind_p = getattr(args, "industry", DEFAULT_INDUSTRY)
    cache_dir = os.path.join(ROOT, "data", "cache")
    os.makedirs(cache_dir, exist_ok=True)
    fp = fingerprint(args)
    cache = os.path.join(cache_dir, f"panel_{fp}.parquet")

    hit = False
    if use_cache and os.path.exists(cache):
        df = pd.read_parquet(cache)
        df["date"] = df.date.astype(str)
        if "ind_l1" not in df.columns:        # 旧缓存（并入行业列之前生成的）→ 补上
            df = attach_industry(df, ind_p)
        hit = True
        if verbose:
            print(f"  [缓存命中] {os.path.relpath(cache, ROOT)}  {len(df):,} 行", flush=True)
    else:
        if verbose:
            print(f"  [缓存未命中] 指纹 {fp} → 构建面板（约 1~2 分钟）…", flush=True)
        t0 = time.time()
        ns = SimpleNamespace(bars=args.bars, bfq=args.bfq, dividends=args.dividends,
                             universe=args.universe, min_price=args.min_price,
                             min_amount=args.min_amount, min_listed=args.min_listed,
                             adj_mode=require_adj_mode(args))
        df = prepare_div(ns)
        df["date"] = df.date.astype(str)
        df = attach_industry(df, ind_p)
        df.to_parquet(cache, index=False)
        if verbose:
            print(f"  [缓存已写] {os.path.relpath(cache, ROOT)}  {len(df):,} 行"
                  f"  用时 {time.time()-t0:.0f}s", flush=True)

    dates = sorted(df.date.unique())
    by_date = {t: v for t, v in df.groupby("date", sort=False).indices.items()}
    return df, dates, by_date, hit
