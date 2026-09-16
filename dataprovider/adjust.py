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
- `scan_jumps()`：找出 `|日收益| > thresh` 的日子（阈值见下方"阈值选择"）
- `repair_splits()`：把每个跳变日**之前**的价格整段乘以跳变比率，使收益连续
- **幂等**：修好后 `rate` 不再有超阈跳变 → 再跑一遍无变化
- 在 `DataStore.read()` 里调用 → **无论文件被刷新覆盖多少次，策略读到的永远连续**

阈值选择：0.35，**不是 0.20**（2026-09-16 晚修正，同日二次修正）
----------------------------------------------------------------
**A 股 ETF 的涨跌幅限制取决于跟踪标的所属板块：**

| 类别 | 涨跌幅限制 | 本池 / 本库示例 |
|---|---|---|
| 沪深主板 ETF | **10%** | `510300` / `510500` / `510050` / `510880` |
| 创业板 / 科创板 ETF | **20%** | `159915`（创业板） / `588000` / `588170`（科创板） |
| 北交所 ETF | **30%**（⚠️ 制度预留，**尚无品种**） | 北证50 目前只有场外指数基金 |
| 跨境 ETF | **10%** | `159920` / `513100` / `513500` |
| 商品（含黄金）ETF | **10%** | `518880`（上交所黄金ETF业务指南明文 10%） |
| 债券 / 货币 ETF | **10%** | `511010` / `511880` / `511990` |

→ **现行有效的最高档是 20%；30% 是"未来档"，但阈值必须为它预留。**
→ **完整制度依据与三个误传澄清见 `docs/参考_A股ETF涨跌幅限制.md`（本模块不重复维护）。**

> ⚠️ 本模块曾写「部分跨境 / 商品类 ETF 可能无涨跌幅限制」，**这是错的** ——
> 交易所官方文件（上交所《黄金ETF业务指南》五-5)、证监会《深交所港股ETF答投资者问》）
> 均明确为 **10%**。该错误说法在券商投教与自媒体里流传很广，**别拿它给判据定阈值**。

### 用 0.20 时发生了什么（实测）

| 代码 | 日期 | 收益率 | `>0.20` 判据 | 真相 |
|---|---|---|---|---|
| `159949.SZ` | 2024-10-08 | 0.20019821 | ❌ **误判为折算** | 真实 +20.02% 涨停 |
| `159915.SZ`（★池内） | 2024-09-30 | 0.19999999999999996 | 未触发（浮点侥幸） | 真实 **+20.00%** 涨停 |
| `159915.SZ`（★池内） | 2024-10-08 | 0.19982078853046592 | 未触发 | 真实 +19.98% 涨停 |
| `588000.SH` | 2024-10-08 | 0.19999999999999996 | 未触发（浮点侥幸） | 真实 +20.00% 涨停 |

- `159949` 的 2024-10-08 **已被真实误伤**（`state/split_repairs.log` 有记录）：
  把该日之前的历史整体 ×1.2 → **当天真实涨停被抹成 0%**。
  已用真实行情确证是涨停而非折算（成交额 141.87 亿创上市新高、
  份额折算比例字段为 `--`、无折算公告、涨停价 = 收盘价）。
- `159915` **在实盘池内**，只因 `2.2320/1.8600-1 = 0.19999999999999996 < 0.20`
  这条**浮点舍入**才侥幸逃过 —— **不是判据安全，是运气**。
  对照实验（`tools/probe_split_thresh.py`，同一份文件只切换阈值）：
  若它被误判，实盘配置年化 **3.33% → 2.65%（−0.67pp/年）**、夏普 0.47 → 0.39。

### 取 0.35 的依据（实测，`tools/scan_return_bands.py`）

`data/stocks` 27 只 ETF、176 处 |单日收益| ≥ 9% 的事件：

