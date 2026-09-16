"""抓取**不复权**日线（bfq）+ 真实成交额 + 换手率，用于修正前复权口径缺陷。

为什么需要（2026-09-15 发现）
------------------------------
腾讯的 `qfq` 前复权**不是标准（乘法）复权，而是仿射变换**：

    bfq = k · qfq + m

实测（`tools/diag_qfq_adjust.py`）：
- 中国神华 SH601088 2019 年：k = 1，m = **15.57**（纯减法）
- 兖矿能源 SH600188 2021 年：k ≈ **1.955**，m ≈ 10.98（有送转）

**后果**：减法/仿射复权**不保持日收益**。`qfq` 口径的日收益被放大约 `bfq/qfq` 倍：
- 中国神华 2019-01-04：真实 **+2.15%**，qfq 口径算出 **+17.92%**（放大 8.3×）

在股票池内（`close_qfq >= 2.0`）的 6,730,339 行里：

| 放大倍数 | 占比 |
|---|---|
| 中位 | 1.017 |
| > 1.05 | **28.2%** |
| > 1.10 | **14.9%** |
| > 1.25 | **4.2%** |
| > 1.50 | 1.1% |

且 `放大倍数 = 1 + m/qfq`，**qfq 越小（跌得越惨）放大越狠** ——
恰好是反转策略要买的标的。故必须修正。

顺带解决两个旧限制
------------------
1. **真实成交额**：旧 `fqkline` 只返回 6 个字段（无成交额），
   本项目此前用 `close × volume × 100` 估算。`newfqkline` 直接给**成交额（万元）**。
2. **池子门槛用前复权价比较**：有了 bfq，`min_price` / `min_amount` 可用真实价，
   消除 `docs/个股线_数据体检.md` §五记录的"不可根治"偏差。

`newfqkline` 字段（实测）
------------------------
`[date, open, close, high, low, volume(手), {}, turnover_rate(%), amount(万元), ...]`

用法
----
    PY=.../python.exe
    $PY tools/fetch_stock_bfq.py fetch --out data/stockbars --start 2018-01-01 \
        --end 2026-09-12 --workers 8
    $PY tools/fetch_stock_bfq.py merge --out data/stockbars
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.fetch_stock_history import PAGE_CNT, _get, to_symbol

HOSTS = [
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
    "https://ifzq.gtimg.cn/appstock/app/newfqkline/get",
]
_lock = threading.Lock()
_PREFERRED = {"host": None}


def _page(sym: str, start: str, cursor: str, cnt: int) -> str | None:
    hosts = list(HOSTS)
    if _PREFERRED["host"] in hosts:
        hosts.remove(_PREFERRED["host"])
        hosts.insert(0, _PREFERRED["host"])
    for h in hosts:
        txt = _get(f"{h}?param={sym},day,{start},{cursor},{cnt},bfq", timeout=35)
        if txt:
            _PREFERRED["host"] = h
            return txt
    return None


def fetch_one(code: str, start: str, end: str, max_pages: int = 6):
    """向后分页抓一只标的的不复权日线。"""
    sym = to_symbol(code)
    skey = start.replace("-", "")
    cursor, rows, seen = end, [], set()
    for _ in range(max_pages):
        txt = _page(sym, start, cursor, PAGE_CNT)
        if not txt:
            break
        try:
            node = (json.loads(txt).get("data") or {}).get(sym) or {}
        except Exception:
            break
        arr = node.get("day") or []
        if not arr:
            break
        new = 0
        for x in arr:
            key = x[0].replace("-", "")
            if key in seen or key < skey:
                continue
            seen.add(key)
            new += 1
            rows.append((code, key,
                         float(x[1]), float(x[2]), float(x[3]), float(x[4]),
                         float(x[5]),
                         float(x[7]) if len(x) > 7 and x[7] != "" else None,
                         float(x[8]) if len(x) > 8 and x[8] != "" else None))
        earliest = min(x[0] for x in arr)
        if new == 0 or earliest.replace("-", "") <= skey:
            break
        cursor = (datetime.strptime(earliest, "%Y-%m-%d")
                  - timedelta(days=1)).strftime("%Y-%m-%d")
        time.sleep(0.03)
    return rows or None


COLS = ["code", "date", "open", "close", "high", "low", "volume",
        "turnover", "amount_wan"]


def cmd_fetch(args) -> int:
    uni = os.path.join(args.out, "universe_all.csv")
    if not os.path.exists(uni):
        uni = os.path.join(args.out, "universe.csv")
    u = pd.read_csv(uni, dtype=str)
    codes = sorted(u.code.tolist())
    part = os.path.join(args.out, "_bfq_parts")
    os.makedirs(part, exist_ok=True)
    print(f"  代码表 {os.path.basename(uni)} → {len(codes):,} 只")
    print(f"  区间 {args.start} ~ {args.end} ｜ 并发 {args.workers} ｜ "
          f"分页落盘 {part}\n")

    todo = [c for c in codes
            if args.force or not os.path.exists(os.path.join(part, f"{c}.parquet"))]
    print(f"  待抓 {len(todo):,} 只（已完成 {len(codes)-len(todo):,} 只，断点续跑）\n")

    ok = fail = 0
    t0 = time.time()

    def job(c):
        rows = fetch_one(c, args.start, args.end)
        if rows:
            pd.DataFrame(rows, columns=COLS).to_parquet(
                os.path.join(part, f"{c}.parquet"), index=False)
        return c, bool(rows)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(job, c) for c in todo]
        for k, fu in enumerate(as_completed(futs), 1):
            try:
                _, good = fu.result()
            except Exception:
                good = False
            with _lock:
                ok += good
                fail += (not good)
                if k % 250 == 0 or k == len(todo):
                    print(f"  进度 {k}/{len(todo)}  成功 {ok} 失败 {fail}  "
                          f"({time.time()-t0:.0f}s)", flush=True)
        time.sleep(args.sleep)

    print(f"\n  完成：成功 {ok:,} 失败 {fail:,} → {part}")
    if fail:
        print("  ⚠️ 有失败，重跑本命令会跳过已成功的，只补失败项")
    return 0


def cmd_merge(args) -> int:
    part = os.path.join(args.out, "_bfq_parts")
    files = sorted(glob.glob(os.path.join(part, "*.parquet")))
    if not files:
        print("❌ 没有分页文件，先跑 fetch"); return 1
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df = df.drop_duplicates(subset=["code", "date"]).sort_values(
        ["code", "date"]).reset_index(drop=True)
    out = os.path.join(args.out, "bars_bfq.parquet")
    df.to_parquet(out, index=False)
    print(f"  合并 {len(files):,} 只 → {len(df):,} 行 / {df.code.nunique():,} 只")
    print(f"  区间 {df.date.min()} ~ {df.date.max()}")
    print(f"  成交额非空 {df.amount_wan.notna().mean()*100:.2f}% ｜ "
          f"换手率非空 {df.turnover.notna().mean()*100:.2f}%")
    print(f"  已写出 {out}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="不复权日线 + 成交额")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("fetch", cmd_fetch), ("merge", cmd_merge)):
        p = sub.add_parser(name)
        p.add_argument("--out", default="data/stockbars")
        if name == "fetch":
            p.add_argument("--start", default="2018-01-01")
            p.add_argument("--end", default="2026-09-12")
            p.add_argument("--workers", type=int, default=8)
            p.add_argument("--sleep", type=float, default=0.02)
            p.add_argument("--force", action="store_true")
        p.set_defaults(func=fn)
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
