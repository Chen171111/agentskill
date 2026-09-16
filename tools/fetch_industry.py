"""抓取东财行业分类 —— 个股线「行业中性化」的前置数据。

为什么需要它
------------
`docs/HANDOFF_项目总结与下一步.md` §八 第一步：高股息组合天然偏银行/煤炭/公用事业，
2023-2024 中特估行情可能让「股息率超额」其实是**行业 beta**。
要验证这一点，必须有**行业分类**。

数据源（实测可用，2026-09-15 探通）
-----------------------------------
```
https://datacenter-web.eastmoney.com/api/data/v1/get
  ?reportName=RPT_F10_BASIC_ORGINFO
  &columns=SECURITY_CODE,SECURITY_NAME_ABBR,EM2016,BOARD_NAME_LEVEL,TRADE_MARKETT,INDUSTRYCSRC1
  &pageSize=500&pageNumber=1&sortColumns=SECURITY_CODE&sortTypes=1
```

| 字段 | 含义 | 示例 |
|---|---|---|
| `EM2016` | **东财行业三级分类**（用 `-` 分隔，第一段是**一级行业**） | `金融-银行-股份制与城商行` |
| `BOARD_NAME_LEVEL` | 板块名 | `银行-银行Ⅱ-股份制银行Ⅲ` |
| `TRADE_MARKETT` | 上市板 | `深交所主板` |
| `INDUSTRYCSRC1` | 证监会行业 | `金融业-货币金融服务` |

实测规模：**24,774 条 / 50 页**（含已退市，如 `000003 PT金田A`）。

⚠️ 两个坑
---------
1. **别用 `push2.eastmoney.com/api/qt/clist/get`（`f100` 也是行业）** ——
   首次能通、**连发即被限流**（pz 100/200/500/6000 全失败）。
   用 `datacenter-web` 那个 host。
2. **`filter` 参数里的 `>=` 与单引号必须 URL 编码**（`%3E%3D` / `%27`），
   否则第一页就失败 —— 看着像被封，其实是自己 URL 写坏了。本脚本未用 filter。

⚠️ **前视偏差（必须写进报告）**
-------------------------------
`RPT_F10_BASIC_ORGINFO` 返回的是**当前**行业分类，不是 point-in-time 历史。
行业分类变更频率低（多数公司十年不变），但：
- 次新股上市前的行业标签**不该存在**；
- 借壳/重组后行业跳变的公司会被用**新**行业回填历史。

本项目用途是「判断超额是不是行业 beta」，属于**粗粒度归因**，
这个偏差影响有限；但**不能**拿它去做需要精确 point-in-time 的因子构造。

用法
----
    PY=.../python.exe
    $PY tools/fetch_industry.py --out data/industry/industry_all.parquet
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

import pandas as pd

BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT = "RPT_F10_BASIC_ORGINFO"
COLS = ("SECURITY_CODE,SECURITY_NAME_ABBR,EM2016,BOARD_NAME_LEVEL,"
        "TRADE_MARKETT,INDUSTRYCSRC1")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0 Safari/537.36"),
    "Referer": "https://data.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}
PAGE_SIZE = 500


def fetch_page(page: int, page_size: int = PAGE_SIZE, retries: int = 4) -> dict:
    """取一页，失败重试（东财偶发 502 / 空 result）。"""
    q = {"reportName": REPORT, "columns": COLS, "pageSize": str(page_size),
         "pageNumber": str(page), "sortColumns": "SECURITY_CODE", "sortTypes": "1"}
    url = BASE + "?" + urllib.parse.urlencode(q)
    last = None
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                j = json.loads(r.read().decode())
            if j.get("success") and j.get("result"):
                return j["result"]
            last = f"success={j.get('success')} msg={j.get('message')}"
        except Exception as e:                      # noqa: BLE001
            last = repr(e)
        time.sleep(1.0 + k * 1.5)
    raise RuntimeError(f"第 {page} 页取数失败：{last}")


def _to_full(code6: str) -> str:
    """6 位代码 → 本项目口径（`SH600000` / `SZ300750` / `BJ430047`）。"""
    c = str(code6).zfill(6)
    if c[0] == "6":
        return "SH" + c
    if c[0] in ("0", "2", "3"):
        return "SZ" + c
    if c[0] in ("4", "8"):
        return "BJ" + c
    if c[0] == "9":
        return "SH" + c          # B 股（沪）
    return "SZ" + c


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="抓取东财行业分类")
    ap.add_argument("--out", default="data/industry/industry_all.parquet")
    ap.add_argument("--universe", default="data/stockbars/universe_all.csv",
                    help="用于报告覆盖率（可选）")
    ap.add_argument("--page-size", type=int, default=PAGE_SIZE)
    ap.add_argument("--sleep", type=float, default=0.35,
                    help="每页间隔秒数（东财对高频连发不友好）")
    args = ap.parse_args(argv)

    parts_dir = os.path.join(os.path.dirname(args.out) or ".", "_parts")
    os.makedirs(parts_dir, exist_ok=True)

    print("=" * 88)
    print("  抓取东财行业分类（RPT_F10_BASIC_ORGINFO）")
    print("=" * 88)

    first = fetch_page(1, args.page_size)
    total = int(first.get("count") or 0)
    pages = int(first.get("pages") or 0)
    print(f"  总记录 {total:,}  共 {pages} 页（pageSize={args.page_size}）")

    frames = []
    for p in range(1, pages + 1):
        fp = os.path.join(parts_dir, f"page_{p:04d}.parquet")
        if os.path.exists(fp):
            frames.append(pd.read_parquet(fp))
            continue
        res = first if p == 1 else fetch_page(p, args.page_size)
        df = pd.DataFrame(res.get("data") or [])
        if df.empty:
            print(f"  ⚠️ 第 {p} 页为空，跳过")
            continue
        df.to_parquet(fp, index=False)
        frames.append(df)
        if p % 10 == 0 or p == pages:
            print(f"  ... 已取 {p}/{pages} 页", flush=True)
        if p < pages:
            time.sleep(args.sleep)

    if not frames:
        print("  ❌ 未取到任何数据")
        return 2

    d = pd.concat(frames, ignore_index=True)
    d = d.drop_duplicates(subset=["SECURITY_CODE"], keep="first")
    d = d.rename(columns={"SECURITY_CODE": "code",
                          "SECURITY_NAME_ABBR": "name",
                          "EM2016": "em2016",
                          "BOARD_NAME_LEVEL": "board",
                          "TRADE_MARKETT": "market",
                          "INDUSTRYCSRC1": "csrc"})
    d["code"] = d.code.astype(str).str.zfill(6)
    # ⚠️ 东财返回的是 6 位纯数字代码（`600000`），而本项目日线里的 code 带交易所前缀
    # （`SH600000` / `SZ300750`）。必须生成 `code_full` 才能与 bars / universe 对上，
    # 否则覆盖率会是 0.00%（本次已踩：首版没做前缀，对照显示 0/5570）。
    d["code_full"] = [_to_full(c) for c in d.code]
    # 东财三级行业用 '-' 分隔：第一段 = 一级行业（用于中性化的粒度）
    d["ind_l1"] = d.em2016.fillna("").str.split("-").str[0].str.strip()
    d["ind_l2"] = d.em2016.fillna("").str.split("-").str[1].fillna("").str.strip()
    d.loc[d.ind_l1 == "", "ind_l1"] = "未分类"
    d = d[["code", "code_full", "name", "em2016", "ind_l1", "ind_l2",
           "board", "market", "csrc"]]

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    d.to_parquet(args.out, index=False)

    print("\n" + "=" * 88)
    print(f"  ✅ 写出 {args.out}   {len(d):,} 行 / {d.code.nunique():,} 只")
    print("=" * 88)
    vc = d.ind_l1.value_counts()
    print(f"  一级行业 {len(vc)} 个：")
    for k, v in vc.items():
        print(f"    {k:<12} {v:>6,} 只")

    # 覆盖率体检（与个股日线股票池对照）
    if args.universe and os.path.exists(args.universe):
        u = pd.read_csv(args.universe, dtype=str)
        uc = set(u.code.astype(str))
        ic = set(d.code_full)
        hit = len(uc & ic)
        print(f"\n  与 {os.path.basename(args.universe)} 对照："
              f"{hit:,}/{len(uc):,} 只命中（{hit/max(len(uc),1)*100:.2f}%）")
        miss = sorted(uc - ic)
        if miss:
            print(f"  未命中 {len(miss)} 只，示例（前 10）：{miss[:10]}")
        else:
            print("  全部命中 ✅")
        # 池内（非 ST）覆盖率才是关键 —— 未分类的股票会在中性化时被剔除
        if "is_st" in u.columns:
            live = set(u.loc[~u.is_st.astype(str).str.lower()
                             .isin(["true", "1"]), "code"])
            print(f"  其中非 ST 的 {len(live):,} 只命中 "
                  f"{len(live & ic):,}（{len(live & ic)/max(len(live),1)*100:.2f}%）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
