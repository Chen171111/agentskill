"""每日学习闭环（M4）—— 一条命令跑完「数据增量 + 特征增量 + 标签回填 + 监控」。

> 规格：`docs/需求规格_自优化闭环模块.md` §5.2。

**它不做什么（红线）**
----------------------
- ❌ 不修改任何已定稿策略规则（`N=20 / hold=60 / indpct / 过滤器`）
- ❌ 不写 `state/trading.db`、不写 `state/ALERT.txt`、不下单
- ✅ 只写 `state/learn/` 与（交给 M2 的）`state/LEARN_ALERT.txt`

为什么是这四步
--------------
"每日学习"最容易做错的地方是**每天重训模型**。但标签是 60 日前向收益 →
**每天新增的有效样本只有 1/60**，每天重训 = 用几乎相同的数据反复拟合 = 纯噪音放大。
所以每日只做：**① 补数据 ② 增量特征 ③ 标签回填 ④ 监控**；模型重训留到月度/季度（走五闸门 + 影子期）。

四步（顺序固定，逐步幂等）
--------------------------
| 步 | 动作 | 失败处理 |
|---|---|---|
| 1 | `append_stock_bars` 补最近几天行情 | 失败 → **继续**（不阻断，由 M2 的新鲜度检查兜底告警） |
| 2 | 特征面板：指纹变了才重建（`tools/panel_cache.py`） | 失败 → **中止**（后续都依赖它） |
| 3 | 标签回填：算出 `T − label_horizon` 之前样本的已实现收益并记录覆盖度 | 失败 → **中止** |
| 4 | 调 `tools/monitor_factors.py --strict` | 越界 → 保留 `LEARN_ALERT`，**透传退出码 3** |

用法
----
    $PY tools/daily_learn.py --dry       # 只打印计划
    $PY tools/daily_learn.py             # 实跑
    $PY tools/daily_learn.py --force     # 忽略当日已完成标记，全部重跑

退出码：0 正常；3 监控越界；2 参数/数据错误。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.panel_cache import build_panel, fingerprint  # noqa: E402
from tools.progress import flush_partial  # noqa: E402
from tools.test_dividend_factor import require_adj_mode  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEARN_DIR = os.path.join(ROOT, "state", "learn")
TRADING_DAYS = 244.0


def _state_path(date_tag: str) -> str:
    return os.path.join(LEARN_DIR, f"{date_tag}.json")


def _load_state(date_tag: str) -> dict:
    p = _state_path(date_tag)
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:                                     # noqa: BLE001
            return {}
    return {}


def _save_state(date_tag: str, st: dict) -> None:
    os.makedirs(LEARN_DIR, exist_ok=True)
    with open(_state_path(date_tag), "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=2, default=str)


def step1_data(args, st: dict, force: bool) -> dict:
    """① 行情增量。"""
    if st.get("step1", {}).get("status") in ("ok", "skipped") and not force:
        print("    ① 数据增量 —— 当日已完成，跳过")
        return {"status": "skipped", "note": "当日已完成"}
    cmd = [sys.executable, "-u", "tools/append_stock_bars.py",
           "--out", "data/stockbars", "--workers", str(args.workers)]
    if args.dry or args.dry_run_data:
        cmd.append("--dry-run")
    t0 = time.time()
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    tail = "\n".join((r.stdout or "").strip().splitlines()[-6:])
    print(f"    ① 数据增量 —— 退出码 {r.returncode}，用时 {time.time()-t0:.0f}s")
    if tail:
        print("       " + tail.replace("\n", "\n       "))
    return {"status": "ok" if r.returncode == 0 else "failed",
            "returncode": r.returncode, "tail": tail[-800:],
            "elapsed_sec": round(time.time() - t0, 1),
            "dry_run": bool(args.dry or args.dry_run_data)}


def step2_features(args, st: dict, force: bool) -> tuple[dict, object]:
    """② 特征增量（指纹变了才重建）。"""
    fp = fingerprint(args)
    if (st.get("step2", {}).get("fingerprint") == fp and not force
            and st.get("step2", {}).get("status") in ("ok", "skipped")):
        # ⚠️ 跳过 ≠ 不返回面板：步骤③④ 都依赖面板。
        # 第一版在这里直接 `return ..., None` → 第二次运行必然因"无面板"中止（幂等失败）。
        # 现在改为**仍然读缓存**（命中即 1 秒），保证"第二次运行全 skipped 且成功"。
        print(f"    ② 特征增量 —— 指纹未变（{fp}），跳过（仍读缓存以支撑 ③④）")
        if args.dry:
            return {"status": "skipped", "fingerprint": fp}, None
        df2, d2, bd2, hit2 = build_panel(args)
        return ({"status": "skipped", "fingerprint": fp, "rows": int(len(df2)),
                 "dates": int(len(d2)), "cache_hit": bool(hit2)},
                (df2, d2, bd2))
    if args.dry:
        print(f"    ② 特征增量 —— [dry] 将按指纹 {fp} 构建/读取面板")
        return {"status": "dry", "fingerprint": fp}, None
    t0 = time.time()
    df, dates, by_date, hit = build_panel(args)
    print(f"    ② 特征增量 —— {'缓存命中' if hit else '已重建'}｜"
          f"{len(df):,} 行｜{len(dates):,} 交易日｜用时 {time.time()-t0:.0f}s")
    return ({"status": "ok", "fingerprint": fp, "rows": int(len(df)),
             "dates": int(len(dates)), "cache_hit": bool(hit),
             "elapsed_sec": round(time.time() - t0, 1)},
            (df, dates, by_date))


def step3_labels(args, st: dict, df, dates, force: bool) -> dict:
    """③ 标签回填：算出已到期样本的前向收益覆盖度。"""
    if st.get("step3", {}).get("status") in ("ok", "skipped") and not force:
        print("    ③ 标签回填 —— 当日已完成，跳过")
        return {"status": "skipped", "note": "当日已完成"}
    if df is None:
        print("    ③ 标签回填 —— 无面板（步骤②未执行）")
        return {"status": "skipped", "note": "无面板"}
    t0 = time.time()
    g = df.groupby("code", sort=False).close
    fwd = g.shift(-args.label_horizon) / df.close - 1.0
    # 按日统计"已到期"的样本数（末 label_horizon 个交易日必然为 NaN = 未到期，属正常）
    tmp = pd.DataFrame({"date": df.date.values, "ok": fwd.notna().values})
    cov = tmp.groupby("date", sort=False).ok.agg(["sum", "size"])
    cov["ratio"] = cov["sum"] / cov["size"]
    out = os.path.join(LEARN_DIR, f"label_coverage_{args.date_tag}.csv")
    os.makedirs(LEARN_DIR, exist_ok=True)
    cov.to_csv(out, encoding="utf-8-sig")
    n_ready = int(cov["sum"].sum())
    n_total = int(cov["size"].sum())
    last_ready = cov.index[cov.ratio > 0.5]
    last_ready = str(last_ready[-1]) if len(last_ready) else None
    print(f"    ③ 标签回填 —— 已到期样本 {n_ready:,}/{n_total:,}"
          f"（{n_ready/max(n_total,1)*100:.1f}%）｜最后一个已到期交易日 {last_ready}"
          f"｜用时 {time.time()-t0:.0f}s")
    print(f"       明细 {os.path.relpath(out, ROOT)}")
    return {"status": "ok", "labeled": n_ready, "total": n_total,
            "last_labeled_date": last_ready, "coverage_file": os.path.relpath(out, ROOT),
            "elapsed_sec": round(time.time() - t0, 1)}


def step4_monitor(args, st: dict, force: bool) -> dict:
    """④ 监控（越界透传退出码 3）。"""
    if st.get("step4", {}).get("status") in ("ok", "skipped") and not force:
        print("    ④ 监控 —— 当日已完成，跳过")
        return {"status": "skipped", "note": "当日已完成"}
    if args.dry:
        print("    ④ 监控 —— [dry] 将执行 monitor_factors --strict")
        return {"status": "dry"}
    cmd = [sys.executable, "-u", "tools/monitor_factors.py",
           "--adj-mode", args.adj_mode, "--strict",
           "--bars", args.bars, "--bfq", args.bfq,
           "--dividends", args.dividends, "--universe", args.universe]
    t0 = time.time()
    log = os.path.join(ROOT, "results", f"_daily_learn_{args.date_tag}.log")
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(f"\n### {time.strftime('%Y-%m-%d %H:%M:%S')} monitor_factors --strict\n")
        fh.flush()
        r = subprocess.run(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT)
    print(f"    ④ 监控 —— 退出码 {r.returncode}"
          f"{'（越界，已写 LEARN_ALERT）' if r.returncode == 3 else ''}"
          f"，用时 {time.time()-t0:.0f}s｜日志 {os.path.relpath(log, ROOT)}")
    return {"status": "ok", "returncode": r.returncode, "breached": r.returncode == 3,
            "elapsed_sec": round(time.time() - t0, 1),
            "log": os.path.relpath(log, ROOT)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="每日学习闭环（四步）")
    ap.add_argument("--bars", default="data/stockbars/bars_total_tax10.parquet")
    ap.add_argument("--bfq", default="data/stockbars/bars_bfq.parquet")
    ap.add_argument("--dividends", default="data/dividends/bonus_all.parquet")
    ap.add_argument("--universe", default="data/stockbars/universe_all.csv")
    ap.add_argument("--price-col", default="px_real")
    ap.add_argument("--adj-mode", default=None, choices=["legacy", "correct"])
    ap.add_argument("--min-price", type=float, default=2.0)
    ap.add_argument("--min-amount", type=float, default=3e7)
    ap.add_argument("--min-listed", type=int, default=120)
    ap.add_argument("--label-horizon", type=int, default=60)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry", action="store_true", help="只打印计划，不执行")
    ap.add_argument("--dry-run-data", action="store_true", help="步骤①只统计不写盘")
    ap.add_argument("--force", action="store_true", help="忽略当日已完成标记")
    args = ap.parse_args(argv)

    args.date_tag = time.strftime("%Y%m%d")
    t0 = time.time()
    print("=" * 104)
    print(f"  每日学习闭环  ·  {time.strftime('%Y-%m-%d %H:%M:%S')}"
          f"  ·  adj_mode={require_adj_mode(args)}"
          f"{'  ·  [DRY RUN]' if args.dry else ''}")
    print("=" * 104)
    print("  红线：本脚本不改策略规则、不写 trading.db、不下单\n")

    st = _load_state(args.date_tag)
    st.setdefault("date", args.date_tag)
    st["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    st["dry"] = bool(args.dry)
    rc = 0
    step_rows: list[dict] = []

    def _flush_steps(tag: str) -> None:
        """每完成一步就落盘（增量落盘纪律；被掐断也能看出跑到第几步）。"""
        if step_rows and not args.dry:
            flush_partial(step_rows, f"results/_daily_learn_steps_{args.date_tag}.csv",
                          tag=tag)

    try:
        st["step1"] = step1_data(args, st, args.force)
        step_rows.append({"step": 1, **st["step1"]}); _flush_steps("1")
        s2, panel = step2_features(args, st, args.force)
        st["step2"] = s2
        step_rows.append({"step": 2, **s2}); _flush_steps("2")
        if panel is None and not args.dry:
            raise RuntimeError("步骤②未产出面板")
        df, dates, by_date = panel if panel else (None, None, None)
        st["step3"] = step3_labels(args, st, df, dates, args.force)
        step_rows.append({"step": 3, **st["step3"]}); _flush_steps("3")
        st["step4"] = step4_monitor(args, st, args.force)
        step_rows.append({"step": 4, **st["step4"]}); _flush_steps("4")
        if st["step4"].get("breached"):
            rc = 3
    except Exception as e:                                    # noqa: BLE001
        st["error"] = f"{type(e).__name__}: {e}"
        print(f"\n  ❌ 中止：{st['error']}")
        rc = 2
    finally:
        st["elapsed_sec"] = round(time.time() - t0, 1)
        st["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if not args.dry:
            _save_state(args.date_tag, st)

    print("\n" + "=" * 104)
    print("  步骤汇总：")
    for k in ("step1", "step2", "step3", "step4"):
        s = st.get(k, {})
        print(f"    {k}: {s.get('status', '—'):<8} {s.get('note', '')}")
    print(f"  总用时 {st['elapsed_sec']/60:.1f} min｜状态文件 "
          f"{os.path.relpath(_state_path(args.date_tag), ROOT) if not args.dry else '(dry)'}")
    print("=" * 104)
    return rc


if __name__ == "__main__":
    sys.exit(main())
