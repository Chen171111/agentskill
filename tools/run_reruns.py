"""重跑驱动 —— **断点续跑**，每个步骤单独日志、单独落盘。

为什么需要它
------------
本项目有两条硬约束，而现有脚本都满足不了：

1. **主人的机器有死机史** → 「长任务必须增量落盘 + 断点续跑」是项目铁律，
   但 `test_industry_neutral` / `sweep_hyst` 这类脚本**只在结尾写一次 CSV**，
   中途挂了就全丢。
2. **宿主会在 agent 回合结束时杀掉后台任务**（2026-09-17 实测：21:36 启的重跑
   只跑完第 1 步，第 2 步在 21:55 被掐）→ 长任务只能前台分块跑。

本驱动把「一长串命令」变成**一组可重入的步骤**：

- 每步独立子进程 + 独立日志 `results/_rerun_<id>.log`
- 产物已存在且**比输入数据新** → 自动跳过（`--force` 可强制重跑）
- 失败即停，打印**续跑命令**（直接再跑一次即可从断点继续）
- `--list` 看清单与耗时估计、`--only/--from` 挑子集

步骤来源
--------
`STEPS` 就是**重跑的唯一真源**（`docs/修复方案_20260917.md` §三 的表格与它对应）。
以后加一项重跑，只改这里。

用法
----
    $PY tools/run_reruns.py --list
    $PY tools/run_reruns.py                 # 跑所有未完成的
    $PY tools/run_reruns.py --from R4       # 从 R4 开始
    $PY tools/run_reruns.py --only R1 R2    # 只跑指定几步
    $PY tools/run_reruns.py --force         # 全部重跑（忽略产物存在）
    $PY tools/run_reruns.py --dry           # 只打印将要执行的命令
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BARS = "data/stockbars/bars_total.parquet"
BARS_TAX10 = "data/stockbars/bars_total_tax10.parquet"
BARS_TAX20 = "data/stockbars/bars_total_tax20.parquet"
BFQ = "data/stockbars/bars_bfq.parquet"
DIV = "data/dividends/bonus_all.parquet"
UNI = "data/stockbars/universe_all.csv"
IND = "data/industry/industry_all.parquet"

# 输入数据的「新鲜度基准」：产物比它们新才算有效
INPUTS = [BARS, BARS_TAX10, BARS_TAX20, BFQ, DIV, UNI, IND]

# id, 名称, argv, 产物, 供回灌的文档, 预计分钟
STEPS: list[tuple[str, str, list[str], list[str], str, float]] = [
    ("R1", "机制归因 hyst×行业内",
     ["tools/diag_industry_hyst_overlap.py", "--adj-mode", "correct",
      "--out", "results/adj_correct_ind_hyst_overlap.csv"],
     ["results/adj_correct_ind_hyst_overlap.csv"],
     "个股线_机制归因_hyst与行业中性化.md", 2),

    ("R2", "行业中性化（无税）",
     ["tools/test_industry_neutral.py", "--bars", BARS, "--topn", "20", "30",
      "--hold", "60", "--adj-mode", "correct",
      "--out-prefix", "results/adj_correct_notax"],
     ["results/adj_correct_notax_backtest.csv"],
     "个股线_行业中性化.md", 8),

    ("R3", "行业中性化（税后 10%）",
     ["tools/test_industry_neutral.py", "--bars", BARS_TAX10, "--topn", "20", "30",
      "--hold", "60", "--adj-mode", "correct",
      "--out-prefix", "results/adj_correct_tax10"],
     ["results/adj_correct_tax10_backtest.csv"],
     "个股线_红利税.md", 8),

    ("R4", "四进三出阈值网格（无税）",
     ["tools/sweep_hyst.py", "--topn", "20", "--hold", "60", "--max-dy", "10",
      "--min-div3", "2", "--adj-mode", "correct",
      "--out", "results/adj_correct_hyst_sweep.csv"],
     ["results/adj_correct_hyst_sweep.csv"],
     "个股线_四进三出阈值检验.md", 30),   # 实测 29.7 min（原估 15，18 配置×~40s/区间×3 区间）

    ("R5", "纯门槛对照（分离门槛/滞回）",
     ["tools/sweep_hyst.py", "--ctrl-only", "--entries", "6", "7", "8", "9",
      "--topn", "20", "--hold", "60", "--max-dy", "10", "--min-div3", "2",
      "--adj-mode", "correct",
      "--out", "results/adj_correct_hyst_ctrl.csv"],
     ["results/adj_correct_hyst_ctrl.csv"],
     "个股线_四进三出阈值检验.md", 7),    # 实测 6.8 min

    ("R6", "四进三出（税后 10%）",
     ["tools/sweep_hyst.py", "--bars", BARS_TAX10,
      "--entries", "8", "9", "--exits", "4", "5", "6", "7",
      "--topn", "20", "--hold", "60", "--max-dy", "10", "--min-div3", "2",
      "--adj-mode", "correct",
      "--out", "results/adj_correct_hyst_sweep_tax10.csv"],
     ["results/adj_correct_hyst_sweep_tax10.csv"],
     "个股线_四进三出阈值检验.md", 19),   # 实测 19.0 min

    ("R7", "股息率并入多因子（判定）",
     ["tools/sweep_dividend_into_mf.py", "--topk", "800", "--hold", "20",
      "--weight-mode", "rank", "--adj-mode", "correct",
      "--out", "results/adj_correct_into_mf.csv"],
     ["results/adj_correct_into_mf.csv"],
     "个股线_股息率并入多因子.md", 13),

    ("R8", "股息率并入多因子（深挖）",
     ["tools/diag_dividend_into_mf.py", "--adj-mode", "correct",
      "--out", "results/adj_correct_into_mf_diag.csv",
      "--out-window", "results/adj_correct_into_mf_windows.csv"],
     ["results/adj_correct_into_mf_diag.csv",
      "results/adj_correct_into_mf_windows.csv"],
     "个股线_股息率并入多因子.md", 40),

    ("R9", "股息率组合回测（税后 10%）",
     ["tools/backtest_dividend.py", "--bars", BARS_TAX10,
      "--topn", "10", "20", "30", "50", "--hold", "60", "--max-dy", "10",
      "--min-div3", "2", "--adj-mode", "correct",
      "--out", "results/adj_correct_tax10_dividend_backtest.csv"],
     ["results/adj_correct_tax10_dividend_backtest.csv"],
     "个股线_红利税.md", 5),

    # ── 2026-09-18 补：`个股线_股息率实盘方案.md` 的 §三（N 敏感性）/
    #    §四（hold 敏感性）/ §八（层层加码）在 R1~R9 里**没有对应步骤**，
    #    这三节此前一直是 legacy 数字 → 补 R10~R15 把缺口填上。
    ("R10", "N 敏感性（税后 10%）",
     ["tools/test_industry_neutral.py", "--bars", BARS_TAX10,
      "--topn", "10", "15", "20", "30", "40", "50", "60", "--hold", "60",
      "--hyst-entry", "8", "--hyst-exit", "4", "--adj-mode", "correct",
      "--out-prefix", "results/adj_correct_n_sens"],
     ["results/adj_correct_n_sens_backtest.csv"],
     "个股线_股息率实盘方案.md", 35),

    ("R11", "hold 邻域 20 日（20% 税档）",
     ["tools/test_industry_neutral.py", "--bars", BARS_TAX20,
      "--topn", "20", "--hold", "20",
      "--hyst-entry", "8", "--hyst-exit", "4", "--adj-mode", "correct",
      "--out-prefix", "results/adj_correct_hold20_tax20"],
     ["results/adj_correct_hold20_tax20_backtest.csv"],
     "个股线_股息率实盘方案.md", 8),

    ("R12", "hold 邻域 30 日（10% 税档）",
     ["tools/test_industry_neutral.py", "--bars", BARS_TAX10,
      "--topn", "20", "--hold", "30",
      "--hyst-entry", "8", "--hyst-exit", "4", "--adj-mode", "correct",
      "--out-prefix", "results/adj_correct_hold30_tax10"],
     ["results/adj_correct_hold30_tax10_backtest.csv"],
     "个股线_股息率实盘方案.md", 8),

    ("R13", "hold 邻域 40 日（10% 税档）",
     ["tools/test_industry_neutral.py", "--bars", BARS_TAX10,
      "--topn", "20", "--hold", "40",
      "--hyst-entry", "8", "--hyst-exit", "4", "--adj-mode", "correct",
      "--out-prefix", "results/adj_correct_hold40_tax10"],
     ["results/adj_correct_hold40_tax10_backtest.csv"],
     "个股线_股息率实盘方案.md", 8),

    ("R14", "层层加码·裸版（去掉两个过滤器）",
     ["tools/test_industry_neutral.py", "--bars", BARS,
      "--topn", "20", "--hold", "60", "--max-dy", "999", "--min-div3", "0",
      "--adj-mode", "correct",
      "--out-prefix", "results/adj_correct_ladder_naked"],
     ["results/adj_correct_ladder_naked_backtest.csv"],
     "个股线_股息率实盘方案.md", 8),

    ("R15", "层层加码·＋≤10% 上限",
     ["tools/test_industry_neutral.py", "--bars", BARS,
      "--topn", "20", "--hold", "60", "--max-dy", "10", "--min-div3", "0",
      "--adj-mode", "correct",
      "--out-prefix", "results/adj_correct_ladder_maxdy"],
     ["results/adj_correct_ladder_maxdy_backtest.csv"],
     "个股线_股息率实盘方案.md", 8),

    ("R16", "叠加测试（无税）：hyst 8/4 × 行业中性化",
     ["tools/test_industry_neutral.py", "--bars", BARS,
      "--topn", "20", "--hold", "60", "--max-dy", "10", "--min-div3", "2",
      "--hyst-entry", "8", "--hyst-exit", "4", "--adj-mode", "correct",
      "--out-prefix", "results/adj_correct_combo_notax"],
     ["results/adj_correct_combo_notax_backtest.csv"],
     "个股线_四进三出阈值检验.md", 8),

    # ── 2026-09-18 补：`个股线_股息率策略.md` §二（IC / 分年度 / 样本内外 / 十分位）
    #    与 §三（无税 top-N 阶梯）此前**没有对应步骤**，导致这两节长期是 legacy 数字，
    #    而且 §七 的复现命令指向一个**根本不存在的产物** `adj_correct_dividend_backtest.csv`。
    ("R17", "股息率因子有效性（IC / 分年度 / 十分位）",
     ["tools/test_dividend_factor.py", "--adj-mode", "correct",
      "--out", "results/adj_correct_ic.csv"],
     ["results/adj_correct_ic.csv"],
     "个股线_股息率策略.md", 10),

    ("R18", "股息率组合阶梯（无税）",
     ["tools/backtest_dividend.py", "--bars", BARS,
      "--topn", "10", "20", "30", "50", "--hold", "60",
      "--max-dy", "10", "--min-div3", "2", "--adj-mode", "correct",
      "--out", "results/adj_correct_dividend_backtest.csv"],
     ["results/adj_correct_dividend_backtest.csv"],
     "个股线_股息率策略.md", 6),

    # ── 2026-09-18 回灌 `个股线_股息率实盘方案.md` §八-2「层层加码」时的口径决定：
    #    前三层（裸 / ＋≤10% / ＋≥2次）走 `bars_total.parquet`（无税，末根 20260911）。
    #    「定稿」那一层**直接用 R2 的产物**（`adj_correct_notax_backtest.csv`）——
    #    R2 的 `--bars` 就是 `bars_total.parquet`，与前三层**同文件同窗口**，
    #    且已实测与一次性对照 `results/chk_ladder_final_bars_*.csv` **逐位相同**
    #    （7.895299 / 13.359673）→ **不需要额外步骤**，避免同一数字两个来源。

    # ── 2026-09-18 回灌 `个股线_四进三出阈值检验.md` 时发现的新缺口：
    #    correct 口径下 `entry=6` 在全区间/样本内**已经 binding**（legacy 不生效），
    #    说明 R4 的网格（entry 6/7/8/9）**下界卡在生效边界上** →
    #    最优 entry 可能落在 6 以下，本文无法判断。补 R19 扫细网格。
    ("R19", "四进三出 entry 细网格（无税）",
     ["tools/sweep_hyst.py", "--entries", "4", "4.5", "5", "5.5", "6", "6.5",
      "--exits", "4", "5", "6", "7",
      "--topn", "20", "--hold", "60", "--max-dy", "10", "--min-div3", "2",
      "--adj-mode", "correct",
      "--out", "results/adj_correct_hyst_finegrid.csv"],
     ["results/adj_correct_hyst_finegrid.csv"],
     "个股线_四进三出阈值检验.md", 55),

    # ── 2026-09-18 回灌 `个股线_股息率策略.md` §4.1/§4.2 时发现的缺口：
    #    那两张表是「裸 / ≤10% / ≥2次」×「N=10/20/30/50」×「样本内/样本外」，
    #    但 R14/R15 只给了 N=20 一层 → 24 个格子里有 18 个没有 correct 来源。
    #    补 R20~R22：三个「层」各扫一遍 N 邻域（无税，同一份 bars_total.parquet）。
    #    `≥2次` 那一层用 `--max-dy 999`（= 不限上限）而不是默认的 10，
    #    这样三行**只差一个过滤器**，才能把每层的增量拆干净。
    #
    #    ⚠️ 产物前缀沿用 `chk_` 而不是 `adj_correct_`：这三步最初是以
    #    「一次性对照」形式先跑起来的，产物已经在 `results/chk_ladder_*_nsens_*.csv`。
    #    改成别的前缀会**同一指标出现两个来源**（项目铁律）→ 保持同名，由驱动接管。
    ("R20", "层层加码·裸版 × N 邻域（无税）",
     ["tools/test_industry_neutral.py", "--bars", BARS,
      "--topn", "10", "20", "30", "50", "--hold", "60",
      "--max-dy", "999", "--min-div3", "0", "--adj-mode", "correct",
      "--out-prefix", "results/chk_ladder_naked_nsens"],
     ["results/chk_ladder_naked_nsens_backtest.csv"],
     "个股线_股息率策略.md", 10),

    ("R21", "层层加码·＋≤10% × N 邻域（无税）",
     ["tools/test_industry_neutral.py", "--bars", BARS,
      "--topn", "10", "20", "30", "50", "--hold", "60",
      "--max-dy", "10", "--min-div3", "0", "--adj-mode", "correct",
      "--out-prefix", "results/chk_ladder_maxdy_nsens"],
     ["results/chk_ladder_maxdy_nsens_backtest.csv"],
     "个股线_股息率策略.md", 10),

    ("R22", "层层加码·＋≥2次 × N 邻域（无税）",
     ["tools/test_industry_neutral.py", "--bars", BARS,
      "--topn", "10", "20", "30", "50", "--hold", "60",
      "--max-dy", "999", "--min-div3", "2", "--adj-mode", "correct",
      "--out-prefix", "results/chk_ladder_mindiv_nsens"],
     ["results/chk_ladder_mindiv_nsens_backtest.csv"],
     "个股线_股息率策略.md", 10),

    # ── 回灌 `个股线_机制归因_hyst与行业中性化.md` §六 用：legacy 那句
    #    「hyst+行业配额 样本外只有 9.34%（无税）/ 8.88%（税后）」需要 correct 对照。
    #    无税那半 R16 已给（`adj_correct_combo_notax`），这里补**税后 10%** 那半，
    #    才能和 §六 的「样本外 10 万真实」口径对齐。
    ("R23", "hyst × 行业配额（税后 10%）",
     ["tools/test_industry_neutral.py", "--bars", BARS_TAX10,
      "--topn", "20", "--hold", "60",
      "--hyst-entry", "8", "--hyst-exit", "4", "--adj-mode", "correct",
      "--out-prefix", "results/chk_hyst_indquota_tax10"],
     ["results/chk_hyst_indquota_tax10_backtest.csv"],
     "个股线_机制归因_hyst与行业中性化.md", 8),

    # ── `个股线_四进三出阈值检验.md` §11.6 是「税后复跑：结论完全一致」。
    #    R16 只给了无税，而 correct 口径下 §11 的结论**整体反转** →
    #    「税后是否也反转」必须自己跑一次，不能靠 legacy 的结论外推。
    #    （参数与 R16 逐位相同，只换 `--bars`。）
    ("R24", "叠加测试（税后 10%）：hyst 8/4 × 行业中性化",
     ["tools/test_industry_neutral.py", "--bars", BARS_TAX10,
      "--topn", "20", "--hold", "60", "--max-dy", "10", "--min-div3", "2",
      "--hyst-entry", "8", "--hyst-exit", "4", "--adj-mode", "correct",
      "--out-prefix", "results/adj_correct_combo_tax10"],
     ["results/adj_correct_combo_tax10_backtest.csv"],
     "个股线_四进三出阈值检验.md", 23),
]

# ── 2026-09-18 回灌 `个股线_股息率策略.md` §4.3（阈值邻域稳健性）时发现：
#    那张「`≤8%` / `≤12%` / `≤15%` / `≥1次` / `≥3次`」表**没有可复现来源** ——
#    产物不在 `results/`，§七 的复现命令里也没有它（属"文档写了但没脚本"那一类）。
#    补 R25~R29 把 5 个点补齐（`≤10%` / `≥2次` 已有：R21 / R22）。
#    用循环生成，避免 5 段几乎相同的字面量。
_FILTER_SWEEP = [
    ("R25", "阈值邻域·`max_dy ≤ 8%`",      "maxdy8",  ["--max-dy", "8", "--min-div3", "0"]),
    ("R26", "阈值邻域·`max_dy ≤ 12%`",     "maxdy12", ["--max-dy", "12", "--min-div3", "0"]),
    ("R27", "阈值邻域·`max_dy ≤ 15%`",     "maxdy15", ["--max-dy", "15", "--min-div3", "0"]),
    ("R28", "阈值邻域·`min_div3 ≥ 1次`",   "mindiv1", ["--max-dy", "999", "--min-div3", "1"]),
    ("R29", "阈值邻域·`min_div3 ≥ 3次`",   "mindiv3", ["--max-dy", "999", "--min-div3", "3"]),
    # ── 2026-09-18 续：`≥3次` 在三段区间全面优于定稿的 `≥2次`（+2.13/+3.14/+1.69pp），
    #    但当时只有 3 个点、无邻域 → 按铁律 3 不能下结论。
    #    补 R30/R31 把 `min_div3` 这一族的邻域补齐（`≥4次` / `≥5次`）。
    #    ⚠️ `≥4次` 意味着"近 3 年平均每年分红 >1 次"，只有半年报+年报都派的公司能满足
    #    → 候选池会明显缩小，**必须同时看 `平均持仓` 是否塌陷**（`_backtest.csv` 有该列）。
    ("R30", "阈值邻域·`min_div3 ≥ 4次`",   "mindiv4", ["--max-dy", "999", "--min-div3", "4"]),
    ("R31", "阈值邻域·`min_div3 ≥ 5次`",   "mindiv5", ["--max-dy", "999", "--min-div3", "5"]),
]
for _rid, _nm, _sfx, _extra in _FILTER_SWEEP:
    _pref = "results/chk_div_filter_" + _sfx
    STEPS.append((
        _rid, _nm,
        ["tools/test_industry_neutral.py", "--bars", BARS,
         "--topn", "20", "--hold", "60", *_extra, "--adj-mode", "correct",
         "--out-prefix", _pref],
        [_pref + "_backtest.csv"],
        "个股线_股息率策略.md", 8,
    ))


def _newest_input() -> float:
    ts = 0.0
    for p in INPUTS:
        f = os.path.join(ROOT, p)
        if os.path.exists(f):
            ts = max(ts, os.path.getmtime(f))
    return ts


def _done(outs: list[str], ref: float) -> bool:
    for o in outs:
        f = os.path.join(ROOT, o)
        if not os.path.exists(f) or os.path.getmtime(f) < ref:
            return False
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="重跑驱动（断点续跑）")
    ap.add_argument("--list", action="store_true", help="只列清单与状态")
    ap.add_argument("--only", nargs="+", default=None, help="只跑这些 id")
    ap.add_argument("--from", dest="from_id", default=None, help="从这个 id 开始")
    ap.add_argument("--force", action="store_true", help="忽略产物存在，全部重跑")
    ap.add_argument("--dry", action="store_true", help="只打印命令")
    args = ap.parse_args(argv)

    py = sys.executable
    ref = _newest_input()
    ids = [s[0] for s in STEPS]

    steps = STEPS
    if args.only:
        steps = [s for s in STEPS if s[0] in args.only]
        if not steps:
            print("--only 里没有匹配的 id。可用：" + ", ".join(ids))
            return 2
    elif args.from_id:
        if args.from_id not in ids:
            print(f"--from {args.from_id} 不存在。可用：" + ", ".join(ids))
            return 2
        steps = STEPS[ids.index(args.from_id):]

    if args.list:
        print("=" * 104)
        print(f"  重跑清单（输入数据最新 mtime = "
              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(ref))}）")
        print("=" * 104)
        print(f"  {'id':<4}{'状态':<8}{'预计':<7}{'步骤':<34}{'产物'}")
        print("  " + "-" * 100)
        for sid, name, _c, outs, _doc, est in STEPS:
            st = "已完成" if _done(outs, ref) else "待跑"
            print(f"  {sid:<4}{st:<8}{str(est)+'min':<7}{name:<34}{outs[0]}")
        tot = sum(s[5] for s in STEPS if not _done(s[3], ref))
        print("  " + "-" * 100)
        print(f"  待跑合计约 {tot:.0f} 分钟")
        if tot:
            # 读「快照复核凭证」：若窗口内收益率未变，则上面的"待跑"是 mtime 误报
            rec_p = os.path.join(ROOT, "results", "_snapshot_verified.json")
            verified = []
            if os.path.exists(rec_p):
                try:
                    import json
                    rec = json.load(open(rec_p, encoding="utf-8"))
                    for kind, r in rec.items():
                        cur = os.path.join(ROOT, r["new"])
                        if not os.path.exists(cur):
                            continue
                        if (os.path.getsize(cur) == r["new_size"]
                                and int(os.path.getmtime(cur)) == r["new_mtime"]):
                            verified.append((kind, r))
                except Exception:
                    verified = []
            if verified:
                allok = all(r["verdict"] == "OK_NO_RERUN" for _, r in verified)
                print()
                if allok:
                    print("  ✅ **已复核：窗口内收益率逐位一致 → 这些「待跑」是 mtime 误报，不必重跑**")
                else:
                    print("  ⚠️ **已复核：窗口内历史被改写 → 必须整批重跑**")
                for kind, r in verified:
                    print(f"     [{kind}] 重叠 {r['rows_compared']:,} 行｜收益率差异 "
                          f"{r['ret_diff_rows']} 行｜判定 {r['verdict']}｜复核于 {r['ts']}")
            else:
                print()
                print("  ⚠️ 判据是 **mtime**（产物比输入新才算有效），所以**数据刷新后会全部显示待跑** ——")
                print("     但若刷新只是**往后延长**数据、没改动分析窗口内的历史，重跑结果会逐位相同。")
                print("     ✅ 先确认再决定（几秒，不用跑 7 小时）：")
                print("        $PY tools/cmp_bars_snapshot.py --kind total")
                print("        $PY tools/cmp_bars_snapshot.py --kind tax10")
            print("     ⚠️ 若确认历史**被改写**了，必须 `--from R1 --force` **整批跑**：")
            print("        分批跑会让产物**混合两个数据快照**（本项目明令禁止）。")
        return 0

    print("=" * 104)
    print("  重跑驱动（断点续跑）")
    print("=" * 104)
    t0 = time.time()
    ran = skipped = 0
    for sid, name, cmd, outs, doc, est in steps:
        if not args.force and _done(outs, ref):
            print(f"  ⏭  [{sid}] {name} —— 产物已是最新，跳过")
            skipped += 1
            continue
        log = os.path.join(ROOT, "results", f"_rerun_{sid}.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        full = [py, "-u"] + cmd
        print(f"\n  ▶ [{sid}] {name}（预计 {est:g} min）→ {doc}")
        print(f"      {' '.join(cmd)}")
        if args.dry:
            continue
        t1 = time.time()
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("# " + " ".join(full) + "\n\n")
            fh.flush()
            rc = subprocess.call(full, cwd=ROOT, stdout=fh,
                                 stderr=subprocess.STDOUT)
        dt = (time.time() - t1) / 60.0
        if rc != 0:
            print(f"      ✗ 退出码 {rc}，用时 {dt:.1f} min → 看 {os.path.relpath(log, ROOT)}")
            print(f"\n  续跑：$PY tools/run_reruns.py --from {sid}")
            return rc
        missing = [o for o in outs if not os.path.exists(os.path.join(ROOT, o))]
        if missing:
            print(f"      ✗ 退出码 0 但产物缺失 {missing} → 看 "
                  f"{os.path.relpath(log, ROOT)}")
            print(f"\n  续跑：$PY tools/run_reruns.py --from {sid}")
            return 1
        ran += 1
        print(f"      ✓ 完成，用时 {dt:.1f} min")

    print("\n" + "=" * 104)
    print(f"  本轮执行 {ran} 步、跳过 {skipped} 步，总用时 {(time.time()-t0)/60:.1f} min")
    left = [s[0] for s in STEPS if not _done(s[3], ref)]
    print("  仍未完成：" + (", ".join(left) if left else "无 ✓"))
    print("=" * 104)
    return 0


if __name__ == "__main__":
    sys.exit(main())