| 依据 | 数据 |
|---|---|
| 现行最高涨停带 | 双创 **20%**，实测最高 **+20.02%**；且涨停价按 `前收×(1±比例)` **四舍五入至 0.001 元**，实际涨幅可略超名义值（实测 `159819` **+10.07%**）→ 阈值必须 **> 0.21** |
| 预留档 | 北交所 **30%** → 阈值必须 **> 0.30**（**这是 0.25 不够用的原因**） |
| 最小真实折算 | 池内 4 处 **+248.6% / −74.5% / −80.5% / −49.2%**（最小 **49.18%**）→ 阈值必须 **< 0.49** |
| 中间有无混淆区 | 19~21%：**7 处（全部是涨停）**；**21~30%：0 处**；**30~40%：0 处**；40~60%：1 处；>60%：3 处 |

→ **21%~40% 是一段完全空白的"安全间隙"**，0.35 落在正中：
  距 30% 预留档 **5pp**、距 49.18% 最小真实折算 **14.2pp**。

### ⚠️ 已知边界（必须知道）

1. **真实折算幅度若落在 21%~40% 会被漏检。** 当前全库 ETF 里不存在这种案例
   （21~40% 空白，最小 49.18%），但"净值归 1"型折算若净值恰好接近 1，
   幅度可能只有百分之十几 → **会漏**。这类漏检**体检也抓不到**
   （体检与复权共用同一阈值）→ 只能靠人工留意。
2. **样本边界**：上面的空白带结论基于 `data/stocks` 的 **27 只 ETF**，
   不是全市场 1000+ 只。扩池或换数据源后应重跑 `tools/scan_return_bands.py` 复核。
3. **固定阈值本身有天花板**：本模块已论证现行 A 股场内 ETF **不存在**"无涨跌幅限制"品种，
   但若将来真出现，任何单一阈值都可能误判 —— 因为"真能涨 40% 的品种"与
   "一次 −49% 的折算"在"单日收益"这一个维度上无法区分。
   根治方向是**二次判据**：精确份额比 / 净值交叉验证 / 折算公告白名单
   （详见 `docs/参考_A股ETF涨跌幅限制.md` §六），而不是继续调阈值。

> ⚠️ **本判据项目里曾有四处实现，三处各不相同**：
> `tools/repair_etf_adjust.py`（09-13，`0.25` ✅ 唯一对的）、
> `dataprovider/adjust.py`（09-16 新建，`0.20` ❌）、
> `tools/diag_price_anomalies.py`（`0.20` ❌）、
> `tools/sweep_etf_pool_expand.py`（`0.20` ❌）。
> **09-13 那次修正只改了一处、没回灌到其他三处** —— 这才是真正的病根，
> 比"重新发明"更隐蔽：**改对了 ≠ 改完了。**
> **教训：同一判据只允许一个实现来源；修正必须回灌到所有实现处。**
> 现已全部统一为 0.35，本模块为单一来源。

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

THRESH = 0.35          # 单日 |收益| 超过此值即视为份额折算异常。
                       # ⚠️ 为什么是 0.35 而不是 0.20 / 0.25：
                       #    ETF 涨跌幅限制按跟踪板块分 —— 主板 10%、创业板/科创板 20%、
                       #    北交所 30%（制度预留，尚无品种）、跨境 10%、商品/黄金 10%、
                       #    债券/货币 10%（见 docs/参考_A股ETF涨跌幅限制.md）。
                       #    阈值必须高于现行最高档（20%，实测最高 +20.02%），
                       #    并为北交所 30% 档预留 —— 否则会把真实涨停误判成折算
                       #    （159949 2024-10-08 的 +20.02% 真实涨停就这样被误伤过）。
                       #    另一半依据：真实折算幅度实测最小 49.18%（池内 4 处），
                       #    且全库 ETF 在 21%~40% 区间**完全空白** → 两侧各留 5pp / 14.2pp。
                       #    详见模块 docstring 的「阈值选择」与「已知边界」两节。
                       # ⚠️ 本模块是该判据的**单一来源**；另有 3 个 tools/ 脚本引用同一阈值
                       #    （diag_price_anomalies / repair_etf_adjust / sweep_etf_pool_expand），
                       #    改这里必须同步那三处（grep -rn THRESH）。
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
