"""抓取全市场**分红送配**记录（步骤 4「换强信号低频方向」的前置数据）。

为什么需要这个
--------------
`docs/HANDOFF_项目总结与下一步.md` 第零节：主人的高股息反例（聚宽「四进三出」，
股息率 >4% 买、<3% 卖）指出正确方向是**强信号 × 低频**，10~30 只持仓在 10 万下可行。
但**现有数据只有价格 + 成交量，没有分红/股息率** → 该步骤一直卡在这里（§五「阻塞步骤 4」）。

本脚本补上这个缺口。

数据源（实测 2026-09-15）
------------------------
东财数据中心 `datacenter-web.eastmoney.com/api/data/v1/get`，
报表 `RPT_SHAREBONUS_DET`（分红送配明细）。

⚠️ **纠正一条旧记录**：本项目此前的笔记写「东财/网易/雪球都被拦」——
那指的是**行情接口**（push2 / push2his）。数据中心是**另一个 host**，
**完全可用**（实测返回 56,973 条全市场记录）。

实测约束
--------
- `pageSize` 上限 **500**（传 1000 会回落到 500）
- 2018-01-01 起的记录数 **33,150 条** → 67 页即可拉完
- 支持按 `EX_DIVIDEND_DATE` 过滤，可用 `sortColumns` 排序

字段（关键几个）
----------------
| 字段 | 含义 |
|---|---|
| `SECURITY_CODE` / `SECURITY_NAME_ABBR` | 代码 / 名称 |
| `REPORT_DATE` | 分红所属报告期 |
| `EQUITY_RECORD_DATE` | **股权登记日** |
| `EX_DIVIDEND_DATE` | **除权除息日** ← 算股息率的关键日期 |
| `PRETAX_BONUS_RMB` | **每 10 股税前派息（元）** |
| `BONUS_RATIO` | 送股（每 10 股送几股） |
| `IT_RATIO` | 转增（每 10 股转几股） |
| `BONUS_IT_RATIO` | 送转合计 |
| `IMPL_PLAN_PROFILE` | 方案文本，如 `10转10.00派5.00元(含税,扣税后4.50元)` |
| `ASSIGN_PROGRESS` | 进度（`实施分配` = 已实施，**只保留这个**） |
| `TOTAL_SHARES` | 总股本（可用于核对） |

设计要点
--------
**增量落盘 + 断点续跑**（主人电脑有死机史）：每页单独写一个 parquet，
重跑时跳过已存在的页；最后 `merge` 汇总。

用法
----
    PY=.../python.exe
    # 1) 抓取（首次约 2~4 分钟，67 页）
    $PY tools/fetch_dividend.py fetch --start 2016-01-01 --out data/dividends
    # 2) 汇总（把分页合并成单文件）
    $PY tools/fetch_dividend.py merge --out data/dividends
    # 3) 覆盖度体检（判据：≥5 年、≥3000 只）
    $PY tools/fetch_dividend.py check --out data/dividends
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from urllib.parse import quote

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.fetch_stock_history import _get

EM_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT = "RPT_SHAREBONUS_DET"
PAGE_SIZE = 500          # 实测上限（传 1000 会回落）
SLEEP = 0.25             # 页间间隔，别把人家打急了

KEEP = [
    "SECURITY_CODE", "SECURITY_NAME_ABBR", "REPORT_DATE",
    "EQUITY_RECORD_DATE", "EX_DIVIDEND_DATE", "NOTICE_DATE",
    "PRETAX_BONUS_RMB", "BONUS_RATIO", "IT_RATIO", "BONUS_IT_RATIO",
    "IMPL_PLAN_PROFILE", "ASSIGN_PROGRESS", "TOTAL_SHARES",
]


def _page(page: int, start: str, end: str | None) -> list[dict] | None:
    """取一页；返回 None 表示这一页取失败（调用方可重试/中止）。

    ⚠️ `filter` 里的 `>=` 与单引号**必须 URL 编码**（`%3E%3D` / `%27`），
    否则请求直接失败 —— 第一次实现漏了这一步，表现为"第一页就取失败"，
    看着像接口被封，其实是自己把 URL 写坏了。
    """
    flt = f"(EX_DIVIDEND_DATE>='{start}')"
    if end:
        flt += f"(EX_DIVIDEND_DATE<='{end}')"
    url = (f"{EM_API}?reportName={REPORT}&columns=ALL&pageSize={PAGE_SIZE}"
           f"&pageNumber={page}&sortColumns=EX_DIVIDEND_DATE&sortTypes=1"
           f"&filter={quote(flt, safe='')}")
    txt = _get(url, timeout=40)
    if not txt:
        return None
    try:
        res = json.loads(txt).get("result")
    except Exception:
        return None
    if not res:
        return []                      # 无数据 = 越界，正常结束
    return res.get("data") or []


def cmd_fetch(args) -> int:
    part = os.path.join(args.out, "_parts")
    os.makedirs(part, exist_ok=True)

    # 先探一次总页数
    first = _page(1, args.start, args.end)
    if first is None:
        print("❌ 第一页就取失败，检查网络/接口"); return 1
    flt = f"(EX_DIVIDEND_DATE>='{args.start}')"
    if args.end:
        flt += f"(EX_DIVIDEND_DATE<='{args.end}')"
    url = (f"{EM_API}?reportName={REPORT}&columns=ALL&pageSize=1&pageNumber=1"
           f"&filter={quote(flt, safe='')}")
    total = None
    try:
        total = json.loads(_get(url, timeout=40) or "{}").get("result", {}).get("count")
    except Exception:
        pass
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE if total else args.max_pages
    print(f"  过滤: EX_DIVIDEND_DATE >= {args.start}"
          + (f" 且 <= {args.end}" if args.end else ""))
    print(f"  总记录 {total:,} 条 → 预计 {pages} 页（每页 {PAGE_SIZE}）\n"
          if total else f"  总页数未知，最多拉 {pages} 页\n")

    done = fail = 0
    for p in range(1, pages + 1):
        fp = os.path.join(part, f"page_{p:05d}.parquet")
        if os.path.exists(fp) and not args.force:
            done += 1
            continue
        rows = _page(p, args.start, args.end)
        if rows is None:
            fail += 1
            print(f"  ⚠️ 第 {p} 页失败（已连续失败 {fail} 次）")
            if fail >= 5:
                print("  ⛔ 连续 5 页失败，中止。重跑本命令会自动续跑已完成的页。")
                break
            time.sleep(1.5)
            continue
        if not rows:
            print(f"  第 {p} 页空 → 提前结束（共 {p-1} 页）")
            break
        fail = 0
        df = pd.DataFrame(rows)
        df = df[[c for c in KEEP if c in df.columns]]
        df.to_parquet(fp, index=False)          # 逐页落盘 = 断点续跑
        done += 1
        if p % 10 == 0 or p <= 3:
            print(f"  ...第 {p}/{pages} 页，累计 {done} 页")
        time.sleep(SLEEP)

    print(f"\n  完成 {done} 页，失败 {fail} 页 → {part}")
    if fail:
        print("  ⚠️ 有失败页，重跑本命令即可续跑")
    return 0


def cmd_merge(args) -> int:
    part = os.path.join(args.out, "_parts")
    files = sorted(glob.glob(os.path.join(part, "page_*.parquet")))
    if not files:
        print("❌ 没有分页文件，先跑 fetch"); return 1
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    n0 = len(df)
    df = df.drop_duplicates(subset=["SECURITY_CODE", "EX_DIVIDEND_DATE",
                                    "REPORT_DATE"])
    # 日期统一 YYYYMMDD（与项目其它数据一致）
    for c in ("REPORT_DATE", "EQUITY_RECORD_DATE", "EX_DIVIDEND_DATE",
              "NOTICE_DATE"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce").dt.strftime("%Y%m%d")
    # 数值列
    for c in ("PRETAX_BONUS_RMB", "BONUS_RATIO", "IT_RATIO", "BONUS_IT_RATIO",
              "TOTAL_SHARES"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["code"] = (df.SECURITY_CODE.str.zfill(6)
                  .map(lambda c: ("SH" if c[0] in "69" else "SZ") + c))
    df = df.sort_values(["code", "EX_DIVIDEND_DATE"]).reset_index(drop=True)

    out = os.path.join(args.out, "bonus_all.parquet")
    df.to_parquet(out, index=False)
    print(f"  合并 {len(files)} 页：{n0:,} → 去重后 {len(df):,} 条")
    print(f"  股票 {df.code.nunique():,} 只 ｜ "
          f"除权日 {df.EX_DIVIDEND_DATE.min()} ~ {df.EX_DIVIDEND_DATE.max()}")
    print(f"  已写出 {out}")
    return 0


def cmd_check(args) -> int:
    path = os.path.join(args.out, "bonus_all.parquet")
    if not os.path.exists(path):
        print("❌ 找不到 bonus_all.parquet，先跑 fetch + merge"); return 1
    df = pd.read_parquet(path)
    df["year"] = df.EX_DIVIDEND_DATE.str[:4]

    print("=" * 74)
    print("  分红数据覆盖度体检")
    print("=" * 74)
    print(f"  记录 {len(df):,} 条 ｜ 股票 {df.code.nunique():,} 只 ｜ "
          f"跨度 {df.year.min()} ~ {df.year.max()}\n")

    g = df.groupby("year").agg(记录数=("code", "size"), 股票数=("code", "nunique"))
    print(g.to_string())

    n_year = df.year.nunique()
    n_code = df.code.nunique()
    years = sorted(df.year.unique())
    ok_year = n_year >= 5
    ok_code = n_code >= 3000
    print(f"\n  判据 ① 跨度 ≥5 年：{n_year} 年 → {'✅ 通过' if ok_year else '❌ 不足'}")
    print(f"  判据 ② 股票数 ≥3000：{n_code:,} 只 → {'✅ 通过' if ok_code else '❌ 不足'}")

    # 现金分红 vs 送转
    cash = df.PRETAX_BONUS_RMB.fillna(0) > 0
    tr = df.BONUS_IT_RATIO.fillna(0) > 0
    print(f"\n  纯现金分红 {int((cash & ~tr).sum()):,} 条 ｜ "
          f"含送转 {int(tr.sum()):,} 条 ｜ "
          f"两者都有 {int((cash & tr).sum()):,} 条")
    prog = df.ASSIGN_PROGRESS.value_counts()
    print(f"  进度分布：{dict(prog.head(6))}")

    # 每年至少分一次红的股票数（高股息策略的候选池大小）
    per = df[cash].groupby("code").year.nunique()
    print(f"\n  现金分红年数 ≥5 的股票：{int((per >= 5).sum()):,} 只"
          f"（高股息策略的天然候选池）")
    print(f"  现金分红年数 ≥3 的股票：{int((per >= 3).sum()):,} 只")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="全市场分红送配数据")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn in (("fetch", cmd_fetch), ("merge", cmd_merge),
                     ("check", cmd_check)):
        p = sub.add_parser(name)
        p.add_argument("--out", default="data/dividends")
        if name == "fetch":
            p.add_argument("--start", default="2016-01-01")
            p.add_argument("--end", default=None)
            p.add_argument("--max-pages", type=int, default=200)
            p.add_argument("--force", action="store_true",
                           help="重抓已存在的页")
        p.set_defaults(func=fn)

    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
