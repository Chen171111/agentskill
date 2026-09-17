"""个股线（股息率策略）**纸面跟踪** —— 只生成名单并记录，**不下单**。

为什么必须先纸面跟踪（`docs/个股线_股息率实盘方案.md` 第九节）
--------------------------------------------------------------
定稿形态的样本外表现（10 万真实税后 **7.56%**；2026-09-17 已按修正后的送转口径重算）
**全部来自同一份历史数据**，
而「过滤器（`≤10%` + `≥2次`）是看过全区间结果后才挑的」这条偏差**无法在回测里消除**。
唯一能补的是**真实前向数据**。

本项目三次大坑 —— `hyst` 静默退化、引擎 `topk*2` 静默跳过、ETF 份额折算未复权 ——
**全都是"回测与实现不一致"**，只有真实前向跟踪能发现。

定稿形态（`docs/个股线_股息率实盘方案.md`）
--------------------------------------------
```
全 A 池（剔 ST）
  → 合格池：上市≥120日 且 20日均成交额≥3000万 且 收盘价≥2元 且 非停牌
  → 过滤：0.5% ≤ dy_ttm ≤ 10%  且  近 3 个自然年现金分红 ≥ 2 次
  → 排序：在【所属一级行业内】按 dy_ttm 算百分位
  → 选股：取百分位最高的 20 只，等权（每只 5%）
  → 调仓：持有 60 个交易日（季度），每年 4 次；持有期内不再平衡
```

用法
----
    $PY tools/paper_track_dividend.py                # 到期则生成名单；否则报告下次调仓日
    $PY tools/paper_track_dividend.py --force        # 强制生成（首次建仓用）
    $PY tools/paper_track_dividend.py --report       # 看已记录名单的前向表现

产出（`state/paper_tracking/`）
    anchor.json                     —— 跟踪起点（决定调仓日历）
    selection_YYYYMMDD.csv          —— 每次调仓的名单（代码/权重/股息率/行业）
    summary.csv                     —— 全部记录的汇总（含前向收益）

⚠️ 数据新鲜度：本脚本依赖 `data/stockbars/bars_total_tax10.parquet` 等**研究数据文件**，
   它们**不会自动更新**。脚本会检查最新日期并提示重跑哪些抓取脚本。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                                    # noqa: E402
from tools.backtest_dividend import prepare as prepare_div        # noqa: E402
from tools.test_industry_neutral import plan_selections           # noqa: E402

TRACK_DIR = Path(config.PROJECT_ROOT) / "state" / "paper_tracking"
ANCHOR = TRACK_DIR / "anchor.json"
SUMMARY = TRACK_DIR / "summary.csv"

# 定稿参数（改这里 = 改策略，务必同步 docs/个股线_股息率实盘方案.md）
TOPN = 20
HOLD = 60
MIN_DY = 0.5
MAX_DY = 10.0
MIN_DIV3 = 2
MIN_PRICE = 2.0
MIN_AMOUNT = 3e7
MIN_LISTED = 120
CAPITAL_WAN = 10.0          # 10 万


def _load_anchor() -> dict:
    try:
        return json.loads(ANCHOR.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_anchor(d: dict) -> None:
    TRACK_DIR.mkdir(parents=True, exist_ok=True)
    ANCHOR.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _build(args):
    ns = SimpleNamespace(bars=args.bars, bfq=args.bfq, dividends=args.dividends,
                         universe=args.universe, min_listed=MIN_LISTED,
                         min_amount=MIN_AMOUNT, min_price=MIN_PRICE,
                         adj_mode=args.adj_mode)
    df = prepare_div(ns)
    ind = pd.read_parquet(args.industry)[["code_full", "ind_l1"]]
    ind = ind.rename(columns={"code_full": "code"})
    df = df.merge(ind, on="code", how="left")
    df["ind_l1"] = df.ind_l1.fillna("未分类")
    return df


def _check_freshness(df) -> None:
    last = str(df.date.max())
    print("  研究数据最新日期：{}".format(last))
    try:
        from dataprovider.calendar import latest_closed_trading_day
        want = latest_closed_trading_day(None)
        if last < want:
            print("  ⚠️ 数据陈旧（应有 {}）→ 先重跑（**增量追加**，不要全量重抓）：".format(want))
            print("     $PY tools/append_stock_bars.py --out data/stockbars --workers 8")
            print("     $PY tools/fetch_stock_bfq.py merge --out data/stockbars")
            print("     $PY tools/fetch_dividend.py --out data/dividends")
            print("     $PY tools/rebuild_returns.py --bfq data/stockbars/bars_bfq.parquet "
                  "--dividends data/dividends/bonus_all.parquet --tax-rate 0.10 "
                  "--out data/stockbars/bars_total_tax10.parquet")
    except Exception as e:
        print("  （交易日历不可用，跳过新鲜度检查：{}）".format(e))


def _select_on(df, date: str, args):
    """在指定日期按定稿形态选股。返回 [(code, dy%, industry)]。

    ⚠️ 实盘场景就是「**用今天收盘的数据选股、明天开盘下单**」，
    所以允许在**数据最后一天**选股。`plan_selections` 的循环是
    `range(0, len(dates)-1, hold)`，需要 ≥2 个日期才会跑一次；
    这里补一个**不存在的哨兵日期**（`by_date` 里查不到会被跳过），
    从而只对 `dates[0]` 选股 —— 不引入任何未来数据。
    """
    dates = sorted(df.date.unique())
    if date not in dates:
        raise SystemExit("❌ {} 不在数据里（最新 {}）".format(date, dates[-1]))
    i = dates.index(date)
    by_date = {t: v for t, v in df.groupby("date", sort=False).indices.items()}
    plan = plan_selections(df, [dates[i], "99999999"], by_date, mode="indpct",
                           topn=TOPN, hold=HOLD, dy_col="dy_ttm", min_dy=MIN_DY,
                           max_dy=MAX_DY, min_div3=MIN_DIV3)
    codes = plan.get(dates[i], [])
    sub = df[(df.date == dates[i]) & (df.code.isin(codes))][
        ["code", "dy_ttm", "ind_l1"]].drop_duplicates("code")
    return list(sub.itertuples(index=False, name=None))


def _record(date: str, picks, args) -> Path:
    TRACK_DIR.mkdir(parents=True, exist_ok=True)
    w = 1.0 / max(len(picks), 1)
    rows = [{"date": date, "code": c, "weight": round(w, 6),
             "dy_ttm": round(float(dy), 4), "ind_l1": ind,
             "capital_wan": CAPITAL_WAN, "ticket": round(CAPITAL_WAN * 1e4 * w, 2)}
            for c, dy, ind in picks]
    out = TRACK_DIR / "selection_{}.csv".format(date)
    pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8-sig")

    # 汇总表追加（同日覆盖，避免重复运行产生重复行）
    new = pd.DataFrame(rows)
    if SUMMARY.exists():
        old = pd.read_csv(SUMMARY, dtype={"date": str, "code": str})
        old = old[old.date != date]
        new = pd.concat([old, new], ignore_index=True)
    new.to_csv(SUMMARY, index=False, encoding="utf-8-sig")
    return out


def _report(args) -> int:
    if not SUMMARY.exists():
        print("还没有任何记录（state/paper_tracking/summary.csv 不存在）")
        return 0
    s = pd.read_csv(SUMMARY, dtype={"date": str, "code": str})
    dates = sorted(s.date.unique())
    print("=" * 96)
    print("  个股线纸面跟踪 · 报告")
    print("=" * 96)
    print("  调仓次数 {} ｜ 起止 {} ~ {}".format(len(dates), dates[0], dates[-1]))
    print("  参数：N={} hold={} ≤{}% ≥{}次 ｜ 资金 {} 万".format(
        TOPN, HOLD, MAX_DY, MIN_DIV3, CAPITAL_WAN))

    df = _build(args)
    px = df.pivot_table(index="date", columns="code", values="close", aggfunc="last")
    rows = []
    for i, d in enumerate(dates):
        nxt = dates[i + 1] if i + 1 < len(dates) else None
        codes = s.loc[s.date == d, "code"].tolist()
        have = [c for c in codes if c in px.columns]
        if not have:
            continue
        p0 = px.loc[px.index >= d, have].iloc[0]
        end = nxt if nxt else str(px.index[-1])
        p1 = px.loc[px.index <= end, have].iloc[-1]
        r = (p1 / p0 - 1).dropna()
        rows.append({"date": d, "至": end, "持仓数": len(have),
                     "期收益%": float(r.mean()) * 100 if len(r) else np.nan,
                     "最优%": float(r.max()) * 100 if len(r) else np.nan,
                     "最差%": float(r.min()) * 100 if len(r) else np.nan,
                     "已完成": bool(nxt)})
    if not rows:
        print("\n  暂无可计算的区间")
        return 0
    r = pd.DataFrame(rows)
    print("\n  " + "{:<12}{:<12}{:>8}{:>12}{:>11}{:>11}{:>8}".format(
        "调仓日", "至", "持仓数", "期收益%", "最优%", "最差%", "已完期"))
    print("  " + "-" * 74)
    for _, x in r.iterrows():
        print("  " + "{:<12}{:<12}{:>8}{:>12.2f}{:>11.2f}{:>11.2f}{:>8}".format(
            x["date"], x["至"], x["持仓数"], x["期收益%"], x["最优%"], x["最差%"],
            "是" if x["已完成"] else "否"))
    done = r[r["已完成"]]
    if len(done):
        cum = float(np.prod(1 + done["期收益%"] / 100) - 1) * 100
        print("\n  已完成 {} 期，累计 {:.2f}%".format(len(done), cum))
    else:
        print("\n  ⚠️ 还没有一个完整持有期（首次调仓后需等 {} 个交易日）".format(HOLD))
    print("\n  ⚠️ 对比口径：回测样本外 10 万真实年化 **7.56%**"
          "（`docs/个股线_股息率实盘方案.md`）。")
    print("     判据（第九节）：前向年化与回测样本外差距 **≤3pp** 才算通过。")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="个股线（股息率）纸面跟踪")
    ap.add_argument("--bars", default="data/stockbars/bars_total_tax10.parquet")
    ap.add_argument("--bfq", default="data/stockbars/bars_bfq.parquet")
    ap.add_argument("--dividends", default="data/dividends/bonus_all.parquet")
    ap.add_argument("--industry", default="data/industry/industry_all.parquet")
    ap.add_argument("--universe", default="data/stockbars/universe_all.csv")
    ap.add_argument("--adj-mode", default="correct", choices=["legacy", "correct"],
                    help="送转调整口径（透传给 build_yield_panel）："
                         "correct=价值中性口径（默认）；legacy=已证伪的旧实现")

    ap.add_argument("--date", default=None, help="指定调仓日（默认数据最新日）")
    ap.add_argument("--force", action="store_true", help="忽略日历，强制生成名单")
    ap.add_argument("--report", action="store_true", help="只出报告")
    args = ap.parse_args(argv)

    print("=" * 96)
    print("  个股线（股息率策略）纸面跟踪 —— 只记录，不下单")
    print("=" * 96)
    print("  定稿形态：行业内 dy 百分位 / N={} / hold={}（季度）/ ≤{}% + ≥{}次"
          .format(TOPN, HOLD, MAX_DY, MIN_DIV3))

    if args.report:
        return _report(args)

    df = _build(args)
    _check_freshness(df)
    dates = sorted(df.date.unique())
    today = args.date or dates[-1]

    anchor = _load_anchor()
    if not anchor:
        anchor = {"start": today, "hold": HOLD,
                  "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        _save_anchor(anchor)
        print("\n  📌 首次运行 → 建立跟踪起点 anchor = {}".format(today))

    start = anchor.get("start", today)
    if start not in dates:
        start = min([d for d in dates if d >= start] or [dates[-1]])
    i0, i1 = dates.index(start), dates.index(today)
    n = i1 - i0

    # 调仓日 = 距起点 HOLD 的整数倍。⚠️ 必须允许「越过」：
    # 若数据一次跳过多天（按月/按周更新），n 会落在两个调仓日之间，
    # 而旧逻辑要求 `n % HOLD == 0` 才算到期 → **会永远不生成名单**，静默失效。
    # 现在取「距起点最大的、不超过 n 的 HOLD 整数倍」，且该日名单尚未生成。
    # 副作用（正面的）：名单一旦生成过，同一调仓日就不再 due → 不会被重复覆盖。
    target_n = (n // HOLD) * HOLD if n >= 0 else -1
    target_date = dates[i0 + target_n] if target_n >= 0 else None
    target_file = (TRACK_DIR / "selection_{}.csv".format(target_date)
                   if target_date else None)

    if args.force:
        due, sel_date = True, today
    elif target_date and target_file and not target_file.exists():
        due, sel_date = True, target_date
    else:
        due, sel_date = False, None

    next_in = HOLD - (n % HOLD) if n % HOLD else HOLD

    print("\n  跟踪起点 {} ｜ 数据最新 {} ｜ 距起点 {} 个交易日".format(start, today, n))
    if not due:
        print("  ⏳ 未到新的调仓日（距下一个还差 {} 个交易日）→ 不生成名单".format(next_in))
        print("     想看已记录的表现：--report")
        return 0

    if sel_date != today:
        print("  ℹ️ 数据已越过调仓日 {} → 补生成该日名单（用当日数据，不用最新数据）"
              .format(sel_date))

    picks = _select_on(df, sel_date, args)
    if not picks:
        print("\n  ⚠️ 该日无合格标的（候选池为空）→ 按方案应**空仓**，不硬凑。")
        return 0
    out = _record(sel_date, picks, args)
    print("\n  ✅ 已记录 {} 只（每只 {:.1f}% ／ 单笔约 {:.0f} 元）".format(
        len(picks), 100.0 / len(picks), CAPITAL_WAN * 1e4 / len(picks)))
    print("  " + "{:<12}{:>9}{:>10}{:>14}".format("代码", "股息率%", "权重%", "所属行业"))
    print("  " + "-" * 46)
    for c, dy, ind in sorted(picks, key=lambda x: -x[1]):
        print("  " + "{:<12}{:>9.2f}{:>10.2f}{:>14}".format(
            c, float(dy), 100.0 / len(picks), ind))
    print("\n  名单已写出 {}".format(out))
    print("  ⚠️ **未下任何单**。按方案第九节：第 1 阶段只记录 ≥4 期（≈1 年）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
