"""扫描 `data/stocks` 全部 ETF 的单日 |收益| 分布，定位份额折算判据的安全间隙。

为什么要这个脚本
----------------
份额折算的判据是 `|单日收益| > THRESH`（见 `dataprovider/adjust.py`）。
阈值不能拍脑袋 —— 它必须**高于真实涨跌停的最高档**、**低于真实折算的最小幅度**。
本脚本把两个分布都打出来，让阈值的选择有实测依据。

实测结果（2026-09-16，`data/stocks` 27 只 ETF、176 处 |收益| ≥ 9% 的事件）
--------------------------------------------------------------------------
| 区间      | 数量 | 性质                     |
|-----------|------|--------------------------|
| 9% ~ 11%  | 156  | 主板 ETF 真实涨跌停      |
| 11% ~ 19% |   9  | 双创 ETF 真实涨跌         |
| 19% ~ 21% |   7  | 双创 ETF 真实涨停（见下） |
| **21% ~ 30%** | **0** | **空白**            |
| **30% ~ 40%** | **0** | **空白**            |
| 40% ~ 60% |   1  | 真实份额折算             |
| > 60%     |   3  | 真实份额折算             |

19%~21% 那 7 处全部是**真实涨停**，不是折算：
    159915.SZ@20240930 +20.00%   159915.SZ@20241008 +19.98%
    159949.SZ@20240930 +19.98%   159949.SZ@20241008 +20.02%
    588000.SH@20240930 +19.95%   588000.SH@20241008 +20.00%
    588170.SH@20260721 +19.02%
40% 以上 4 处全部是**真实份额折算**（池内 4 处，与 `dataprovider.adjust` 自检一致）：
    159928.SZ@20210625 -74.47%   510500.SH@20150415 +248.55%
    513100.SH@20220114 -80.45%   513500.SH@20220330 -49.18%

→ 真实涨跌停最高 **20.02%**，真实折算最低 **49.18%**，中间 **21%~40% 完全空白**。
→ `THRESH = 0.35` 落在这条空白带里：距最高涨跌停带 14pp，距最小真实折算 14.2pp；
   并且**高于北交所 30% 档**（该档 ETF 尚未上市，属制度预留），
   所以北证50 ETF 上市后本判据依然成立。

⚠️ 已知边界
-----------
1. 本脚本只覆盖 `data/stocks` 里已有的 ETF（27 只），**不是全市场 1000+ 只 ETF**。
   空白带的结论在样本内成立；换数据源或扩池后应重跑本脚本复核。
2. 涨跌幅限制价格按 `前收盘价 ×(1±比例)` **四舍五入至 0.001 元**（深交所《交易规则》3.3.19），
   所以实际单日涨幅可**略超**名义比例。实测 `159819.SH@20240930 = +10.07%`（10% 档）。
   理论上低价 ETF 的偏离可达 +0.1pp 量级，对 0.35 无威胁，但别把阈值贴着 20.0% 取。
3. **固定阈值有天花板**：若将来真出现"无涨跌幅限制"的场内品种，任何固定阈值都可能误判。
   根治要靠二次判据（精确份额比 / 净值交叉验证 / 折算公告白名单），详见
   `docs/参考_A股ETF涨跌幅限制.md` §六。

用法
----
    $PY tools/scan_return_bands.py                 # 默认扫 data/stocks
    $PY tools/scan_return_bands.py --dir data/stocks --floor 0.09
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ETF 代码前缀（沪市 51/56/58，深市 15/16）
ETF_PREFIX = ("51", "56", "58", "15", "16")

BANDS = [
    (0.09, 0.11),
    (0.11, 0.19),
    (0.19, 0.21),
    (0.21, 0.30),
    (0.30, 0.40),
    (0.40, 0.60),
    (0.60, 999.0),
]


def is_etf(code: str) -> bool:
    return code.split(".")[0][:2] in ETF_PREFIX


def scan(d: str, floor: float) -> tuple[list, int, int]:
    """返回 ([(code, date, ret)], csv 总数, ETF 数)。"""
    files = [f for f in sorted(os.listdir(d)) if f.endswith(".csv")]
    etfs = [f for f in files if is_etf(f[:-4])]
    rows = []
    for f in etfs:
        code = f[:-4]
        df = (pd.read_csv(os.path.join(d, f), dtype={"date": str})
                .set_index("date").sort_index())
        if "close" not in df.columns:
            continue
        r = df.close.pct_change()
        for dt, v in r.items():
            if v == v and abs(v) >= floor:
                rows.append((code, dt, float(v)))
    return rows, len(files), len(etfs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/stocks")
    ap.add_argument("--floor", type=float, default=0.09,
                    help="只列出 |收益| >= 此值的事件（默认 0.09）")
    args = ap.parse_args()

    rows, n_files, n_etfs = scan(args.dir, args.floor)
    print(f"{args.dir}: {n_files} 个 csv，其中 ETF {n_etfs} 只")
    print(f"|日收益| >= {args.floor:.0%} 的事件：{len(rows)} 处\n")

    hdr = f"{'band':>12} {'n':>4}   detail"
    print(hdr)
    print("-" * len(hdr))
    for lo, hi in BANDS:
        if lo < args.floor:
            continue
        hit = [x for x in rows if lo <= abs(x[2]) < hi]
        label = f"{lo:.0%}~{hi:.0%}" if hi < 999 else f">{lo:.0%}"
        detail = ", ".join(f"{c}@{d}:{v:+.2%}" for c, d, v in hit[:8])
        if len(hit) > 8:
            detail += f" ...(+{len(hit) - 8})"
        print(f"{label:>12} {len(hit):>4}   {detail}")

    print("\n=== 21%~40% 逐条明细（判据安全间隙的关键区） ===")
    gap = [x for x in rows if 0.21 <= abs(x[2]) < 0.40]
    if not gap:
        print("  （空 —— 说明该区间没有真实事件，阈值取在这里是安全的）")
    for c, d, v in gap:
        print(f"  {c} {d} {v:+.4%}")

    print("\n=== 40% 以上逐条明细（应为真实份额折算） ===")
    for c, d, v in rows:
        if abs(v) >= 0.40:
            print(f"  {c} {d} {v:+.4%}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
