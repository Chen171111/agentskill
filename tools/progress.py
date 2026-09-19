"""增量落盘 —— 长任务的「已完成部分」保护（**单一来源**）。

为什么必须有它
--------------
项目铁律写着「长任务必须**增量落盘 + 断点续跑**」（主人机器有死机史），
但 `sweep_hyst` / `test_industry_neutral` / `sweep_dividend_into_mf` 这些
多配置循环脚本**原本只在结尾写一次 CSV** → 中途被掐断就**全丢**。

实测（2026-09-18）：`sweep_hyst` 跑到第 12 分钟被宿主 SIGTERM（前台命令有时限），
**12 分钟全部白跑、`results/` 里连半成品都没有**。而改成每跑完一个区间就落盘，
被掐断时至少能拿到已完成区间 —— 对「哪个区间的结论已经能看」这件事是决定性的。

设计取舍
--------
用**覆盖写全量**（不是 append）：调用方把"到目前为止收集到的所有行"传进来，
函数整体重写目标文件。

- 幂等：调用多次结果相同，不会产生重复行
- 无需状态管理：不依赖"上次写到哪"的游标，断了重跑即可
- 最后那次调用写出的就是完整结果，与原来的"结尾写一次"行为一致

用法
----
    from tools.progress import flush_partial
    rows = []
    for period in periods:
        for cfg in cfgs:
            rows.append(run(cfg))
        flush_partial(rows, f"{prefix}_backtest.csv", tag=period)
"""
from __future__ import annotations

import os

import pandas as pd


def flush_partial(rows, out: str, tag: str = "", quiet: bool = False) -> int:
    """把已完成的行**覆盖写**入 `out`，返回写入行数。

    `tag` 只用于日志（通常是"刚跑完的那个区间/分组名"）。
    """
    if not rows:
        return 0
    d = os.path.dirname(out)
    if d:
        os.makedirs(d, exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8-sig")
    if not quiet:
        suffix = f"（刚完成：{tag}）" if tag else ""
        print(f"      [进度] 已增量落盘 {len(rows)} 行 → {out}{suffix}", flush=True)
    return len(rows)
