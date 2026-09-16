"""抓指数历史日线（分页），存 `data/indexes/`。

为什么需要
----------
个股模型要判断"什么时候该用它、什么时候不该用"，就需要**大盘/风格指数**做对照。
`bars_all.parquet` 只有个股，没有指数。

分页要点（与个股抓取同一个坑）
-------------------------------
腾讯 fqkline 接口单次上限 **cnt ≤ 800**，且 **cnt 受限时 `beg` 被忽略、`end` 生效**。
所以拿长历史必须**按 `end` 向后翻页**：每页取完，把下一页的 `end` 设为本页最早日期 − 1 天。

用法
----
    $PY tools/fetch_index_history.py --out data/indexes
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

import pandas as pd

HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
BASE = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get"
        "?param={sym},day,,{end},800,qfq")

# 关注的指数：大盘 / 中盘 / 小盘 / 小微盘
INDEXES = [
    ("sh000300", "沪深300", "大盘"),
    ("sh000905", "中证500", "中盘"),
    ("sh000852", "中证1000", "小盘"),
    ("sz399303", "国证2000", "小微盘"),
]


def fetch_page(sym: str, end: str) -> list:
    url = BASE.format(sym=sym, end=end)
    with urllib.request.urlopen(
            urllib.request.Request(url, headers=HEADERS), timeout=25) as r:
        j = json.loads(r.read().decode())
    d = j.get("data", {}).get(sym, {}) or {}
    return d.get("qfqday") or d.get("day") or []


def fetch_index(sym: str, start: str = "2018-01-01",
                end: str = "2026-09-12") -> pd.DataFrame:
    """按 end 向后翻页，直到覆盖 start。"""
    out, cur_end = [], end
    for _ in range(10):
        arr = fetch_page(sym, cur_end)
        if not arr:
            break
        out = arr + out
        earliest = min(r[0] for r in arr)
        if earliest <= start:
            break
        nxt = pd.Timestamp(earliest) - pd.Timedelta(days=1)
        cur_end = nxt.strftime("%Y-%m-%d")
        time.sleep(0.25)
    if not out:
        return pd.DataFrame()
    df = pd.DataFrame([r[:6] for r in out],
                      columns=["date", "open", "close", "high", "low", "volume"])
    df["date"] = df.date.astype(str).str.replace("-", "", regex=False)
    df = df.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
    for c in ("open", "close", "high", "low", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/indexes")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--end", default="2026-09-12")
    args = ap.parse_args(argv)

    os.makedirs(args.out, exist_ok=True)
    frames = {}
    for sym, name, tier in INDEXES:
        df = fetch_index(sym, args.start, args.end)
        if not len(df):
            print(f"  {name}（{sym}）抓取失败", flush=True)
            continue
        df["code"] = sym.upper()
        df["name"] = name
        path = os.path.join(args.out, f"{sym}.parquet")
        df.to_parquet(path, index=False, compression="zstd")
        print(f"  {name:<8}（{tier}） {len(df):>5} 行  "
              f"{df.date.min()} ~ {df.date.max()}  -> {path}", flush=True)
        frames[sym] = df

    if frames:
        allf = pd.concat(frames.values(), ignore_index=True)
        allp = os.path.join(args.out, "indexes_all.parquet")
        allf.to_parquet(allp, index=False, compression="zstd")
        print(f"\n合并写出 {allp}：{len(allf):,} 行 / {allf.code.nunique()} 个指数")
    return 0


if __name__ == "__main__":
    sys.exit(main())
