"""批量修复 data/stocks 下**全部 ETF** 的份额拆分/折算未复权问题。

背景
----
`data/stocks/*.csv` 是 akshare 的**不复权**行情。ETF 份额拆分/折算时价格会跳变
（如 1 拆 4 → 价格 −75%），但真实收益率连续，引擎却当成真实涨跌。

之前只修复过 `ETF全球` 池内的 11 只；扫描发现**其余 ETF 大量仍有跳变**，
包括 512010/512100/512170/512480/512690/512800/515880/588170 等。

阈值选择（关键）
----------------
`THRESH = 0.35`。三个约束（实测见 `tools/scan_return_bands.py`）：

1. 必须 **> 现行最高涨跌停档**：创业板/科创板 ETF 就是 20%
   （`159949` 2024-10-08 的 +20.02% 是**真实涨停**，曾被 0.20 误判成折算）；
   且涨跌幅价格四舍五入至 0.001 元，实际涨幅可略超名义比例（实测 10.07%）。
2. 必须 **> 北交所预留档 30%**：北证50 ETF 尚未上市，但上市即 30% 档。
   这是本脚本从 0.25 上调到 0.35 的原因（0.25 在那天会失效）。
3. 必须 **< 最小真实折算幅度**：本库实测最小 **49.18%**（`513500.SH` 2022-03-30）。

实测 `data/stocks` 27 只 ETF 的 |单日收益| 分布：21%~40% **完全空白**，
0.35 落在这条空白带里（距最高涨跌停带 14pp、距最小真实折算 14.2pp）。

⚠️ 本判据的单一来源是 `dataprovider/adjust.py`（读取时复权）。
本脚本是**离线批量修复**的旧路径，阈值须与之一致，勿各自改。
涨跌幅分档的完整制度依据见 `docs/参考_A股ETF涨跌幅限制.md`。

修复方法
--------
对每个跳变日，把**该日之前**的价格整段乘以跳变比率，使收益率序列连续
（等价于**前复权**：末值不变，只重标早期段）。多次跳变按日期升序依次处理。

安全
----
- 默认**只诊断不修改**；加 `--repair` 才写文件
- 写文件前先整目录备份到 `data/stocks_backup_<YYYYMMDD>/`
- 修复后自动复检，确认无残留异常

用法
----
    $PY tools/repair_etf_adjust.py                # 只诊断
    $PY tools/repair_etf_adjust.py --repair       # 备份 + 修复
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                              # noqa: E402

DATA = "data/stocks"
THRESH = 0.35   # 与 dataprovider/adjust.py 保持单一来源，勿各自改（理由见模块 docstring）


def list_etfs(d=DATA):
    out = []
    for f in sorted(os.listdir(d)):
        if not f.endswith(".csv"):
            continue
        code = f[:-4]
        if code.split(".")[0][:2] in ("51", "56", "58", "15", "16"):
            out.append(code)
    return out


def scan(code, d=DATA, thresh=THRESH):
    p = os.path.join(d, code + ".csv")
    df = pd.read_csv(p, dtype={"date": str}).set_index("date").sort_index()
    r = df.close.pct_change()
    hits = []
    for dt, v in r.items():
        if v == v and abs(v) > thresh:
            i = df.index.get_loc(dt)
            hits.append((dt, float(df.close.iloc[i - 1]), float(df.close.iloc[i]), float(v)))
    return df, hits


def repair(d: pd.DataFrame, jumps) -> pd.DataFrame:
    """把每个跳变日**之前**的价格整段乘跳变比率（前复权）。"""
    out = d.copy()
    for dt, prev_close, close, ret in jumps:            # 按日期升序
        ratio = close / prev_close
        out.loc[out.index < dt, ["open", "high", "low", "close"]] *= ratio
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repair", action="store_true", help="备份并写回修复结果")
    ap.add_argument("--thresh", type=float, default=THRESH)
    args = ap.parse_args()

    codes = list_etfs()
    print(f"扫描 {len(codes)} 只 ETF（阈值 |日收益| > {args.thresh:.0%}）\n")

    total, touched = 0, {}
    for c in codes:
        try:
            df, hits = scan(c, thresh=args.thresh)
        except Exception as e:
            print(f"  {c} 读取失败: {e}")
            continue
        if not hits:
            continue
        touched[c] = len(hits)
        total += len(hits)
        nm = config.ETF_NAMES.get(c, "")
        print(f"{c} {nm}")
        for dt, pc, cl, r in hits:
            print(f"    {dt}  {pc:>9.3f} -> {cl:>9.3f}   ({r:+.1%})")
        print()

    print("=" * 72)
    print(f"合计 {total} 处异常，涉及 {len(touched)} 只 ETF")
    if not args.repair:
        print("（加 --repair 备份并写回修复结果）")
        return 0

    # ---- 备份 ----
    stamp = datetime.now().strftime("%Y%m%d")
    bk = f"data/stocks_backup_{stamp}"
    if os.path.exists(bk):
        bk = f"data/stocks_backup_{stamp}_2"
    shutil.copytree(DATA, bk)
    print(f"\n已备份: {DATA} -> {bk}")

    # ---- 修复 ----
    fixed = 0
    for c in touched:
        df, hits = scan(c, thresh=args.thresh)
        rp = repair(df, hits)
        rp.reset_index().to_csv(os.path.join(DATA, c + ".csv"), index=False,
                                encoding="utf-8-sig")
        fixed += 1
    print(f"已修复 {fixed} 只 ETF")

    # ---- 复检 ----
    print("\n复检：")
    left = 0
    for c in codes:
        try:
            _, hits = scan(c, thresh=args.thresh)
            if hits:
                left += len(hits)
                print(f"  ⚠️ {c} 仍有 {len(hits)} 处: "
                      + ", ".join(f"{d}({r:+.1%})" for d, _, _, r in hits))
        except Exception:
            pass
    print("  ✓ 全部干净" if left == 0 else f"  ⚠️ 残留 {left} 处")
    return 0


if __name__ == "__main__":
    sys.exit(main())
