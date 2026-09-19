"""轻量统计工具（不依赖 scipy）。

> **规格偏差说明（重要）**
> `docs/需求规格_自优化闭环模块.md` §2.1 写的是「Spearman 用 `pandas.Series.corr('spearman')`」。
>
> ⚠️ **2026-09-18 22:30 复核订正**：当时记为「实测**不可行**（pandas 内部会 `from scipy.stats
> import spearmanr` → `ModuleNotFoundError`）」——**这条在当前环境已不成立**。
> 实测（`pandas 3.0.5` / `numpy 2.5.3`，venv 里**确实没有 scipy**）：
> `df.groupby("date")[["f","y"]].corr(method="spearman")` **可以正常跑**，
> 且与下面的 `spearman()` **数值完全一致（最大差 5.6e-17）** —— 新版 pandas 内置了秩相关实现。
> → 即 `tools/test_dividend_factor.py` 里那句 `.corr(method="spearman")` 是能跑的。
>
> **仍然保留本模块的理由（不是"因为不可行"）**：
> ① **不依赖 pandas 内部实现**（它的 spearman 走哪条路径、哪个版本有 scipy 回退，不是我们能控的）；
> ② **与既有脚本口径一致**（`test_stock_factors` 等一直是 `rank()` + `np.corrcoef`）；
> ③ `min_n` 守卫与常数序列返回 `nan` 的行为是显式的。
> **→ 两者数值等价，选哪个都不影响结论；本模块是"显式可控"的那一个。**
>
> 正态分布相关函数（DSR 需要）同样不引入 scipy：`ncdf` 用 `math.erf`，`nppf` 用二分法。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

EULER = 0.5772156649015329


def spearman(a, b, *, min_n: int = 5) -> float:
    """NaN 感知的 Spearman 秩相关（= 秩的 Pearson 相关）。

    与 `scipy.stats.spearmanr(a, b)` 等价；只用 pandas/numpy。
    样本不足或存在常数序列时返回 `nan`。
    """
    x = np.asarray(a, dtype=float)
    y = np.asarray(b, dtype=float)
    m = np.isfinite(x) & np.isfinite(y)
    if int(m.sum()) < min_n:
        return float("nan")
    rx = pd.Series(x[m]).rank().to_numpy()
    ry = pd.Series(y[m]).rank().to_numpy()
    if rx.std(ddof=0) == 0 or ry.std(ddof=0) == 0:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


def ncdf(x: float) -> float:
    """标准正态 CDF（替代 scipy.stats.norm.cdf）。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def nppf(p: float, lo: float = -12.0, hi: float = 12.0, iters: int = 200) -> float:
    """标准正态分位数（二分法，替代 scipy.stats.norm.ppf）。"""
    p = min(max(p, 1e-12), 1 - 1e-12)
    for _ in range(iters):
        mid = (lo + hi) / 2
        if ncdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2
