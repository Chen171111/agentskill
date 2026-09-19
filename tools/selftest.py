"""快速回归自检 —— **改动前后都跑一遍**（默认 < 60 秒，不跑长回测）。

为什么需要它
------------
本项目反复吃同一类亏，而每一次都是**事后偶然发现**的：

- `hyst` 阈值在 `entry=4%` 下从未 binding（"跑过" ≠ "测过"）
- 引擎 `topk*2` 守卫**静默跳过**调仓，伪造出"topk>700 alpha 转负"的假断崖
- 引擎成本默认万3、而调整式按万5 扣 → 所有「10 万真实」数字偏高
- 涨跌停判据 `.round(2)` 打在总收益指数价上 → **漏判 40.6%**
- ETF 份额折算被每日刷新**静默回退**（代价 0.98pp/年）
- `diag_dividend_into_mf.py` 漏了 `--adj-mode` → 口径不可复现
- `--adj-mode` 虽有 `require_adj_mode` 守卫，但 12 个脚本的 argparse 塞了 `default="correct"`
  → 守卫**永远不触发**，忘记透传时静默换口径（2026-09-18 BUG-4）

它们的共同点：**破坏是静默的，而发现靠运气**。
本脚本把"靠运气"换成"一条命令"。

检查项
------
| 组 | 内容 |
|---|---|
| 成本 | `costs.py` 是**唯一来源**；引擎默认与它自洽；无调用方覆盖成本；派生函数数值不变 |
| 口径 | `require_adj_mode` 缺参即报错（防"静默换口径"复发）；且**没有脚本给它设 argparse 默认值** |
| 口径 | **point-in-time**：`correct` 换数据截止日后历史段**差异必须恰好为 0** |
| 涨跌停 | 分档表（主板 10% / 科创 20% / 创业板 **时间分档** / 北交所 30%）；判据用比例而非绝对价 |
| 复权 | `THRESH` 四处实现值一致（且等于 `dataprovider.adjust.THRESH`） |
| 文档 | `audit_doc_commands` 四类问题全 0 |
| CLI | `argparse` 的 `help`/`description` 里没有「裸 `%`」（否则 `--help` 直接崩，且是静默的） |
| 稳定性 | 长循环脚本接入 `flush_partial` 增量落盘；**大文件缓存原子写 + 读容错**；**年化外推有最小样本守卫** |
| 数据 | 关键数据文件存在、行数/末根交易日在合理范围 |
| 数据 | 池内 `close<=0` / `amt<=0` = 0（个股线地基） |

用法
----
    $PY tools/selftest.py              # 全部（含 point-in-time，约 30~60 秒）
    $PY tools/selftest.py --quick      # 跳过需要读大 parquet 的项
    $PY tools/selftest.py --list       # 只列检查项

退出码：0 = 全过；1 = 有失败（适合挂进 CI / 提交前跑）。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import traceback

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BARS = os.path.join(ROOT, "data", "stockbars")

_CHECKS: list[tuple[str, str, bool]] = []   # (组, 名称, 是否需要重数据)


def check(group: str, name: str, heavy: bool = False):
    """注册一个检查。被装饰函数返回值会被忽略；抛异常 = 失败。"""
    def deco(fn):
        _CHECKS.append((group, f"{name}", heavy))
        fn._selftest = (group, name, heavy)
        return fn
    return deco


# ============================ 成本 ============================
@check("成本", "costs.py 是成本的唯一来源")
def _cost_single_source():
    defs = ("MIN_COMMISSION", "NOMINAL_FEE", "SLIP", "STAMP",
            "MODELED_ROUND", "BREAK_EVEN_TICKET")
    pat = re.compile(r"^(" + "|".join(defs) + r")\s*=")
    bad = []
    for fn in sorted(os.listdir(os.path.join(ROOT, "tools"))):
        if not fn.endswith(".py") or fn == "costs.py":
            continue
        for i, line in enumerate(open(os.path.join(ROOT, "tools", fn),
                                      encoding="utf-8"), 1):
            if pat.match(line):
                bad.append(f"tools/{fn}:{i}")
    assert not bad, "成本常量在 costs.py 之外还有定义 → 会再次分叉：\n  " + "\n  ".join(bad)


@check("成本", "引擎默认值 == costs.py 且 round-trip 自洽")
def _engine_defaults():
    import inspect
    from tools import costs
    from tools.backtest_stock import run
    p = inspect.signature(run).parameters
    assert p["cost_buy"].default == costs.COST_BUY, "cost_buy 与 costs.COST_BUY 不一致"
    assert p["cost_sell"].default == costs.COST_SELL, "cost_sell 与 costs.COST_SELL 不一致"
    assert p["slippage"].default == costs.ENGINE_SLIPPAGE, "slippage 与 costs 不一致"
    engine_round = p["cost_buy"].default + p["cost_sell"].default + 2 * p["slippage"].default
    assert abs(engine_round - costs.MODELED_ROUND) < 1e-15, (
        f"引擎 round-trip {engine_round} ≠ MODELED_ROUND {costs.MODELED_ROUND} "
        "→ 事后调整式会与实际口径不符（2026-09-17 BUG-1）")


@check("成本", "无调用方覆盖 cost_buy/cost_sell（改默认值才有效）")
def _no_cost_override():
    hits = []
    for fn in sorted(os.listdir(os.path.join(ROOT, "tools"))):
        if not fn.endswith(".py") or fn == "backtest_stock.py":
            continue
        src = open(os.path.join(ROOT, "tools", fn), encoding="utf-8").read()
        if re.search(r"cost_(buy|sell)\s*=", src):
            hits.append(f"tools/{fn}")
    assert not hits, ("有调用方显式传 cost_buy/cost_sell → 引擎默认值被绕过，口径可能再次分叉：\n  "
                      + "\n  ".join(hits))


@check("成本", "派生函数与旧公式数值逐位一致")
def _cost_helpers_equivalent():
    from tools import costs
    for t in (5000.0, 10000.0, 3333.0, 20000.0, 1.0):
        old = (max(0.0005, 5 / t) + 0.0005) + (max(0.0005, 5 / t) + 0.001 + 0.0005)
        assert abs(costs.round_cost(t) - old) < 1e-15, f"ticket={t} 不等价"
    assert abs(costs.round_cost() - 0.003) < 1e-15
    assert abs(costs.BREAK_EVEN_TICKET - 10_000.0) < 1e-9


# ============================ 口径 ============================
@check("口径", "require_adj_mode 缺参即报错（防静默换口径）")
def _adj_mode_strict():
    import argparse
    from tools.test_dividend_factor import require_adj_mode
    try:
        require_adj_mode(argparse.Namespace())
    except RuntimeError:
        pass
    else:
        raise AssertionError(
            "缺少 adj_mode 时没有报错 → 会静默用默认口径，"
            "造成『口径不可复现却没提示』（2026-09-17 BUG-3）")
    assert require_adj_mode(argparse.Namespace(adj_mode="legacy")) == "legacy"
    assert require_adj_mode(argparse.Namespace(adj_mode="correct")) == "correct"
    try:
        require_adj_mode(argparse.Namespace(adj_mode="whatever"))
    except RuntimeError:
        pass
    else:
        raise AssertionError("非法 adj_mode 值未被拒绝")


@check("口径", "所有 adj 敏感脚本都提供 --adj-mode 且**不给静默默认值**", heavy=False)
def _adj_flag_present():
    import re
    need = ["test_dividend_factor.py", "backtest_dividend.py", "sweep_hyst.py",
            "test_industry_neutral.py", "sweep_dividend_into_mf.py",
            "diag_dividend_into_mf.py", "diag_industry_hyst_overlap.py",
            "paper_track_dividend.py"]
    miss = []
    for fn in need:
        src = open(os.path.join(ROOT, "tools", fn), encoding="utf-8").read()
        if '"--adj-mode"' not in src:
            miss.append(fn)
    assert not miss, "这些脚本没有 --adj-mode → 口径无法显式指定：\n  " + "\n  ".join(miss)

    # 2026-09-18 BUG-4：argparse 里给 `--adj-mode` 设 default 会**架空** require_adj_mode
    # 的「缺参即报错」—— argparse 总会把默认值填进 namespace，于是 getattr 永不返 None，
    # 上面那条 `_adj_mode_strict` 检查就永远测不到真实调用路径。
    # 实测 12 个脚本全部中招（backtest_dividend / daily_learn / diag_* / eval_candidate /
    # monitor_factors / paper_track_dividend / sweep_* / test_* / walk_forward）。
    bad = []
    tdir = os.path.join(ROOT, "tools")
    for fn in sorted(os.listdir(tdir)):
        if not fn.endswith(".py"):
            continue
        src = open(os.path.join(tdir, fn), encoding="utf-8").read()
        m = re.search(r'add_argument\(\s*"--adj-mode"[^)]*\)', src)
        if m and "default=" in m.group(0) and "default=None" not in m.group(0):
            bad.append(fn)
    assert not bad, (
        "这些脚本给 --adj-mode 设了静默默认值 → require_adj_mode 的安全网被架空，"
        "忘记透传时会**悄悄换口径**（2026-09-18 BUG-4）：\n  " + "\n  ".join(bad))


@check("口径", "point-in-time：correct 换截止日 → 历史段差异恰好 0", heavy=True)
def _adj_pointintime():
    from tools.test_dividend_factor import build_yield_panel
    b = pd.read_parquet(os.path.join(BARS, "bars_total_tax10.parquet"),
                        columns=["code", "date", "close", "volume"])
    b["date"] = b.date.astype(str).str.replace("-", "", regex=False)
    div = pd.read_parquet(os.path.join(ROOT, "data", "dividends", "bonus_all.parquet"))
    # 抽样 400 只，控制耗时
    rng = np.random.default_rng(0)
    codes = rng.choice(b.code.unique(), min(400, b.code.nunique()), replace=False)
    b = b[b.code.isin(codes)].sort_values(["code", "date"]).reset_index(drop=True)
    div = div[div.code.isin(codes)]
    CUT_A, CUT_B = "20221231", "20260911"
    grid = b[b.date <= CUT_A].copy()
    da = div[div.EX_DIVIDEND_DATE.astype(str).str.replace("-", "", regex=False) <= CUT_A]
    db = div.copy()
    for mode in ("correct", "legacy"):
        pa = build_yield_panel(grid.copy(), da, price_col="close", adj_mode=mode)
        pb = build_yield_panel(grid.copy(), db, price_col="close", adj_mode=mode)
        m = pa[["code", "date", "dps_ttm"]].merge(
            pb[["code", "date", "dps_ttm"]], on=["code", "date"], suffixes=("_a", "_b"))
        m = m.dropna(subset=["dps_ttm_a", "dps_ttm_b"])
        diff = (m.dps_ttm_a - m.dps_ttm_b).abs().max()
        n_bad = int(((m.dps_ttm_a - m.dps_ttm_b).abs() > 1e-12).sum())
        if mode == "correct":
            assert diff == 0, (
                f"correct 口径在换数据截止日后历史段变了（不一致 {n_bad} 组 / 最大差 {diff}）"
                "→ 它不再是 point-in-time 的，可复现性假设被破坏")
        else:
            print(f"      （对照）legacy 不一致 {n_bad} 组，最大差 {diff:.6f} —— 预期非 0")


# ============================ 涨跌停 ============================
@check("涨跌停", "分档表正确（含创业板时间分档 / 北交所 30%）")
def _limit_tiers():
    from tools.backtest_stock import build_features
    n = 130
    rows = []
    for code in ("SH600000", "SH688981", "SZ300750", "BJ430047"):
        for i in range(n):
            d = pd.Timestamp("2019-01-02") + pd.Timedelta(days=i)
            rows.append({"code": code, "date": d.strftime("%Y%m%d"),
                         "open": 10.0, "close": 10.0, "volume": 1e5})
    f = build_features(pd.DataFrame(rows))
    got = {}
    for code, lim in (("SH600000", 0.10), ("SH688981", 0.20),
                      ("SZ300750", 0.10), ("BJ430047", 0.30)):
        got[code] = set(f.loc[f.code == code, "limit"].round(4).unique())
    # 2019 年的创业板是 10%
    assert got["SZ300750"] == {0.10}, f"创业板 2019 年应为 10%，实得 {got['SZ300750']}"
    assert got["SH600000"] == {0.10}, got["SH600000"]
    assert got["SH688981"] == {0.20}, got["SH688981"]
    assert got["BJ430047"] == {0.30}, got["BJ430047"]
    # 2021 年的创业板应为 20%
    rows2 = [{"code": "SZ300750", "date": (pd.Timestamp("2021-01-04") + pd.Timedelta(days=i)).strftime("%Y%m%d"),
              "open": 10.0, "close": 10.0, "volume": 1e5} for i in range(n)]
    f2 = build_features(pd.DataFrame(rows2))
    assert set(f2.limit.round(4).unique()) == {0.20}, \
        f"创业板 2021 年应为 20%，实得 {set(f2.limit.round(4).unique())}"


@check("涨跌停", "判据用比例而非绝对价 round(2)（漏判率修复）")
def _limit_is_ratio():
    from tools.backtest_stock import build_features
    n = 130
    rows = [{"code": "SH600000",
             "date": (pd.Timestamp("2024-01-02") + pd.Timedelta(days=i)).strftime("%Y%m%d"),
             "open": 11.0 if i == n - 1 else 10.0,      # 末日一字涨停开盘 +10%
             "close": 10.0, "volume": 1e5} for i in range(n)]
    f = build_features(pd.DataFrame(rows))
    assert bool(f.buy_blocked.iloc[-1]) is True, (
        "开盘正好 +10%（主板一字涨停）没有被判为买不进 → 判据仍在漏判")
    # 指数价带小数时也要能判到（旧 round(2) 会因阈值偏高一格而漏判）
    rows[0]["open"] = 10.0
    df = pd.DataFrame(rows)
    df.loc[df.index[-1], "close"] = 10.0
    df["close"] = 9.876543
    df["open"] = np.where(df.index == df.index[-1], 9.876543 * 1.10, 9.876543)
    f2 = build_features(df)
    assert bool(f2.buy_blocked.iloc[-1]) is True, "指数价口径下 +10% 一字涨停被漏判"


# ============================ 复权阈值 ============================
@check("复权", "THRESH 四处实现值一致（单一来源纪律）")
def _thresh_consistent():
    import dataprovider.adjust as adj
    vals = {"dataprovider/adjust.py": adj.THRESH}
    for fn in ("diag_price_anomalies.py", "repair_etf_adjust.py", "sweep_etf_pool_expand.py"):
        src = open(os.path.join(ROOT, "tools", fn), encoding="utf-8").read()
        m = re.search(r"^THRESH\s*=\s*([0-9.]+)", src, re.M)
        if m:
            vals[f"tools/{fn}"] = float(m.group(1))
    uniq = {round(v, 6) for v in vals.values()}
    assert len(uniq) == 1, f"THRESH 出现多个值 → 会分叉：{vals}"
    assert uniq == {0.35}, f"THRESH 应为 0.35，实得 {uniq}"


# ============================ 稳定性 ============================
@check("稳定性", "长循环脚本必须接入增量落盘（不允许只写在结尾）")
def _incremental_flush_wired():
    """⚠️ 2026-09-18 新增。

    项目铁律要求「长任务增量落盘 + 断点续跑」，但这些脚本原本**只在结尾写一次 CSV**。
    实测代价：`sweep_hyst` 被 SIGTERM 掐在第 12 分钟 → **12 分钟白跑、产物为空**。
    这条检查防止以后有人把 `flush_partial` 删掉又退回"结尾写一次"。
    """
    heavy_loops = ["sweep_hyst.py", "test_industry_neutral.py",
                   "sweep_dividend_into_mf.py", "diag_dividend_into_mf.py"]
    miss = []
    for fn in heavy_loops:
        src = open(os.path.join(ROOT, "tools", fn), encoding="utf-8").read()
        if "flush_partial(" not in src:
            miss.append(fn)
    assert not miss, ("这些脚本没有接入增量落盘（中途被掐断会全丢）：\n  " + "\n  ".join(miss))
    # 单一来源：不许自己再造一个写盘函数
    src = open(os.path.join(ROOT, "tools", "progress.py"), encoding="utf-8").read()
    assert "def flush_partial(" in src, "tools/progress.py 缺少 flush_partial"


@check("稳定性", "大文件缓存必须「原子写 + 读容错」；年化外推必须有样本守卫")
def _panel_cache_atomic():
    """⚠️ 2026-09-19 新增，起因是**两次真实事故**（同一类：守卫太弱 / 不校验）。

    **事故 1 —— 缓存写坏就永久崩**：`panel_cache.build_panel` 原来直接
    `df.to_parquet(cache)` —— 面板 **1.4GB**、写盘 30~60 秒，被 SIGTERM / 死机打断
    就留下**截断的 parquet**（实测只写了 **315MB**，正式文件被覆盖）。
    更糟的是**读缓存不校验** → 此后**每次运行都抛 `ArrowInvalid` 直接崩**，
    而且报错完全看不出是缓存的问题（要翻到 `panel_cache.py:73` 才知道）。

    **事故 2 —— 样本不足还硬算年化 → 假警报**：`monitor_factors.forward_dev`
    原来只要求 `len(wide) >= 2`，于是锚点后**只有 3 行数据**时就算出
    「前向年化 13.10% vs 回测 7.45%」→ `fwd_vs_bt_dev` 0.0565 > 阈值 0.05 → 越界。
    2 个交易日的年化值方差极大（年化 = 日收益^244），**不具统计意义**。

    本检查防止以后有人把这两处守卫删掉、退回"直接写 / 不校验 / 样本不足也外推"。
    """
    src = open(os.path.join(ROOT, "tools", "panel_cache.py"), encoding="utf-8").read()
    assert "os.replace(" in src, \
        "panel_cache.py 缺少原子替换 os.replace() → 写盘被打断会留下截断的 parquet"
    assert ".tmp" in src, "panel_cache.py 没有先写 .tmp 临时文件"
    assert "except Exception" in src, \
        "panel_cache.py 读缓存没有容错 → 缓存写坏后每次运行都会直接崩"
    # 缓存目录不许进版本库（1.4GB×N）
    gi = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read()
    assert "data/" in gi, ".gitignore 里没有忽略 data/（缓存会进仓库）"

    mon = open(os.path.join(ROOT, "tools", "monitor_factors.py"), encoding="utf-8").read()
    assert "MIN_TRACK_DAYS" in mon, \
        ("monitor_factors.py 的前向偏离缺少最小样本守卫 → "
         "锚点后几天数据就会被年化，产出假警报")
    assert "样本不足" in mon, "monitor_factors.py 缺少「样本不足」的显式提示"


@check("稳定性", "run_reruns 的步骤产物路径与文档口径一致")
def _reruns_consistent():
    from tools.run_reruns import STEPS
    ids = [s[0] for s in STEPS]
    assert len(ids) == len(set(ids)), f"步骤 id 重复：{ids}"
    for sid, name, cmd, outs, doc, est in STEPS:
        assert cmd and outs and doc, f"{sid} 字段不全"
        for o in outs:
            assert o.startswith("results/"), f"{sid} 产物应落在 results/ 下：{o}"
        assert "--adj-mode" in cmd, f"{sid} 缺少 --adj-mode（口径必须显式）"


# ============================ 自优化（M1~M4） ============================
AUTO_MODULES = ["eval_candidate.py", "monitor_factors.py", "walk_forward.py",
                "daily_learn.py"]
MONITOR_KEYS = ["ic_all_min", "ic_12m_min", "icir_min", "ic_pos_years_min",
                "decile_tb_min", "dd_max", "excess_vs_csi1000_min", "hhi_max",
                "industry_count_min", "turnover_max_mult", "fwd_vs_bt_max_dev",
                "pool_size_min"]


@check("自优化", "M1~M4 都存在且 --help 可执行")
def _auto_modules_runnable():
    import subprocess
    missing = [f for f in AUTO_MODULES
               if not os.path.exists(os.path.join(ROOT, "tools", f))]
    assert not missing, "缺少模块：" + ", ".join(missing)
    for f in AUTO_MODULES:
        r = subprocess.run([sys.executable, os.path.join("tools", f), "--help"],
                           cwd=ROOT, capture_output=True, timeout=120)
        assert r.returncode == 0, f"{f} --help 失败：{r.stderr.decode('utf-8','replace')[:300]}"


@check("自优化", "M1~M4 都接入增量落盘（flush_partial）")
def _auto_incremental():
    bad = []
    for f in AUTO_MODULES:
        src = open(os.path.join(ROOT, "tools", f), encoding="utf-8").read()
        if "flush_partial(" not in src:
            bad.append(f)
    assert not bad, "未接入增量落盘：" + ", ".join(bad)


@check("自优化", "M1~M4 都使用 require_adj_mode（口径必传）")
def _auto_adj_mode():
    bad = []
    for f in AUTO_MODULES:
        src = open(os.path.join(ROOT, "tools", f), encoding="utf-8").read()
        if "require_adj_mode" not in src:
            bad.append(f)
    assert not bad, "未使用 require_adj_mode：" + ", ".join(bad)


@check("自优化", "M1~M4 不自行定义成本常量")
def _auto_no_cost_const():
    pat = re.compile(r"^(MIN_COMMISSION|NOMINAL_FEE|SLIP|STAMP|MODELED_ROUND"
                     r"|BREAK_EVEN_TICKET)\s*=")
    bad = []
    for f in AUTO_MODULES:
        for i, line in enumerate(open(os.path.join(ROOT, "tools", f),
                                      encoding="utf-8"), 1):
            if pat.match(line):
                bad.append(f"tools/{f}:{i}")
    assert not bad, "自行定义了成本常量：\n  " + "\n  ".join(bad)


@check("自优化", "config/monitor.json 12 个阈值键齐全")
def _auto_monitor_config():
    import json
    p = os.path.join(ROOT, "config", "monitor.json")
    assert os.path.exists(p), "缺少 config/monitor.json"
    cfg = json.load(open(p, encoding="utf-8"))
    miss = [k for k in MONITOR_KEYS if k not in cfg]
    assert not miss, f"缺键：{miss}"
    from tools.monitor_factors import REQUIRED_KEYS
    assert set(REQUIRED_KEYS) == set(MONITOR_KEYS), (
        "monitor_factors.REQUIRED_KEYS 与 selftest.MONITOR_KEYS 不一致（会分叉）")


@check("自优化", "state/refuted.jsonl 每行是合法 JSON（允许不存在）")
def _auto_refuted_readable():
    import json
    p = os.path.join(ROOT, "state", "refuted.jsonl")
    if not os.path.exists(p):
        print("      （文件尚未产生，跳过解析）")
        return
    for i, line in enumerate(open(p, encoding="utf-8"), 1):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except Exception as e:                                # noqa: BLE001
            raise AssertionError(f"state/refuted.jsonl 第 {i} 行不是合法 JSON：{e}")


# ============================ 文档命令 ============================
@check("文档", "文档命令审计四类全 0")
def _doc_commands_clean():
    from tools.audit_doc_commands import collect_problems
    p = collect_problems()
    bad = {k: v for k, v in p.items() if v}
    assert not bad, ("文档命令有未处理问题（照文档跑会报错 / 静默换口径 / 覆盖历史产物）：\n  "
                     + "\n  ".join(f"{k}: {len(v)} 条" for k, v in bad.items()))


# ============================ CLI ============================
@check("CLI", "argparse 的 help/description 里没有「裸 %」（否则 --help 直接崩）")
def _cli_help_no_bare_percent():
    """2026-09-18 踩：`help="股息率上限（%）"` 会让 `argparse` 在 `--help` 时做 `help % params`
    → `ValueError: unsupported format character '?' (0xff09)` → **脚本 `--help` 直接崩**。
    这是**纯静默**的（正常调用完全不受影响），实测 3 个脚本中招：
    `test_industry_neutral.py` / `backtest_dividend.py` / `verify_adj_pointintime.py`。
    修法：把 `%` 写成 `%%`。本检查用 AST 静态扫，秒级完成，不 spawn 子进程。"""
    import ast

    def bare_percent(s: str) -> bool:
        i = 0
        while i < len(s):
            if s[i] == "%":
                if s[i + 1:i + 2] == "%":       # 已转义的 %%
                    i += 2
                    continue
                if s[i + 1:i + 2] in ("s", "d", "f", "r", "i", "g", "e", "x", "o", "c", "a"):
                    i += 2                      # 合法占位符
                    continue
                return True
            i += 1
        return False

    bad = []
    for fn in sorted(os.listdir(os.path.join(ROOT, "tools"))):
        if not fn.endswith(".py"):
            continue
        path = os.path.join(ROOT, "tools", fn)
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError as e:
            bad.append(f"tools/{fn}: 语法错误 {e}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg not in ("help", "description", "epilog", "usage"):
                    continue
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    if bare_percent(kw.value.value):
                        bad.append(f"tools/{fn}:{kw.value.lineno}  {kw.value.value[:48]}")
    assert not bad, ("以下 argparse 文本含「裸 %」→ `--help` 会崩（改成 %%）：\n  "
                     + "\n  ".join(bad))


# ============================ 数据 ============================
@check("数据", "关键数据文件存在且末根交易日合理")
def _data_present():
    need = ["bars_total.parquet", "bars_total_tax10.parquet", "bars_total_tax20.parquet",
            "bars_bfq.parquet", "universe_all.csv"]
    miss = [f for f in need if not os.path.exists(os.path.join(BARS, f))]
    assert not miss, "缺少数据文件：" + ", ".join(miss)
    for f in ("data/dividends/bonus_all.parquet", "data/industry/industry_all.parquet"):
        assert os.path.exists(os.path.join(ROOT, f)), f"缺少 {f}"
    b = pd.read_parquet(os.path.join(BARS, "bars_total_tax10.parquet"),
                        columns=["date"])
    last = int(b.date.astype(str).str.replace("-", "", regex=False).max())
    assert 20240101 < last <= 21000101, f"末根交易日异常：{last}"
    print(f"      bars_total_tax10 末根交易日 = {last}")


@check("数据", "个股线数据新鲜度（末根交易日 vs 最近已收盘交易日）")
def _stock_data_fresh():
    """⚠️ 2026-09-18 新增：原先**只有 ETF 线**有新鲜度守卫，个股线没有 ——
    研究/纸面跟踪可能不知不觉跑在陈旧数据上（样本外区间末端会失真）。"""
    from dataprovider.calendar import latest_closed_trading_day
    ref = str(latest_closed_trading_day(None))
    b = pd.read_parquet(os.path.join(BARS, "bars_total_tax10.parquet"),
                        columns=["date"])
    last = str(int(b.date.astype(str).str.replace("-", "", regex=False).max()))
    d1 = pd.Timestamp(ref)
    d2 = pd.Timestamp(last)
    gap = (d1 - d2).days
    print(f"      最近已收盘交易日 = {ref}｜个股线数据末根 = {last}｜落后 {gap} 个自然日")
    if gap > 10:
        raise AssertionError(
            f"个股线数据落后 {gap} 个自然日（>{10}）→ 跑 "
            f"`$PY tools/append_stock_bars.py --out data/stockbars --workers 8` 补齐")
    if gap > 4:
        print(f"      ⚠️ 已落后 {gap} 个自然日，建议尽快补数据（未达失败线 10 天）")


@check("数据", "个股线池内 close<=0 / amt<=0 均为 0", heavy=True)
def _data_sane():
    b = pd.read_parquet(os.path.join(BARS, "bars_total_tax10.parquet"),
                        columns=["code", "close", "amount"])
    assert int((b.close <= 0).sum()) == 0, "存在非正收盘价"
    if "amount" in b.columns:
        assert int((b.amount.fillna(0) < 0).sum()) == 0, "存在负成交额"


# ============================ 主流程 ============================
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="快速回归自检")
    ap.add_argument("--quick", action="store_true", help="跳过需要读大 parquet 的项")
    ap.add_argument("--list", action="store_true", help="只列出检查项")
    args = ap.parse_args(argv)

    if args.list:
        for g, n, h in _CHECKS:
            print(f"  [{g}] {n}{'  (heavy)' if h else ''}")
        return 0

    fns = [v for v in globals().values() if hasattr(v, "_selftest")]
    fns.sort(key=lambda f: f._selftest)
    print("=" * 96)
    print("  快速回归自检    （改动前后都该跑一遍）")
    print("=" * 96)
    n_pass = n_fail = n_skip = 0
    fails = []
    last_group = None
    for fn in fns:
        group, name, heavy = fn._selftest
        if group != last_group:
            print(f"\n  ── {group} ──")
            last_group = group
        if heavy and args.quick:
            print(f"    ⏭  {name}（--quick 跳过）")
            n_skip += 1
            continue
        try:
            fn()
        except Exception as e:                       # noqa: BLE001
            n_fail += 1
            print(f"    ✗  {name}")
            print(f"       {type(e).__name__}: {e}")
            fails.append((group, name, traceback.format_exc()))
        else:
            n_pass += 1
            print(f"    ✓  {name}")

    print("\n" + "=" * 96)
    print(f"  通过 {n_pass} ／ 失败 {n_fail} ／ 跳过 {n_skip}")
    if n_fail:
        print("  失败明细：")
        for g, n, tb in fails:
            print(f"\n  ── [{g}] {n} ──")
            print("    " + tb.strip().replace("\n", "\n    ")[-1500:])
    print("=" * 96)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
