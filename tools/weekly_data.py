"""周度数据任务：**分红 / 行业 / 指数** 的定期刷新（HANDOFF §4.2 P1）。

为什么需要
----------
`tools/refresh_data.py` 只管 **ETF 行情 + 体检**，这三份研究数据**不会自动更新**：

| 文件 | 用途 | 滞后后果 |
|---|---|---|
| `data/dividends/bonus_all.parquet` | 个股线**股息率面板**的输入 | 新分红漏掉 → `dy_ttm` 偏低 → 选股错 |
| `data/industry/industry_all.parquet` | 行业中性化 / 行业配额 | 新上市股无行业 → 落「未分类」桶 |
| `data/indexes/`（6 个指数日线） | 基准对照（中证1000 等） | 超额算错 |

它们**数据量都很小**（实测合计 < 2MB）→ **全量重抓即可**，不需要做增量逻辑
（增量逻辑反而更容易出错：分红有「预案 / 实施」两态，见 `fetch_dividend.load_events` 的注释）。

⚠️ 与 `refresh_data.py` 的分工：本脚本**不碰** ETF 行情、**不碰**个股日线
（后者由 `daily_learn.py` 的步骤①用 `append_stock_bars.py` 增量补）。

用法
----
    E:\\Python\\python.exe tools/weekly_data.py              # 全部（建议用 64 位解释器）
    E:\\Python\\python.exe tools/weekly_data.py --only dividend,industry
    E:\\Python\\python.exe tools/weekly_data.py --dry        # 只打印将执行的命令

退出码：0 = 全部成功；3 = 有步骤失败（并写 `state/DATA_ALERT.txt`）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
# ⚠️ 用**独立**告警文件，不碰 `DATA_ALERT.txt` —— 那个是 `refresh_data.py`（每日体检）写的，
# 周度任务成功就把它清掉会**掩盖每日数据问题**（`check_alerts.py` 的注释专门强调过这一点）。
DATA_ALERT = STATE / "WEEKLY_ALERT.txt"

# (key, 说明, 命令模板)  —— {PY} 会替换成当前解释器
STEPS = [
    ("dividend", "分红送配（全市场）",
     ["tools/fetch_dividend.py", "--out", "data/dividends",
      "--start", "2016-01-01"]),
    ("industry", "行业分类（东财 EM2016）",
     ["tools/fetch_industry.py", "--out", "data/industry/industry_all.parquet",
      "--universe", "data/stockbars/universe_all.csv"]),
    ("index", "指数日线（6 个基准/风格指数）",
     None),          # end 要动态算，见 _cmd_for
]


def _cmd_for(key: str):
    """返回该步骤的命令（args 已展开）。`index` 的 --end 需要动态日期。"""
    for k, _, tpl in STEPS:
        if k != key:
            continue
        if tpl is not None:
            return list(tpl)
        # 指数：--end 默认值写死成 2026-09-12，必须显式传「昨天」
        end = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        return ["tools/fetch_index_history.py", "--out", "data/indexes",
                "--start", "2018-01-01", "--end", end]
    raise KeyError(key)


def _write_data_alert(failed):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        lines = ["=" * 60, "【周度数据任务失败】", "=" * 60,
                 "时间   : {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                 "失败步骤: {}".format(", ".join(failed)), "",
                 "处理: 看下方日志，或手动重跑对应脚本："]
        for k in failed:
            lines.append("    {} tools/{}".format(
                sys.executable, " ".join(_cmd_for(k))))
        lines.append("=" * 60)
        DATA_ALERT.write_text("\n".join(lines), encoding="utf-8")
    except Exception as e:
        print("[weekly_data] 写 DATA_ALERT 失败（忽略）: {}".format(e))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="周度数据任务：分红 / 行业 / 指数")
    ap.add_argument("--only", default=None,
                    help="只跑这些步骤（逗号分隔）：dividend,industry,index")
    ap.add_argument("--dry", action="store_true", help="只打印命令，不执行")
    args = ap.parse_args(argv)

    want = set((args.only or "dividend,industry,index").split(","))
    print("=" * 92)
    print("  周度数据任务  {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    print("  解释器: {}".format(sys.executable))
    print("=" * 92)

    failed, results = [], []
    for key, desc, _ in STEPS:
        if key not in want:
            print("\n  [{}] {} —— 跳过（--only 未选中）".format(key, desc))
            continue
        cmd = [sys.executable, "-u"] + _cmd_for(key)
        print("\n  [{}] {}".format(key, desc))
        print("      $ {}".format(" ".join(cmd)))
        if args.dry:
            continue
        t0 = time.time()
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        tail = "\n".join((r.stdout or "").strip().splitlines()[-4:])
        print("      退出码 {}，用时 {:.0f}s".format(r.returncode, time.time() - t0))
        if tail:
            print("      " + tail.replace("\n", "\n      "))
        results.append((key, r.returncode, round(time.time() - t0, 1)))
        if r.returncode != 0:
            failed.append(key)
            if r.stderr:
                print("      stderr: " + (r.stderr.strip().splitlines() or [""])[-1])

    print("\n" + "=" * 92)
    if args.dry:
        print("  [dry] 未执行任何抓取")
        return 0
    if failed:
        _write_data_alert(failed)
        print("  ❌ 失败步骤: {} —— 已写 state/DATA_ALERT.txt".format(", ".join(failed)))
        return 3
    print("  ✅ 全部成功: " + " ｜ ".join(
        "{} {}s".format(k, t) for k, rc, t in results))
    # 成功路径清除旧的 DATA_ALERT（与 daily_job 的成功清 ALERT 同理）
    try:
        if DATA_ALERT.exists():
            DATA_ALERT.unlink()
            print("  （已清除旧的 state/DATA_ALERT.txt）")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
