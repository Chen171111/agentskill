"""抓取全市场个股长历史日线 + 股票名称，供个股多因子模型使用。

数据源（实测可用性见 docs；`web.` 前缀与 `ifzq.gtimg.cn` 会被代理拦成 501）
--------------------------------------------------------------------------
- 日线：`https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get`
        `https://ifzq.gtimg.cn/appstock/app/fqkline/get`（备用，易限流）
        `?param=<sh600000>,day,<BEG>,<END>,<CNT>,qfq`
- 名称：`https://qt.gtimg.cn/q=sh600000,sz000001,...`（GBK，一次可带上百只）

关键实测约束
------------
1. **单次 cnt ≤ 800**（超过 800 会回落到 640），返回区间内**最近的 cnt 根**；
2. **`end` 参数生效** → 可向后分页：end=2026-09-12 取一页后，
   下一页把 end 设为「上一页最早日期 − 1 天」，循环直到覆盖目标起点；
3. `beg` 参数在 cnt 受限时会被忽略，故分页以 `end` 为准。

用法
----
    PY=.../python.exe
    # 1) 探测全市场代码表（含名称，用于剔除 ST/退市）
    $PY tools/fetch_stock_history.py universe --out data/stockbars
    # 2) 抓长历史日线（默认 2018-01-01 至今，800 根/页向后翻）
    $PY tools/fetch_stock_history.py bars --out data/stockbars \
        --start 2018-01-01 --end 2026-09-12 --workers 8
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

HOSTS = [
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
]
QT_URL = "https://qt.gtimg.cn/q="
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://gu.qq.com/",
}
PAGE_CNT = 800          # 实测上限

_lock = threading.Lock()
_PREFERRED = {"host": None}      # 健康主机缓存（避免每页都去撞被封的 host）


# ---------------------------------------------------------------- 通用请求
def _get(url: str, timeout: int = 30, gbk: bool = False) -> str | None:
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            return raw.decode("gbk", "replace") if gbk else raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (501, 502, 403):     # 被代理/服务端拦，重试无意义
                return None
            if i == 2:
                return None
            time.sleep(0.5 * (i + 1))
        except Exception:
            if i == 2:
                return None
            time.sleep(0.5 * (i + 1))
    return None


def _fetch_page(sym: str, start: str, cursor: str, cnt: int) -> str | None:
    """取一页；优先用上次成功的主机，失败再试其他。"""
    hosts = list(HOSTS)
    if _PREFERRED["host"] in hosts:
        hosts.remove(_PREFERRED["host"])
        hosts.insert(0, _PREFERRED["host"])
    for h in hosts:
        txt = _get(f"{h}?param={sym},day,{start},{cursor},{cnt},qfq", timeout=35)
        if txt:
            _PREFERRED["host"] = h
            return txt
    return None


def to_symbol(code: str) -> str:
    """SH600000 / 600000.SH / sh600000 -> sh600000。"""
    c = code.upper().replace(".SH", "").replace(".SZ", "")
    if c.startswith(("SH", "SZ")):
        return c[:2].lower() + c[2:]
    return ("sh" if c.startswith(("6", "9", "68")) else "sz") + c


def to_code(sym: str) -> str:
    """sh600000 -> SH600000。"""
    return sym[:2].upper() + sym[2:]


# ---------------------------------------------------------------- 1. 代码表
def _candidate_codes() -> list[str]:
    """枚举可能的 A 股代码段（沪：600/601/603/605/688/689；深：000/001/002/003/300/301）。"""
    out = []
    for pfx in ("600", "601", "603", "605"):
        out += [f"{pfx}{i:03d}" for i in range(1000)]
    for pfx in ("688", "689"):
        out += [f"{pfx}{i:03d}" for i in range(1000)]
    for pfx in ("000", "001", "002", "003"):
        out += [f"{pfx}{i:03d}" for i in range(1000)]
    for pfx in ("300", "301"):
        out += [f"{pfx}{i:03d}" for i in range(1000)]
    return out


def _qt_batch(syms: list[str]) -> dict:
    """批量查名称，返回 {SH600000: 名称}。"""
    txt = _get(QT_URL + ",".join(syms), gbk=True)
    out = {}
    if not txt:
        return out
    for line in txt.strip().split("\n"):
        if '="' not in line:
            continue
        head, body = line.split('="', 1)
        sym = head.replace("v_", "").strip()
        fields = body.split("~")
        if len(fields) < 3:
            continue
        name = fields[1].strip()
        num = fields[2].strip()
        if not name or not num:
            continue
        out[to_code(sym)] = name
    return out


def cmd_universe(args) -> int:
    cands = _candidate_codes()
    print(f"待探测候选代码 {len(cands):,} 个")
    # 按交易所分组批量探测（qt 接口一次可带很多，保守用 120）
    batches, step = [], 120
    for i in range(0, len(cands), step):
        chunk = cands[i:i + step]
        syms = [to_symbol(c) for c in chunk]
        batches.append(syms)

    found: dict[str, str] = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(_qt_batch, b) for b in batches]
        for k, fu in enumerate(as_completed(futs), 1):
            found.update(fu.result() or {})
            if k % 20 == 0:
                print(f"  {k}/{len(batches)} 批  已发现 {len(found):,} 只  "
                      f"({time.time()-t0:.0f}s)", flush=True)

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "universe.csv")
    import pandas as pd
    df = pd.DataFrame(sorted(found.items()), columns=["code", "name"])
    # 名称里带 ST / 退 / * 的单独标记
    df["is_st"] = df.name.str.contains("ST|退", regex=True, na=False)
    df["board"] = df.code.str[2:5].map(
        lambda p: "科创板" if p.startswith("68") else
        "创业板" if p.startswith("30") else
        "主板")
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"\n写出 {path}: {len(df):,} 只")
    print(f"  ST/退市标记: {int(df.is_st.sum()):,} 只")
    print(df.board.value_counts().to_string())
    return 0


# ---------------------------------------------------------------- 2. 日线
def fetch_one(code: str, start: str, end: str, max_pages: int = 6,
              have_until: str | None = None):
    """向后分页抓取一只标的的日线。

    have_until: 已有数据的**最早日期**（YYYY-MM-DD）。给了就从它前一天开始往前抓，
                实现断点续抓，避免重复拉最近一页。
    返回 list[(code, date, open, close, high, low, volume)]。
    """
    sym = to_symbol(code)
    start_key = start.replace("-", "")          # 比较用无横线格式
    cursor = end
    if have_until:
        cursor = (datetime.strptime(have_until, "%Y-%m-%d")
                  - timedelta(days=1)).strftime("%Y-%m-%d")
    rows: list[tuple] = []
    seen = set()
    for page in range(max_pages):
        txt = _fetch_page(sym, start, cursor, PAGE_CNT)
        if not txt:
            break
        try:
            j = json.loads(txt)
        except Exception:
            break
        d = j.get("data")
        if not isinstance(d, dict):
            break
        node = d.get(sym) or {}
        arr = node.get("qfqday") or node.get("day") or []
        if not arr:
            break
        new = 0
        for x in arr:
            key = x[0].replace("-", "")          # 统一为 YYYYMMDD，避免与 base 混格式
            if key in seen or key < start_key:
                continue
            seen.add(key)
            new += 1
            rows.append((code, key, float(x[1]), float(x[2]), float(x[3]),
                         float(x[4]), float(x[5])))
        earliest = min(x[0] for x in arr)
        if new == 0 or earliest.replace("-", "") <= start_key:
            break
        cursor = (datetime.strptime(earliest, "%Y-%m-%d")
                  - timedelta(days=1)).strftime("%Y-%m-%d")
        time.sleep(0.03)
    return rows or None


def cmd_bars(args) -> int:
    import pandas as pd

    uni = os.path.join(args.out, "universe.csv")
    if os.path.exists(uni):
        u = pd.read_csv(uni, dtype=str)
        n_all = len(u)
        if not args.include_st:
            u = u[~u.is_st.astype(str).str.lower().isin(["true", "1"])]
        codes = sorted(u.code.tolist())
        if args.include_st:
            print(f"代码表: {uni} -> {len(codes):,} 只（**含 ST/退市**）")
        else:
            print(f"代码表: {uni} -> {len(codes):,} 只（已剔除 ST/退市，"
                  f"共 {n_all:,} 只）")
    else:
        raise SystemExit(f"未找到 {uni}，请先运行 `universe` 子命令")

    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, "bars.parquet")

    # ---- 断点续抓：复用已有数据，只补抓更老的部分 ----
    base = None
    have_until: dict[str, str] = {}
    if os.path.exists(out_path) and not args.force:
        base = pd.read_parquet(out_path)
        base["date"] = base.date.astype(str).str.replace("-", "", regex=False)
        for c, g in base.groupby("code", sort=False):
            have_until[c] = min(g.date)
        print(f"断点续抓：已有 {len(base):,} 行 / {base.code.nunique()} 只，"
              f"最早 {min(have_until.values())}")

    print(f"抓取区间 {args.start} ~ {args.end}，每页 {PAGE_CNT} 根，并发 {args.workers}")

    ok = fail = 0
    frames: list = []
    t0 = time.time()

    def job(c):
        nonlocal ok, fail
        hu = have_until.get(c)
        hu_fmt = (f"{hu[:4]}-{hu[4:6]}-{hu[6:]}"
                  if hu and len(hu) == 8 else None)
        if hu_fmt and hu_fmt <= args.start:
            with _lock:                       # 已覆盖到起点，无需再抓
                ok += 1
            return None
        rows = fetch_one(c, args.start, args.end, have_until=hu_fmt)
        with _lock:
            if rows:
                ok += 1
            else:
                fail += 1
            n = ok + fail
            if n % 250 == 0:
                print(f"  进度 {n}/{len(codes)}  成功 {ok} 失败 {fail}  "
                      f"({time.time()-t0:.0f}s)", flush=True)
        time.sleep(args.sleep)
        return rows

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(job, c) for c in codes]
        for k, fu in enumerate(as_completed(futs), 1):
            rows = fu.result()
            if rows:
                frames.append(pd.DataFrame(
                    rows, columns=["code", "date", "open", "close", "high",
                                   "low", "volume"]))
            if k % 500 == 0:
                parts = ([base] if base is not None else []) + frames
                if parts:
                    pd.concat(parts, ignore_index=True).drop_duplicates(
                        subset=["code", "date"]).to_parquet(
                        out_path + ".partial", index=False, compression="zstd")
                    print(f"  [增量落盘] {out_path}.partial ({k}/{len(codes)})",
                          flush=True)

    parts = ([base] if base is not None else []) + frames
    if not parts:
        print("全部失败")
        return 1
    df = pd.concat(parts, ignore_index=True).drop_duplicates(subset=["code", "date"])
    df.to_parquet(out_path, index=False, compression="zstd")
    print(f"\n写出 {out_path}: {len(df):,} 行, {df.code.nunique()} 只, "
          f"{df.date.nunique()} 个交易日 ({df.date.min()} ~ {df.date.max()}), "
          f"用时 {time.time()-t0:.0f}s")
    # 覆盖度体检：每只行数分位
    n = df.groupby("code").size()
    print("每只行数分位:", {q: int(n.quantile(q)) for q in (.1, .5, .9)})
    return 0


def cmd_delisted(args) -> int:
    """补齐**退市股**行情，修正生存者偏差。

    思路：把代码空间里**当前没有上市**的候选代码全部探一遍。
    `qt.gtimg.cn` 只返回当前存续标的，所以「候选 − 存续」里既包含退市股、
    也包含从未使用过的空号。实测腾讯 fqkline 接口**仍能返回退市股的历史**
    （如 600401 退市海润止于 2019-07-08、300104 乐视网止于 2020-07-20），
    故逐个抓取，能取到数据的即为退市股。
    """
    import pandas as pd

    uni = os.path.join(args.out, "universe.csv")
    if not os.path.exists(uni):
        raise SystemExit(f"未找到 {uni}，请先运行 `universe` 子命令")
    udf = pd.read_csv(uni, dtype=str)
    listed = set(udf.code)

    if args.from_st:
        # 已退市股**仍在腾讯名称接口里**（实测 SH600401 退市海润、SZ300104 乐视网
        # 都在 universe.csv 中），只是建主数据时被 ST 过滤挡掉了。
        # 所以正确的候选源是 ST/退市名单，而不是去探测几千个空号。
        flag = udf.is_st.astype(str).str.lower().isin(["true", "1"])
        probe = sorted(set(udf.loc[flag, "code"]))
        print(f"ST/退市名单 {len(probe):,} 个（代码空间探测对已退市股无效，"
              f"它们本就在 universe 里）")
    else:
        cands = [to_code(s) for s in (to_symbol(c) for c in _candidate_codes())]
        probe = sorted(set(cands) - listed)
        print(f"代码空间 {len(set(cands)):,} 个，当前存续 {len(listed):,} 个，"
              f"待探测 {len(probe):,} 个")

    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, "bars_delisted.parquet")
    base = None
    have_until: dict[str, str] = {}
    if os.path.exists(out_path) and not args.force:
        base = pd.read_parquet(out_path)
        base["date"] = base.date.astype(str).str.replace("-", "", regex=False)
        for c, g in base.groupby("code", sort=False):
            have_until[c] = min(g.date)
        print(f"断点续抓：已有退市股 {len(base):,} 行 / {base.code.nunique()} 只")

    ok = fail = 0
    frames: list = []
    t0 = time.time()

    def job(c):
        nonlocal ok, fail
        hu = have_until.get(c)
        hu_fmt = (f"{hu[:4]}-{hu[4:6]}-{hu[6:]}" if hu and len(hu) == 8 else None)
        rows = fetch_one(c, args.start, args.end, max_pages=4, have_until=hu_fmt)
        with _lock:
            if rows:
                ok += 1
            else:
                fail += 1
            n = ok + fail
            if n % 500 == 0:
                print(f"  进度 {n}/{len(probe)}  命中退市股 {ok}  "
                      f"({time.time()-t0:.0f}s)", flush=True)
        time.sleep(args.sleep)
        return rows

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(job, c) for c in probe]
        for k, fu in enumerate(as_completed(futs), 1):
            rows = fu.result()
            if rows:
                frames.append(pd.DataFrame(
                    rows, columns=["code", "date", "open", "close", "high",
                                   "low", "volume"]))
            if k % 200 == 0:
                parts = ([base] if base is not None else []) + frames
                if parts:
                    merged = pd.concat(parts, ignore_index=True).drop_duplicates(
                        subset=["code", "date"])
                    merged.to_parquet(out_path + ".partial", index=False,
                                      compression="zstd")
                    # 同时刷新正式文件：本机有死机史，这样中断后可直接断点续抓，
                    # 不必手工把 .partial 改名
                    merged.to_parquet(out_path, index=False, compression="zstd")
                    print(f"  [增量落盘] ({k}/{len(probe)})  累计 "
                          f"{merged.code.nunique()} 只", flush=True)

    parts = ([base] if base is not None else []) + frames
    if not parts:
        print("未发现任何退市股数据")
        return 1
    df = pd.concat(parts, ignore_index=True).drop_duplicates(subset=["code", "date"])
    df.to_parquet(out_path, index=False, compression="zstd")
    print(f"\n写出 {out_path}: {len(df):,} 行, {df.code.nunique()} 只退市股, "
          f"日期 {df.date.min()} ~ {df.date.max()}, 用时 {time.time()-t0:.0f}s")
    # 退市年份分布（用于核对：应与实际退市潮吻合）
    last = df.groupby("code").date.max().str[:4].value_counts().sort_index()
    print("按「最后交易日年份」分布:"); print(last.to_string())
    return 0


def cmd_merge(args) -> int:
    """把退市股并进主数据集，产出 `bars_all.parquet` + `universe_all.csv`。

    ⚠️ 关键（踩过的坑）：退市股**就在 `universe.csv` 的 ST 名单里** —— 实测
    SH600401（退市海润）、SZ300104（乐视网）都在。所以直接合并的话，
    回测的 `--universe` ST 过滤会把它们**全部剔掉**，抓了也白抓。

    故这里做两件事：
    1. **只并入已停止交易的那批**（最后交易日 < 主数据最新日）—— 那才是生存者
       偏差要修的对象；「当前 ST 但仍交易」的按策略定义继续剔除。
    2. 产出 `universe_all.csv`：把已退市股标成 `is_st=False`，
       这样它们在回测里不会被过滤掉。回测请用这个 universe。
    """
    import pandas as pd

    main = os.path.join(args.out, "bars.parquet")
    dele = os.path.join(args.out, "bars_delisted.parquet")
    out_path = os.path.join(args.out, "bars_all.parquet")
    if not os.path.exists(main):
        raise SystemExit(f"未找到 {main}")
    a = pd.read_parquet(main)
    a["date"] = a.date.astype(str).str.replace("-", "", regex=False)
    print(f"存续股: {len(a):,} 行, {a.code.nunique()} 只")
    parts = [a]
    dead_codes = set()
    if os.path.exists(dele):
        b = pd.read_parquet(dele)
        b["date"] = b.date.astype(str).str.replace("-", "", regex=False)
        print(f"退市股(原始): {len(b):,} 行, {b.code.nunique()} 只")
        latest = a.date.max()
        last = b.groupby("code").date.max()
        dead_codes = set(last[last < latest].index)
        alive = set(last[last >= latest].index)
        print(f"  其中【已停止交易】{len(dead_codes)} 只 -> 并入；"
              f"【当前 ST 仍交易】{len(alive)} 只 -> 按策略继续剔除")
        parts.append(b[b.code.isin(dead_codes)])
    else:
        print("（未找到退市股文件，仅合并存续股）")
    df = pd.concat(parts, ignore_index=True).drop_duplicates(subset=["code", "date"])
    df = df.sort_values(["code", "date"])
    df.to_parquet(out_path, index=False, compression="zstd")
    print(f"\n写出 {out_path}: {len(df):,} 行, {df.code.nunique()} 只, "
          f"{df.date.nunique()} 个交易日 ({df.date.min()} ~ {df.date.max()})")
    # 覆盖度体检
    d = df[df.code.isin(b.code.unique())] if os.path.exists(dele) else df.iloc[:0]
    if len(d):
        last = d.groupby("code").date.max().str[:4].value_counts().sort_index()
        print("\n退市股「最后交易日年份」分布（应与实际退市潮吻合）:")
        print(last.to_string())

    # 配套 universe：已退市股必须标成**非 ST**，否则回测的 ST 过滤会把它们剔掉
    uni_path = os.path.join(args.out, "universe.csv")
    if os.path.exists(uni_path):
        u = pd.read_csv(uni_path, dtype=str)
        n_dead = int(u.code.isin(dead_codes).sum())
        u.loc[u.code.isin(dead_codes), "is_st"] = "False"
        out_uni = os.path.join(args.out, "universe_all.csv")
        u.to_csv(out_uni, index=False, encoding="utf-8-sig")
        print(f"\n写出 {out_uni}: {len(u):,} 只，其中 {n_dead} 只已退市股"
              f"标记为非 ST（纳入股票池）")
        print(f"  回测请用 --bars {out_path} --universe {out_uni}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="抓取全市场个股长历史日线")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("universe", help="探测全市场代码表 + 名称")
    sp.add_argument("--out", required=True)
    sp.add_argument("--workers", type=int, default=8)
    sp.set_defaults(func=cmd_universe)

    sp = sub.add_parser("bars", help="抓取长历史日线")
    sp.add_argument("--out", required=True)
    sp.add_argument("--start", default="2018-01-01")
    sp.add_argument("--end", default="2026-09-12")
    sp.add_argument("--workers", type=int, default=8)
    sp.add_argument("--sleep", type=float, default=0.05)
    sp.add_argument("--include-st", dest="include_st", action="store_true",
                    default=False,
                    help="**含 ST/退市**（修正生存者偏差时必须打开；"
                         "名称含 ST/退 的标的一并抓取）")
    sp.add_argument("--force", action="store_true", help="忽略已有数据，全量重抓")
    sp.set_defaults(func=cmd_bars)

    sp = sub.add_parser("delisted", help="补齐退市股行情（修正生存者偏差）")
    sp.add_argument("--out", required=True)
    sp.add_argument("--start", default="2018-01-01")
    sp.add_argument("--end", default="2026-09-12")
    sp.add_argument("--workers", type=int, default=10)
    sp.add_argument("--sleep", type=float, default=0.03)
    sp.add_argument("--from-st", action="store_true",
                    help="从 universe.csv 的 ST/退市名单取候选（推荐）")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_delisted)

    sp = sub.add_parser("merge", help="合并存续股 + 退市股 -> bars_all.parquet")
    sp.add_argument("--out", required=True)
    sp.set_defaults(func=cmd_merge)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
