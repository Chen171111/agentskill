"""个股线数据「往后追加」—— 只抓最近几天，补进已有的分页文件。

为什么需要（2026-09-16 发现）
------------------------------
个股线研究数据 `data/stockbars/bars_bfq.parquet` 停在 **20260911**，
而现有抓取脚本**只支持往前补历史**：

| 脚本 | 增量逻辑 | 能否追加 |
|---|---|---|
| `fetch_stock_history.py bars` | `have_until` = 已有数据的**最早**日期，给了就往前抓 | ❌ |
| `fetch_stock_bfq.py fetch` | 分页文件**已存在就跳过**（`--force` 才重抓） | ❌ |

→ 要更新到最新只能 `--force` **全量重抓 5260 只**（长任务，且主人机器有死机史）。
本脚本补上「**只抓最近 N 天并追加**」这条路径。

为什么能这么简单
----------------
`fetch_stock_bfq.fetch_one(code, start, end)` 的分页是**从 `end` 往回翻**
（`cursor = end` → 逐页往前，直到覆盖 `start`）。
所以 `fetch_one(code, "2026-09-10", "2026-09-16")` 正好只抓最近几天。

用法
----
    PY=.../python.exe

    # 1) 先看会抓哪些（不写盘）
    $PY tools/append_stock_bars.py --out data/stockbars --dry-run

    # 2) 正式追加（断点续跑：已是目标的代码会跳过）
    $PY tools/append_stock_bars.py --out data/stockbars --workers 8

    # 3) 合并 + 重建总收益（两档税）
    $PY tools/fetch_stock_bfq.py merge --out data/stockbars
    $PY tools/rebuild_returns.py --bfq data/stockbars/bars_bfq.parquet \
        --dividends data/dividends/bonus_all.parquet --tax-rate 0.10 \
        --out data/stockbars/bars_total_tax10.parquet

安全性
------
- **每个代码单独写**，写前先写 `.tmp` 再 `os.replace`（原子替换）→ 中途崩不会损坏已有数据
- 已是最新的代码**直接跳过** → 重跑本命令 = 断点续跑
- `--overlap N`（默认 5）：从「已有最新日期往前 N 天」开始抓，容忍数据源回溯修订
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.fetch_stock_bfq import COLS, fetch_one, to_symbol  # noqa: E402

_lock = threading.Lock()


def _max_date(path: str):
    try:
        d = pd.read_parquet(path, columns=["date"])
        return str(d.date.max()) if len(d) else None
    except Exception:
        return None


def _append_one(code: str, part_dir: str, target: str, overlap: int, dry: bool):
    """把 [target-最近] 的新行追加进分页文件。返回 (code, 新增行数, 状态)。"""
    p = os.path.join(part_dir, f"{code}.parquet")
    have = _max_date(p) if os.path.exists(p) else None
    if have is not None and have >= target:
        return code, 0, "已最新"

    # 起点：已有最新日期往前 overlap 天（容忍数据源回溯修订）；没有历史就抓 1 年
    if have:
        start = (datetime.strptime(have, "%Y%m%d")
                 - timedelta(days=overlap)).strftime("%Y-%m-%d")
    else:
        start = (datetime.strptime(target, "%Y%m%d")
                 - timedelta(days=365)).strftime("%Y-%m-%d")
    end = datetime.strptime(target, "%Y%m%d").strftime("%Y-%m-%d")

    rows = fetch_one(code, start, end)
    if not rows:
        return code, 0, "无返回"
    new = pd.DataFrame(rows, columns=COLS)
    if dry:
        return code, len(new), "试算"

    if os.path.exists(p):
        old = pd.read_parquet(p)
        merged = pd.concat([old, new], ignore_index=True)
    else:
        merged = new
    merged = (merged.drop_duplicates(subset=["code", "date"])
                    .sort_values(["code", "date"]).reset_index(drop=True))
    added = len(merged) - (len(old) if os.path.exists(p) else 0)

    tmp = p + ".tmp"
    merged.to_parquet(tmp, index=False)
    os.replace(tmp, p)                      # 原子替换
    return code, added, "OK"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="个股线数据往后追加")
    ap.add_argument("--out", default="data/stockbars")
    ap.add_argument("--end", default=None, help="目标日期 YYYYMMDD（默认最近已收盘交易日）")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--sleep", type=float, default=0.02)
    ap.add_argument("--overlap", type=int, default=5, help="从已有最新日期往前几天开始抓")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写盘")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（调试用）")
    args = ap.parse_args(argv)

    part_dir = os.path.join(args.out, "_bfq_parts")
    if not os.path.isdir(part_dir):
        print(f"❌ 找不到分页目录 {part_dir}，请先跑 fetch_stock_bfq.py fetch")
        return 1

    uni = os.path.join(args.out, "universe_all.csv")
    if not os.path.exists(uni):
        uni = os.path.join(args.out, "universe.csv")
    codes = sorted(pd.read_csv(uni, dtype=str).code.tolist())
    if args.limit:
        codes = codes[:args.limit]

    if args.end:
        target = args.end
    else:
        try:
            from dataprovider.calendar import latest_closed_trading_day
            target = latest_closed_trading_day(None)
        except Exception:
            target = datetime.now().strftime("%Y%m%d")
    print(f"分页目录 {part_dir}")
    print(f"标的 {len(codes):,} 只 ｜ 目标日期 {target} ｜ 并发 {args.workers}"
          f" ｜ overlap {args.overlap} 天 ｜ {'试算（不写盘）' if args.dry_run else '正式追加'}\n")

    # 先统计待处理量（不请求网络）
    # ⚠️ **没有分页文件的代码直接跳过**：那是原抓取就失败的（多为退市股），
    # 给它们只抓最近 1 年会造出「历史只有 1 年」的畸形文件，
    # 混进 merge 后会污染回测。要补它们必须走全量 fetch。
    todo, uptodate, nopart = [], 0, []
    for c in codes:
        p = os.path.join(part_dir, f"{c}.parquet")
        if not os.path.exists(p):
            nopart.append(c)
            continue
        have = _max_date(p)
        if have is not None and have >= target:
            uptodate += 1
        else:
            todo.append(c)
    print(f"已最新 {uptodate:,} 只 ｜ 待追加 {len(todo):,} 只"
          f" ｜ 无分页文件跳过 {len(nopart):,} 只")
    if nopart:
        print(f"    （无分页文件的是原抓取就失败的，多为退市股；"
              f"如确需补齐请用 fetch_stock_bfq.py fetch --force）")
    if not todo:
        print("\n✅ 全部已是最新，无需追加。")
        return 0
    if args.dry_run:
        print(f"\n（试算模式：实际会请求 {len(todo):,} 只）")
        return 0

    ok = added_total = fail = 0
    t0 = time.time()
    fails = []

    def job(c):
        try:
            return _append_one(c, part_dir, target, args.overlap, False)
        except Exception as e:
            return c, 0, f"异常 {type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(job, c) for c in todo]
        for k, fu in enumerate(as_completed(futs), 1):
            code, added, status = fu.result()
            with _lock:
                if status == "OK":
                    ok += 1
                    added_total += added
                else:
                    fail += 1
                    fails.append((code, status))
                if k % 250 == 0 or k == len(todo):
                    print(f"  进度 {k}/{len(todo)}  成功 {ok} 失败 {fail}  "
                          f"新增 {added_total:,} 行  ({time.time()-t0:.0f}s)", flush=True)
        time.sleep(args.sleep)

    print(f"\n完成：成功 {ok:,} ｜ 失败 {fail:,} ｜ 累计新增 {added_total:,} 行")
    if fails:
        print(f"⚠️ 失败 {len(fails)} 只（重跑本命令会自动只补这些）：")
        for c, s in fails[:10]:
            print(f"    {c}: {s}")
        if len(fails) > 10:
            print(f"    …还有 {len(fails)-10} 只")
    print("\n下一步：")
    print(f"  $PY tools/fetch_stock_bfq.py merge --out {args.out}")
    print("  $PY tools/rebuild_returns.py --bfq {0}/bars_bfq.parquet "
          "--dividends data/dividends/bonus_all.parquet --tax-rate 0.10 "
          "--out {0}/bars_total_tax10.parquet".format(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
