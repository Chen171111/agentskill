"""诊断：份额折算复权判据的「按品种涨跌停」对照（只读，不改任何数据）。

背景（2026-10-15 质检遗留项）
------------------------------
现行判据是扁平阈值 THRESH=0.35（dataprovider/adjust.py）：|单日收益| > 0.35 即视为
份额折算。它对本品种涨跌停「物理上不可能」的日波动视而不见：
  · 512690.SH（主板 ETF，±10%）2021-12-31 单日 **−27.06%** —— 真实交易不可能
    走出超过跌停价的幅度 → 必为份额折算，但 27% < 35% → **漏检**，历史序列留下假跌；
  · 同时阈值不能简单降到 0.20：159949.SZ 2024-10-08 的 **+20.02%** 是真实涨停
    （双创 20% 档 + 0.001 元取整溢出），会被误判成折算（09-16 真实误伤过）。

本脚本用 `tools/limit_rules.limit_of`（涨跌停单一来源）做对照：
  按品种判据 = |ret| > limit + tick/prev_close + 安全边际
  tick = 10^-limit_price_round(code)（ETF 0.001 元、个股 0.01 元）
物理意义：超过本品种涨跌停板的单日波动不可能是真实成交 → 只能是公司行为。
输出三张单：
  A. 按品种判据捕获、现行 0.35 漏检的跳变（= 现行判据的漏检清单）；
  B. 现行 0.35 捕获、按品种判据不捕获的（= 换判据会丢的，预期只有 30% 档
     北交所标的，池内不存在）；
  C. 已知真实涨停抽查（159949/159915/588000/159819）在按品种判据下是否误报。

运行：E:/Python32/python.exe tools/diag_split_limit_criterion.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from dataprovider.store import DataStore
from dataprovider.adjust import THRESH
from tools.limit_rules import limit_of, limit_price_round

SAFETY = 0.001          # 0.1pp 安全边际（取整溢出之外的其他杂讯）
STOCK_DIR = Path(__file__).resolve().parent.parent / "data" / "stocks"


def _eps(code: str, prev_close: float) -> float:
    tick = 10 ** (-limit_price_round(code))
    return tick / prev_close + SAFETY if prev_close > 0 else SAFETY


def main():
    raw = DataStore(repair_splits=False)     # 未复权原始序列
    newly, lost, checked = [], [], []
    for p in sorted(STOCK_DIR.glob("*.csv")):
        code = p.stem
        df = raw.read(code)
        lim = limit_of(code)
        r = df["close"].pct_change()
        for dt, v in r.items():
            if v != v:
                continue
            i = df.index.get_loc(dt)
            if i == 0:
                continue
            pc = float(df["close"].iloc[i - 1])
            if pc <= 0:
                continue
            av = abs(v)
            cur_flag = av > THRESH
            if lim is None:                 # 指数等：回落现行扁平阈值
                lim_flag = cur_flag
                thr_txt = "THRESH(无档位)"
            else:
                thr = lim + _eps(code, pc)
                lim_flag = av > thr
                thr_txt = "limit={:.0%}+eps".format(lim)
            if lim_flag and not cur_flag:
                newly.append((code, str(dt), pc, float(df["close"].iloc[i]), v, thr_txt))
            if cur_flag and not lim_flag:
                lost.append((code, str(dt), pc, float(df["close"].iloc[i]), v, thr_txt))
        # C：已知真实涨停抽查
        if code in ("159949.SZ", "159915.SZ", "588000.SH", "159819.SZ"):
            for dt, v in r.items():
                if v != v:
                    continue
                i = df.index.get_loc(dt)
                if i == 0:
                    continue
                pc = float(df["close"].iloc[i - 1])
                thr = lim + _eps(code, pc)
                if abs(v) > 0.15:           # 只列 >15% 的大波动日
                    checked.append((code, str(dt), pc, float(df["close"].iloc[i]), v,
                                    thr, abs(v) > thr))

    print("=" * 78)
    print("A. 按品种判据捕获、现行 0.35 漏检的跳变（漏检清单）:")
    for c, dt, pc, cl, v, t in newly:
        print("   {}  {}  {:.4f}->{:.4f}  {:+.2%}   [{}]".format(c, dt, pc, cl, v, t))
    if not newly:
        print("   （无）")
    print()
    print("B. 现行 0.35 捕获、按品种判据不捕获的（换判据的损失，预期空）:")
    for c, dt, pc, cl, v, t in lost:
        print("   {}  {}  {:.4f}->{:.4f}  {:+.2%}   [{}]".format(c, dt, pc, cl, v, t))
    if not lost:
        print("   （无）")
    print()
    print("C. 已知真实涨停抽查（>15% 的大波动日，按品种判据是否误报）:")
    for c, dt, pc, cl, v, thr, misfire in checked:
        print("   {}  {}  {:+.2%}  判据线 {:.2%}  误报={}".format(c, dt, v, thr, misfire))
    print()
    print("结论: A 非空 = 现行判据存在同类漏检（512690 在列则证实）; "
          "B 必须为空（换判据不漏掉现行能抓的）; C 必须全 False（不误伤真实涨停）。")


if __name__ == "__main__":
    main()
