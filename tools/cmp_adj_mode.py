"""送转调整口径「legacy vs correct」对照回测分析 —— 立项修的**判据机**。

背景
----
`docs/个股线_送转调整口径缺陷.md`：`build_yield_panel` 的送转调整 `adj[i] = Π_{j>i}(1+r_j)`
有两个错（方向该除却乘、时间上不设上界）。已加 `adj_mode='legacy'|'correct'` 开关
（默认 legacy，既有结论逐位不变）。

本脚本把两份对照回测结果摆在一起，**按 §五 的 6 条验收判据自动判定**
（判据① 中立性由 `joinquant/_verify_core.py` 负责，不在这里）。

用法
----
    PY="C:/Users/XiaoQi/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
    cd "E:/MyWorkAndProject/量化/agentskill"

    # 先各跑一遍（约 15 分钟/次）
    $PY tools/test_industry_neutral.py --bars data/stockbars/bars_total_tax10.parquet \\
        --topn 15 20 30 --hold 60 --adj-mode legacy  --out-prefix results/adjfix_legacy
    $PY tools/test_industry_neutral.py --bars data/stockbars/bars_total_tax10.parquet \\
        --topn 15 20 30 --hold 60 --adj-mode correct --out-prefix results/adjfix_correct

    # 再分析
    $PY tools/cmp_adj_mode.py
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ORDER = ["全区间", "样本内", "样本外"]
COLS = ["年化%", "夏普", "回撤%", "10万真实年化%"]


def _load(path: str, tag: str) -> pd.DataFrame:
    if not os.path.exists(path):
        raise SystemExit("❌ 缺少 {}（先跑 {} 那一遍）".format(path, tag))
    d = pd.read_csv(path)
    d["口径"] = tag
    return d


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="送转口径 legacy vs correct 对照分析")
    ap.add_argument("--legacy", default="results/adjfix_legacy_backtest.csv")
    ap.add_argument("--correct", default="results/adjfix_correct_backtest.csv")
    args = ap.parse_args(argv)

    L = _load(args.legacy, "legacy")
    C = _load(args.correct, "correct")
    both = pd.concat([L, C], ignore_index=True)
    key = ["区间", "模式", "N"]

    print("=" * 100)
    print("  送转调整口径对照：legacy（现行）vs correct（价值中性）")
    print("=" * 100)

    for metric in COLS:
        print()
        print("  ── {} ──".format(metric))
        piv = both.pivot_table(index=["区间", "模式", "N"], columns="口径",
                               values=metric, aggfunc="first")
        piv = piv.reindex(columns=["legacy", "correct"])
        piv["Δ(correct−legacy)"] = piv["correct"] - piv["legacy"]
        piv = piv.reset_index()
        piv["_o"] = piv["区间"].map({r: i for i, r in enumerate(ORDER)})
        piv = piv.sort_values(["_o", "模式", "N"]).drop(columns="_o")
        print(piv.to_string(index=False, float_format=lambda x: "{:>8.2f}".format(x)))

    # ---------------- 判据机 ----------------
    print()
    print("=" * 100)
    print("  判据机（`docs/个股线_送转调整口径缺陷.md` §五）")
    print("=" * 100)
    piv = both.pivot_table(index=["区间", "模式", "N"], columns="口径",
                           values="10万真实年化%", aggfunc="first")
    piv["Δ"] = piv["correct"] - piv["legacy"]
    piv = piv.reset_index()

    def d(mode, n, seg):
        r = piv[(piv.模式 == mode) & (piv.N == n) & (piv.区间 == seg)]
        return float(r["Δ"].iloc[0]) if len(r) else float("nan")

    verdicts = []

    # 判据③ 样本外方向（indpct N=20）
    so = d("indpct", 20, "样本外")
    verdicts.append(("③ 样本外方向", "样本外 Δ = {:+.2f}pp".format(so),
                     "修正后**掉多少**（这是决定要不要采纳的关键）"))

    # 判据④ 样本内外一致
    si = d("indpct", 20, "样本内")
    verdicts.append(("④ 样本内外一致",
                     "样本内 Δ = {:+.2f}pp ／ 样本外 Δ = {:+.2f}pp".format(si, so),
                     "✅ 同号" if si * so > 0 else "⚠️ 异号（只有一边变化，需警惕）"))

    # 判据⑤ N 邻域同向
    ns = [d("indpct", n, "样本外") for n in sorted(piv.N.unique())]
    ns_ok = all(x < 0 for x in ns) or all(x > 0 for x in ns)
    verdicts.append(("⑤ N 邻域同向",
                     "样本外 Δ by N: " + " ／ ".join(
                         "N={} {:+.2f}".format(n, x)
                         for n, x in zip(sorted(piv.N.unique()), ns)),
                     "✅ 全部同向" if ns_ok else "⚠️ 方向不一致"))

    # 机制：indpct 相对 topn 的优势是否仍成立
    def adv(seg, tag):
        a = piv[(piv.模式 == "indpct") & (piv.N == 20) & (piv.区间 == seg)][tag].iloc[0]
        b = piv[(piv.模式 == "topn") & (piv.N == 20) & (piv.区间 == seg)][tag].iloc[0]
        return float(a), float(b), float(a - b)
    ok_mech = True
    mech_lines = []
    for seg in ORDER:
        try:
            la, lb, laa = adv(seg, "legacy")
            ca, cb, caa = adv(seg, "correct")
            mech_lines.append("  {}  indpct−topn: legacy {:+.2f}pp ｜ correct {:+.2f}pp".format(
                seg, laa, caa))
            ok_mech &= (caa > 0)
        except Exception:
            pass
    verdicts.append(("⑥ 机制（行业内百分位仍优于全局排名）", "\n     " + "\n     ".join(mech_lines),
                     "✅ 三个区间都仍为正" if ok_mech else "⚠️ 有区间转负"))

    for name, ev, v in verdicts:
        print()
        print("  【{}】".format(name))
        print("     {}".format(ev))
        print("     → {}".format(v))

    print()
    print("=" * 100)
    print("  三种可接受结局（见缺陷文档 §五）：① 略降、方向不变 → 保留定稿、更新数字")
    print("    ② 明显变差 → 部分 alpha 来自前视，降预期甚至暂停")
    print("    ③ 变好 → 先查是不是又引入了新的口径错误")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
