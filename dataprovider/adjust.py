"""ETF/指数行情的**份额拆分（份额折算）自动复权** —— 单一实现来源。

为什么需要它（2026-09-16 的真实事故）
------------------------------------
`data/stocks/*.csv` 是 akshare 的**不复权**行情。ETF 发生份额折算时价格会跳变
（如 1 份变 5 份、价格腰斩），但**投资者的真实收益是连续的**，回测引擎只看到价格，
于是把跳变当成真实涨跌。

09-13 已修好 4 处，结果 **09-16 的每日刷新把修复静默回退了** ——
`DataStore.refresh()` 会用 akshare 的**原始未复权全量历史**覆盖 `data/stocks/*.csv`。
实测（`tools/cmp_split_impact.py`，同一引擎同一参数，只切换数据目录）：

| 数据 | 年化 | 夏普 | 回撤 |
|---|---|---|---|
| 未复权（刷新后） | 2.35% | 0.33 | −13.84% |
| 已复权 | **3.47%** | **0.49** | **−9.28%** |

→ **代价 1.12pp/年。** 一次性打补丁治不了，必须在**读取时**复权。

设计
----
- `scan_jumps()`：找出 `|日收益| > thresh` 的日子（ETF 无涨跌停，20% 已极罕见）
- `repair_splits()`：把每个跳变日**之前**的价格整段乘以跳变比率，使收益连续
- **幂等**：修好后 `rate` 不再有超阈跳变 → 再跑一遍无变化
- 在 `DataStore.read()` 里调用 → **无论文件被刷新覆盖多少次，策略读到的永远连续**

⚠️ **副作用（必须知道）**：复权会把**跳变日之前**的绝对价格整体缩放。
本项目 ETF 回测不使用 `min_price` 绝对价格门槛，故无影响；
若将来引入绝对价格过滤，需重新评估（参见 `docs/个股线_复权口径缺陷.md` 的同类教训：
**重建价格序列必须断言量级与原价一致**）。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

THRESH = 0.20          # 单日 |收益| 超过此值即视为异常
PRICE_COLS = ["open", "high", "low", "close"]


def scan_jumps(d: pd.DataFrame, thresh: float = THRESH) -> list:
    """找出跳变日。返回 [(date, prev_close, close, ret)]，按日期升序。

    `d` 需以 `date` 为索引且已升序，含 `close` 列。
    """
    if d.empty or "close" not in d.columns:
        return []
    r = d["close"].pct_change()
    out = []
    for dt, v in r.items():
        if v == v and abs(v) > thresh:          # v == v 排除 NaN
            i = d.index.get_loc(dt)
            if i == 0:
                continue
            out.append((str(dt), float(d["close"].iloc[i - 1]),
                        float(d["close"].iloc[i]), float(v)))
    return out


def repair_splits(d: pd.DataFrame, jumps: list) -> pd.DataFrame:
    """把每个跳变日**之前**的价格整段乘以跳变比率，使收益连续。

    与 `tools/diag_price_anomalies.py` 原实现**语义完全一致**（该实现已验证过）：
    从最早的跳变开始处理，避免多跳变时索引错位。
    """
    out = d.copy()
    for dt, prev_close, close, ret in jumps:          # 已按日期升序
        if prev_close == 0:
            continue
        ratio = close / prev_close
        mask = out.index < dt
        cols = [c for c in PRICE_COLS if c in out.columns]
        out.loc[mask, cols] = out.loc[mask, cols] * ratio
    return out


def repair_frame(d: pd.DataFrame, code: str = "", thresh: float = THRESH,
                 log_path=None) -> tuple:
    """一步到位：扫描 + 修复。返回 `(df, n_repaired, jumps)`。

    幂等：无跳变时原样返回、`n_repaired=0`。
    """
    jumps = scan_jumps(d, thresh)
    if not jumps:
        return d, 0, []
    fixed = repair_splits(d, jumps)
    if log_path:
        _log(log_path, code, jumps)
    return fixed, len(jumps), jumps


def _log(log_path, code: str, jumps: list) -> None:
    """把复权动作追加到审计日志（**份额折算是正常事件，不该刷告警**，但必须留痕）。"""
    try:
        p = Path(log_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(p, "a", encoding="utf-8") as f:
            for dt, pc, cl, ret in jumps:
                f.write(f"{ts}\t{code}\t{dt}\t{pc:.4f}->{cl:.4f}\t{ret:+.1%}\n")
    except Exception:
        pass


def repair_csv(path, out_path=None, thresh: float = THRESH) -> int:
    """对 CSV 文件做复权。`out_path=None` 时**原地覆盖**（会先备份）。

    返回修复的跳变数。仅供工具/脚本使用；策略路径走 `DataStore.read()` 的内存复权。
    """
    p = Path(path)
    d = pd.read_csv(p, dtype={"date": str}).set_index("date").sort_index()
    fixed, n, _ = repair_frame(d, p.stem, thresh)
    if n == 0:
        return 0
    if out_path is None:
        import shutil
        bak = p.with_suffix(p.suffix + ".pre_split_repair.bak")
        if not bak.exists():
            shutil.copy2(p, bak)
        out_path = p
    fixed.reset_index().to_csv(out_path, index=False, encoding="utf-8-sig")
    return n


if __name__ == "__main__":
    # 自检：对当前 data/stocks 扫描一遍，报告复权前后异常数
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import config  # noqa: E402

    codes = list(config.RECOMMENDED_POOLS["ETF全球"])
    tot = 0
    for c in codes:
        fp = Path(config.STOCK_DIR) / (c + ".csv")
        if not fp.exists():
            continue
        raw = pd.read_csv(fp, dtype={"date": str}).set_index("date").sort_index()
        fixed, n, jumps = repair_frame(raw, c)
        if n:
            resid = scan_jumps(fixed)
            print(f"{c}: 修复 {n} 处 → 残留 {len(resid)} 处")
            for dt, pc, cl, ret in jumps:
                print(f"    {dt}  {pc:>9.3f} -> {cl:>9.3f}  ({ret:+.1%})")
            tot += n
    print(f"\n合计需复权 {tot} 处")
