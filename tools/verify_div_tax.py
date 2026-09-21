"""D4 验证：引擎内扣税 vs 换税后价格路径。

核心判据
--------
1. **组合恒等**：`--tax-rate 0.1` 与不扣税的 `trades` 必须**逐位相同**
   （价格路径没变，只是多了一笔现金流出）→ Δ 才干净地等于税负。
2. **税负量级**：与旧口径（`bars_total_tax10.parquet`）的 Δ 应当同向、量级相近。

用法
----
    PY="C:/Users/XiaoQi/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
    cd "E:/MyWorkAndProject/量化/agentskill"
    $PY -u tools/verify_div_tax.py                 # 用默认参数（topk=50 / hold=5 / 2019~2026-09-11）
    $PY -u tools/verify_div_tax.py --help          # ← 2026-09-21 前这行会**卡死**（见下）

⚠️ 2026-09-21 修：本脚本原来**没有 `main()`、也没有 `if __name__ == "__main__"` 守卫**，
   全部工作都写在模块层。后果有两条：
   1. `--help` **不会打印帮助，而是直接开始跑完整回测**（实测 27 秒仍未返回）——
      任何"探一下 `--help`"的自动化都会挂在这里；
   2. `import tools.verify_div_tax` 会**触发一次完整回测**（隐性副作用）。
   改法：把模块层的工作整体搬进 `main()`，参数走 argparse。
   **计算逻辑与打印文本一字未动** —— 改造前后输出已逐字 diff 验证一致。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import pandas as pd  # noqa: E402

from tools.backtest_stock import build_features, run, metrics  # noqa: E402

BARS = os.path.join(ROOT, "data/stockbars/bars_total.parquet")
BARS_TAX = os.path.join(ROOT, "data/stockbars/bars_total_tax10.parquet")
UNI = os.path.join(ROOT, "data/stockbars/universe_all.csv")
DIV = os.path.join(ROOT, "data/stockbars/div_tax_table.parquet")

ALL8 = [("rev20", 1.0), ("rev60", 1.0), ("rev120", 1.0), ("rev5", 1.0),
        ("vol20", 1.0), ("max20", 1.0), ("turn20", 1.0), ("illiq20", 1.0)]


def load(path):
    b = pd.read_parquet(path)
    b["date"] = b.date.astype(str).str.replace("-", "", regex=False)
    u = pd.read_csv(UNI, dtype=str)
    st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
    return b[~b.code.isin(st)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="D4 验证：引擎内扣税 vs 换税后价格路径")
    ap.add_argument("--topk", type=int, default=50)
    ap.add_argument("--hold", type=int, default=5)
    ap.add_argument("--weight-mode", default="equal",
                    choices=["equal", "tilt", "rank"],
                    help="与 backtest_stock.run() 一致（默认 equal）")
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260911")
    ap.add_argument("--tax-rate", type=float, default=0.10,
                    help="引擎内扣税率（默认 0.10 = 10%% 档）")
    args = ap.parse_args(argv)

    base = dict(topk=args.topk, hold=args.hold, weight_mode=args.weight_mode)
    s, e = args.start, args.end

    div = pd.read_parquet(DIV)
    div["date"] = div.date.astype(str)
    print("派现表 {:,} 行 / {} 只 / {}~{}".format(
        len(div), div.code.nunique(), div.date.min(), div.date.max()), flush=True)

    print("\n计算因子（bars_total，税前）…", flush=True)
    df = build_features(load(BARS))
    print("因子完成", flush=True)

    res = {}
    for tag, kw in [("① 无税", {}),
                    ("② 引擎内扣税 10%", dict(div_tax=div, tax_rate=args.tax_rate))]:
        eq, tr, meta = run(df, ALL8, start=s, end=e, **base, **kw)
        m = metrics(eq.equity)
        res[tag] = (m, tr, meta)
        print("  {}  年化 {:>6.2f}%  夏普 {:>5.2f}  回撤 {:>7.2f}%  交易 {:>4} 笔  "
              "tax_paid {:.4f}  扣税事件 {:,}".format(
                  tag, m["年化收益"], m["夏普比率"], m["最大回撤"], len(tr),
                  meta["tax_paid"], meta["n_tax_events"]), flush=True)

    a, b = res["① 无税"][1], res["② 引擎内扣税 10%"][1]
    same = a.shape == b.shape and (a.values == b.values).all()
    print("\n  ── 判据 1：组合恒等 ──")
    print("    trades 逐位相同 : {}  （无税 {} 笔 / 扣税 {} 笔）".format(same, len(a), len(b)))
    print("    年化差（税负）  : {:+.3f}pp".format(
        res["② 引擎内扣税 10%"][0]["年化收益"] - res["① 无税"][0]["年化收益"]))

    print("\n计算因子（bars_total_tax10，旧口径）…", flush=True)
    df_tax = build_features(load(BARS_TAX))
    eq, tr, meta = run(df_tax, ALL8, start=s, end=e, **base)
    m = metrics(eq.equity)
    print("  {}  年化 {:>6.2f}%  夏普 {:>5.2f}  回撤 {:>7.2f}%  交易 {:>4} 笔".format(
        "③ 旧口径 tax10 路径", m["年化收益"], m["夏普比率"], m["最大回撤"], len(tr)))

    c = tr
    same_ac = a.shape == c.shape and (a.values == c.values).all()
    print("\n  ── 判据 2：旧口径是否真的换了组合 ──")
    print("    旧口径 trades 与无税相同 : {}".format(same_ac))
    print("    年化：无税 {:.2f}% ｜ 引擎内扣税 {:.2f}% ｜ 旧口径 {:.2f}%".format(
        res["① 无税"][0]["年化收益"], res["② 引擎内扣税 10%"][0]["年化收益"],
        m["年化收益"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
