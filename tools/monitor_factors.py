"""策略健康监控（M2）—— 让「策略正在失效」**当天可见**。

> 规格：`docs/需求规格_自优化闭环模块.md` §4。

为什么需要它
------------
本项目三次大坑（`hyst` 静默退化、引擎静默跳过、ETF 份额折算被刷新回退）的**共同点**是：
**问题发生了，但没有任何机制告诉你**。个股线尤其危险 ——
IC 从 0.055 掉到 0.02、行业暴露漂移、前向跑输基准，**当前全都不可见**。

本脚本**只读**：不写策略参数、不写 `state/trading.db`、不下单。
它唯一的副作用是产出监控文件与 `state/LEARN_ALERT.txt`。

⚠️ 告警文件必须与 `ALERT.txt`（下单失败）、`DATA_ALERT.txt`（数据问题）**分开** ——
理由同 `tools/README.md` 铁律 13：**策略问题不该被"下单成功"掩盖。**

指标与阈值
----------
阈值在 `config/monitor.json`（12 个键）；越界项写告警并按 `--strict` 返回退出码 3。

用法
----
    $PY tools/monitor_factors.py --adj-mode correct --strict ; echo "exit=$?"

退出码：0 正常；3 越界（仅 --strict）；2 参数/数据错误。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest_stock import metrics, run  # noqa: E402
from tools.panel_cache import build_panel  # noqa: E402
from tools.stats_lite import spearman  # noqa: E402
from tools.progress import flush_partial  # noqa: E402
from tools.test_dividend_factor import require_adj_mode  # noqa: E402
from tools.test_industry_neutral import (industry_exposure,  # noqa: E402
                                         mask_from_plan, plan_selections,
                                         real_ann)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config", "monitor.json")
ALERT = os.path.join(ROOT, "state", "LEARN_ALERT.txt")
TRADING_DAYS = 244.0

# 与 config/monitor.json 必须逐一对齐（selftest 会断言）
REQUIRED_KEYS = ["ic_all_min", "ic_12m_min", "icir_min", "ic_pos_years_min",
                 "decile_tb_min", "dd_max", "excess_vs_csi1000_min", "hhi_max",
                 "industry_count_min", "turnover_max_mult", "fwd_vs_bt_max_dev",
                 "pool_size_min"]

# 定稿形态的基准值（用于展示"当前 vs 基准"，不用于判 PASS/FAIL）
BASELINE = {"ic_all": 0.0553, "hhi": 0.050, "industry_count": 20.0,
            "turnover": 2.64, "dy_mean": 5.0, "oos_ann": 7.45, "dd": -0.2613}


def load_thresholds() -> dict:
    if not os.path.exists(CONFIG):
        raise SystemExit(f"❌ 缺少阈值配置 {CONFIG}")
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    miss = [k for k in REQUIRED_KEYS if k not in cfg]
    if miss:
        raise SystemExit(f"❌ {os.path.relpath(CONFIG, ROOT)} 缺键：{miss}")
    return cfg


# ---------------------------------------------------------------- 信号类
def signal_metrics(df, dates, by_date, args) -> dict:
    """IC 系列 / 分年度 / 十分位。按 `--ic-step` 抽样交易日控制耗时。

    ⚠️ 2026-09-18 修两处口径（原本与 `test_dividend_factor.ic_table` 不一致）：
    ① **窗口**：原来用面板全区间（`bars_total_tax10` 的面板跨 **2018-01-02~20260915**），
       而 `--start/--end` 是 20190101~20260911 → `ic_all` 把 2018 也算进去了。
    ② **池过滤**：原来**没有**任何池过滤 —— 实测 `dy_ttm` 在面板 9,032,766 行里
       **全部非空**（其中 2,113,219 行不在池内）→ 这 2.1M 行 ST/低流动性/次新股
       全都进了 IC 样本。
    修法与 `test_dividend_factor.ic_table` 对齐：**窗口 + `df._in_uni`**
    （`_in_uni` 的定义与那里的 `pool` **逐字相同**，见 `backtest_dividend.prepare`）。
    """
    dy = pd.to_numeric(df[args.dy_col], errors="coerce")
    close = df.close.astype(float)
    fwd = close.groupby(df.code.values, sort=False).shift(-args.label_horizon) / close - 1.0
    step = max(1, int(args.ic_step))
    sd = [t for t in dates if args.start <= t <= args.end][::step]
    ic_rows, decile_acc = [], {}
    for t in sd:
        rows = by_date.get(t)
        if rows is None:
            continue
        ok = df._in_uni.values[rows]
        a = dy.values[rows][ok]
        b = fwd.values[rows][ok]
        m = np.isfinite(a) & np.isfinite(b)
        if m.sum() < 100:
            continue
        a2, b2 = a[m], b[m]
        if np.unique(a2).size < 5:
            continue
        ic = spearman(a2, b2, min_n=100)     # 不用 pandas.corr("spearman")：会转调 scipy
        if not np.isfinite(ic):
            continue
        ic_rows.append({"date": t, "ic": ic})
        # ── 十分位（⚠️ 2026-09-18 修，原实现与 `test_dividend_factor` 三处不一致）──
        # ① **必须在 `dy > 0` 的股票里分档** —— 池内 **28.7%** 的行 `dy_ttm == 0`
        #    （实测 1,984,256 / 6,919,547），不剔除的话 **D1 全是"不分红"股**，
        #    底档被稀释成"市场平均" → 顶底差符号会翻（原实现 +1.33 vs 因子检验 −0.54）。
        # ② **池化（pooled）而不是逐日平均** —— 因子检验是 `groupby('_dec')._fwd.mean()`，
        #    按行加权；逐日平均等于按"日期"等权，两者在横截面大小不均时不等价。
        # ③ **年化用复利** —— 因子检验是 `((1+mean)**(244/60) - 1)*100`，原实现是线性 `*(244/60)`。
        pos = a2 > 0
        if int(pos.sum()) < 100:
            continue
        ap, bp = a2[pos], b2[pos]
        q = pd.qcut(pd.Series(ap).rank(method="first"), 10, labels=False)
        g = pd.Series(bp).groupby(q.values).agg(["sum", "count"])
        for k, r in g.iterrows():
            acc = decile_acc.setdefault(int(k) + 1, [0.0, 0])
            acc[0] += float(r["sum"])
            acc[1] += int(r["count"])
    icd = pd.DataFrame(ic_rows)
    out: dict = {}
    if icd.empty:
        return {k: float("nan") for k in
                ("ic_all", "ic_in", "ic_oos", "icir", "ic_12m", "ic_pos_years",
                 "ic_years_total", "decile_tb_ann", "decile_monotonic_frac")} | \
               {"ic_samples": 0}
    icd["year"] = icd.date.str[:4].astype(int)
    out["ic_all"] = float(icd.ic.mean())
    out["icir"] = float(icd.ic.mean() / icd.ic.std()) if icd.ic.std() else float("nan")
    out["ic_in"] = float(icd.loc[icd.date <= "20221231", "ic"].mean())
    out["ic_oos"] = float(icd.loc[icd.date >= "20230101", "ic"].mean())
    last = icd.loc[icd.date >= dates[max(0, len(dates) - 250)], "ic"]
    out["ic_12m"] = float(last.mean()) if len(last) else float("nan")
    ym = icd.groupby("year").ic.mean().tail(8)
    out["ic_pos_years"] = int((ym > 0).sum())
    out["ic_years_total"] = int(len(ym))
    out["ic_samples"] = int(len(icd))
    # 十分位（年化）—— 与 `test_dividend_factor.py` §四 逐位对齐：池化均值 + 复利年化
    dm = {k: v[0] / v[1] for k, v in decile_acc.items() if v[1]}
    if dm:
        ann_ = lambda x: ((1 + x) ** (TRADING_DAYS / args.label_horizon) - 1) * 100
        lo, hi = ann_(dm.get(1, np.nan)), ann_(dm.get(10, np.nan))
        out["decile_tb_ann"] = float(hi - lo)
        out["decile_d1_ann"] = float(lo)
        out["decile_d10_ann"] = float(hi)
        ks = sorted(dm)
        inc = sum(1 for i in range(len(ks) - 1) if dm[ks[i + 1]] > dm[ks[i]])
        out["decile_monotonic_frac"] = round(inc / max(len(ks) - 1, 1), 3)
    return out


# ---------------------------------------------------------------- 组合类
def portfolio_metrics(df, dates, by_date, args) -> dict:
    """跑一次定稿形态回测（**窗口 = `--start`~`--end`**）→ 持仓/换手/回撤/年化 + 行业与因子暴露。

    ⚠️ 2026-09-18 修：原来 `plan_selections(df, dates, ...)` 传的是**面板全区间**的 `dates`
    （`bars_total_tax10` 的面板跨 **2018-01-02~20260915**），`run(start=dates[0], end=dates[-1])`
    也照抄 → 组合实际跑的是 **2018-01~2026-09**，而 `index_bench` 用 `--start/--end`
    （2019-01~2026-09）→ **`excess_vs_csi1000` 变成两个不同窗口相减**。
    这正是 HANDOFF §5.1 第 1 条「M2 组合层 9.26% vs 定稿 13.02%」的**真正原因**
    （既不是"数据文件今日已更新"，也不是 `industry_exposure` 口径）。
    修完 M2 的 `ann_all` 应与 `results/adj_correct_tax10_backtest.csv` 的
    `行业内百分位 N=20` **全区间 13.02%** 对齐（同一窗口 + 同一 `plan_selections`）。
    """
    dts = [t for t in dates if args.start <= t <= args.end]
    plan = plan_selections(df, dts, by_date, mode=args.mode, topn=args.topn,
                           hold=args.hold, dy_col=args.dy_col, min_dy=args.min_dy,
                           max_dy=args.max_dy, min_div3=args.min_div3)
    df["_sig"] = mask_from_plan(df, plan, by_date)
    s, e = args.start, args.end
    eq, tr, meta = run(df, [], start=s, end=e, hold=args.hold, cond_col="_sig",
                       min_price=args.min_price, min_amount=args.min_amount,
                       min_listed=args.min_listed)
    m = metrics(eq.equity)
    rows = by_date.get(dates[-1])
    nh = meta.get("avg_hold", 0) or 1
    turn = (int((tr.side == "buy").sum()) / (len(eq) / TRADING_DAYS) / nh) if len(tr) else 0.0
    exp = industry_exposure(df, plan, topn=args.topn)
    if len(exp) and "组合权重" in exp.columns:
        w = exp.组合权重.values
        hhi, icnt = float((w ** 2).sum()), int((w > 0).sum())
    else:
        hhi, icnt = float("nan"), 0
    out = {"ann_all": m["年化收益"], "dd": m["最大回撤"] / 100.0,
           "sharpe": m["夏普比率"], "avg_hold": nh, "turnover": turn,
           "ann_real10": real_ann(m["年化收益"], turn, nh, 10.0),
           "hhi": hhi, "industry_count": icnt}
    # 因子暴露（横截面百分位均值，跨期可比）
    last_t = [t for t in plan][-1] if plan else None
    if last_t is not None:
        rws = by_date.get(last_t)
        sel = plan[last_t]
        if rws is not None and sel:
            mm = np.isin(df.code.values[rws], sel)
            for f in ("vol20", "illiq20"):
                if f in df.columns:
                    pct = pd.Series(df[f].values, index=df.index) \
                        .groupby(df.date.values).rank(pct=True)
                    out[f"{f}_exposure"] = float(np.nanmean(pct.values[rws][mm]))
            out["dy_mean"] = float(np.nanmean(df[args.dy_col].values[rws][mm]))
    return out


def pool_metrics(df, dates, by_date, args) -> dict:
    """合格池大小与 dy≥8% 的股票数（中位）。

    ⚠️ 2026-09-18 修：同样加窗口限制（原来扫面板全区间，含 2018）。"""
    win = [t for t in dates if args.start <= t <= args.end]
    sizes, hi = [], []
    for t in win[::max(1, len(win) // 60)]:
        rows = by_date.get(t)
        if rows is None:
            continue
        ok = df._in_uni.values[rows]
        n = int(ok.sum())
        if n == 0:
            continue
        sizes.append(n)
        dy = pd.to_numeric(df[args.dy_col].values[rows][ok], errors="coerce")
        hi.append(int((dy >= 0.08).sum()))
    return {"pool_size": float(np.median(sizes)) if sizes else float("nan"),
            "pool_dy8_count": float(np.median(hi)) if hi else float("nan")}


def index_bench(args) -> float:
    """中证1000 同期年化（%）。"""
    p = os.path.join(ROOT, args.indexes)
    if not os.path.exists(p):
        return float("nan")
    ix = pd.read_parquet(p)
    ix["date"] = ix.date.astype(str).str.replace("-", "", regex=False)
    px = ix[ix.code == "SH000852"].set_index("date").close.sort_index()
    px = px[(px.index >= args.start) & (px.index <= args.end)]
    return float(metrics(px)["年化收益"]) if len(px) > 2 else float("nan")


def forward_dev(args) -> tuple[float | None, str]:
    """纸面跟踪的前向偏离（读 state/paper_tracking/，为空则返回 None）。"""
    d = os.path.join(ROOT, "state", "paper_tracking")
    if not os.path.isdir(d):
        return None, "无纸面跟踪目录"
    files = sorted(f for f in os.listdir(d) if f.startswith("selection_"))
    if not files:
        return None, "无名单文件"
    try:
        sel = pd.read_csv(os.path.join(d, files[-1]))
        anchor = files[-1].replace("selection_", "").replace(".csv", "")
    except Exception as e:                                   # noqa: BLE001
        return None, f"名单读取失败：{e}"
    # ⚠️ 必须按**列名**取代码，不能取"第一列" —— 名单第一列是 `date`，
    #    取错会得到一堆日期当代码，然后表现为"名单在市场数据里无匹配"，
    #    看起来像"数据源没覆盖"。这正是 tools/README.md 铁律 7 那个坑（主键格式不对齐）。
    if "code" not in sel.columns:
        return None, f"名单 {anchor} 缺少 code 列（实得 {list(sel.columns)}）"
    if "date" in sel.columns:
        sel["date"] = sel.date.astype(str).str.replace("-", "", regex=False)
        anchor = str(sel.date.max())
    p = os.path.join(ROOT, args.bars)
    b = pd.read_parquet(p, columns=["code", "date", "close"])
    b["date"] = b.date.astype(str).str.replace("-", "", regex=False)
    codes = set(sel.code.astype(str))
    b = b[b.code.isin(codes)]
    if b.empty:
        return None, f"名单 {anchor} 在市场数据里无匹配"
    wide = b.pivot_table(index="date", columns="code", values="close")
    wide = wide[wide.index >= anchor].ffill()
    if len(wide) < 2:
        return None, f"锚点 {anchor} 之后无行情（跟踪尚未开始）"
    ret = (wide.iloc[-1] / wide.iloc[0] - 1).mean()
    years = max(len(wide) / TRADING_DAYS, 1e-6)
    fwd_ann = ((1 + ret) ** (1 / years) - 1) * 100
    dev = (fwd_ann - BASELINE["oos_ann"]) / 100.0
    return float(dev), f"锚点 {anchor}，前向年化 {fwd_ann:.2f}% vs 回测 7.45%"


# ---------------------------------------------------------------- 主流程
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="策略健康监控")
    ap.add_argument("--bars", default="data/stockbars/bars_total_tax10.parquet")
    ap.add_argument("--bfq", default="data/stockbars/bars_bfq.parquet")
    ap.add_argument("--dividends", default="data/dividends/bonus_all.parquet")
    ap.add_argument("--universe", default="data/stockbars/universe_all.csv")
    ap.add_argument("--indexes", default="data/indexes/indexes_all.parquet")
    ap.add_argument("--price-col", default="px_real")
    ap.add_argument("--adj-mode", default=None, choices=["legacy", "correct"])
    ap.add_argument("--dy-col", default="dy_ttm")
    ap.add_argument("--mode", default="indpct", choices=["topn", "indpct", "indquota"])
    ap.add_argument("--topn", type=int, default=20)
    ap.add_argument("--hold", type=int, default=60)
    ap.add_argument("--min-dy", type=float, default=0.5)
    ap.add_argument("--max-dy", type=float, default=10.0)
    ap.add_argument("--min-div3", type=int, default=2)
    ap.add_argument("--min-price", type=float, default=2.0)
    ap.add_argument("--min-amount", type=float, default=3e7)
    ap.add_argument("--min-listed", type=int, default=120)
    ap.add_argument("--label-horizon", type=int, default=60)
    ap.add_argument("--ic-step", type=int, default=5, help="IC 抽样步长（交易日）")
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260911")
    ap.add_argument("--out", default=None)
    ap.add_argument("--strict", action="store_true", help="越界返回退出码 3")
    args = ap.parse_args(argv)

    date_tag = time.strftime("%Y%m%d")
    args.out = args.out or f"results/monitor_{date_tag}.csv"
    t0 = time.time()
    print("=" * 104)
    print(f"  策略健康监控  ·  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 104)
    cfg = load_thresholds()
    df, dates, by_date, _hit = build_panel(args)
    print(f"  面板 {len(df):,} 行 / {df.code.nunique():,} 只｜adj_mode="
          f"{require_adj_mode(args)}｜抽样步长 {args.ic_step}", flush=True)

    print("\n  A. 信号层…", flush=True)
    sig = signal_metrics(df, dates, by_date, args)
    print("\n  B. 组合层（跑一次定稿形态全区间）…", flush=True)
    port = portfolio_metrics(df, dates, by_date, args)
    print("  C. 候选池…", flush=True)
    pool = pool_metrics(df, dates, by_date, args)
    bench = index_bench(args)
    fdev, fnote = forward_dev(args)

    m = {**sig, **port, **pool,
         "index_csi1000_ann": bench, "excess_vs_csi1000": port["ann_all"] / 100 - bench / 100,
         "fwd_vs_bt_dev": fdev, "fwd_note": fnote,
         "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "adj_mode": args.adj_mode}

    # ---------- 越界判定 ----------
    breaches = []

    def chk(key, ok, cur, thr, note=""):
        if not ok:
            breaches.append({"指标": key, "当前值": cur, "阈值": thr, "说明": note})

    chk("ic_all", m["ic_all"] >= cfg["ic_all_min"], m["ic_all"], cfg["ic_all_min"])
    chk("ic_12m", m["ic_12m"] >= cfg["ic_12m_min"], m["ic_12m"], cfg["ic_12m_min"])
    chk("icir", m["icir"] >= cfg["icir_min"], m["icir"], cfg["icir_min"])
    chk("ic_pos_years", m["ic_pos_years"] >= cfg["ic_pos_years_min"],
        m["ic_pos_years"], cfg["ic_pos_years_min"],
        f"{m['ic_pos_years']}/{m['ic_years_total']}")
    chk("decile_tb_ann", m["decile_tb_ann"] >= cfg["decile_tb_min"],
        m["decile_tb_ann"], cfg["decile_tb_min"])
    chk("dd", m["dd"] >= cfg["dd_max"], m["dd"], cfg["dd_max"])
    chk("excess_vs_csi1000", m["excess_vs_csi1000"] >= cfg["excess_vs_csi1000_min"],
        m["excess_vs_csi1000"], cfg["excess_vs_csi1000_min"])
    chk("hhi", m["hhi"] <= cfg["hhi_max"], m["hhi"], cfg["hhi_max"])
    chk("industry_count", m["industry_count"] >= cfg["industry_count_min"],
        m["industry_count"], cfg["industry_count_min"])
    chk("turnover", m["turnover"] <= BASELINE["turnover"] * cfg["turnover_max_mult"],
        m["turnover"], BASELINE["turnover"] * cfg["turnover_max_mult"])
    chk("pool_size", m["pool_size"] >= cfg["pool_size_min"],
        m["pool_size"], cfg["pool_size_min"])
    if fdev is not None:
        chk("fwd_vs_bt_dev", abs(fdev) <= cfg["fwd_vs_bt_max_dev"], fdev,
            cfg["fwd_vs_bt_max_dev"])

    # ---------- 打印 ----------
    print("\n  ── 信号 ──")
    print(f"    IC(全) {m['ic_all']:.4f}（基准 {BASELINE['ic_all']}）｜ICIR {m['icir']:.3f}"
          f"｜IC(内) {m['ic_in']:.4f}｜IC(外) {m['ic_oos']:.4f}｜IC(近12月) {m['ic_12m']:.4f}")
    print(f"    分年度为正 {m['ic_pos_years']}/{m['ic_years_total']}（基准 5/8）"
          f"｜十分位顶底 {m['decile_tb_ann']:+.2f}pp/年"
          f"（D1 {m.get('decile_d1_ann', float('nan')):+.2f} → D10 {m.get('decile_d10_ann', float('nan')):+.2f}，"
          f"阈值 {cfg['decile_tb_min']:+.1f}）"
          f"｜单调相邻上升占比 {m.get('decile_monotonic_frac', float('nan')):.3f}")
    print("  ── 组合 ──")
    print(f"    年化 {m['ann_all']:.2f}%｜夏普 {m['sharpe']:.2f}｜回撤 {m['dd']*100:.2f}%"
          f"｜10万真实 {m['ann_real10']:.2f}%")
    print(f"    行业HHI {m['hhi']:.4f}（基准 {BASELINE['hhi']}）｜行业数 {m['industry_count']}"
          f"｜持仓 {m['avg_hold']:.1f}｜换手 {m['turnover']:.2f}x（基准 {BASELINE['turnover']}）")
    print(f"    vol20暴露 {m.get('vol20_exposure', float('nan')):.3f}"
          f"｜illiq20暴露 {m.get('illiq20_exposure', float('nan')):.3f}"
          f"｜平均dy {m.get('dy_mean', float('nan')):.2f}%")
    print("  ── 池 / 基准 / 前向 ──")
    print(f"    合格池 {m['pool_size']:.0f}｜池内 dy≥8% {m['pool_dy8_count']:.0f}"
          f"｜中证1000 {m['index_csi1000_ann']:.2f}%"
          f"｜超额 {m['excess_vs_csi1000']*100:+.2f}pp")
    print(f"    前向：{fnote}")

    # ---------- 告警 ----------
    if breaches:
        print("\n  [!] 越界项：")
        for b in breaches:
            print(f"      {b['指标']:<18} 当前 {b['当前值']}  阈值 {b['阈值']}  {b['说明']}")
        lines = [f"策略健康告警 · {time.strftime('%Y-%m-%d %H:%M:%S')}",
                 f"（{len(breaches)} 项越界）", ""]
        for b in breaches:
            lines.append(f"[{b['指标']}] 当前 {b['当前值']} / 阈值 {b['阈值']} {b['说明']}")
        lines += ["", "处置建议（按 docs/因子策略自优化方案_每日学习闭环.md §六）：",
                  "  · IC/超额类越界 → 降仓至 1/2，停止新增资金，不追加",
                  "  · 回撤类越界 → 按风控规则降仓",
                  "  · 暴露类越界 → 检查行业标签/候选池，勿调参",
                  "  · 本文件与 ALERT.txt / DATA_ALERT.txt 分开，勿混用"]
        with open(ALERT, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        print(f"      → 已写 {os.path.relpath(ALERT, ROOT)}")
    else:
        if os.path.exists(ALERT):
            os.remove(ALERT)
        print("\n  [正常] 全部指标在阈值内")

    # ---------- 落盘 ----------
    pd.DataFrame([{k: v for k, v in m.items() if not isinstance(v, (list, dict))}]) \
        .to_csv(args.out, index=False, encoding="utf-8-sig")
    with open(os.path.join(ROOT, "state", "monitor_latest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(m, fh, ensure_ascii=False, indent=2, default=str)
    flush_partial([{"指标": b["指标"], "当前值": b["当前值"], "阈值": b["阈值"]}
                   for b in breaches] or [{"指标": "无", "当前值": None, "阈值": None}],
                  f"results/monitor_{date_tag}_breaches.csv", tag="")
    print(f"\n  产物：{args.out} / state/monitor_latest.json"
          f"／用时 {(time.time()-t0)/60:.1f} min")
    if breaches and args.strict:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
