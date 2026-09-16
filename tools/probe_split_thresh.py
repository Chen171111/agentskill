"""阈值对照探针：切换「份额折算」判定阈值，量化误判对实盘配置的影响。

为什么需要它（2026-09-16 晚发现的真实缺陷）
-------------------------------------------
`dataprovider/adjust.py` 曾用 `|日收益| > 20%` 判份额折算，但
**A 股 ETF 有涨跌停：主板 10%、创业板/科创板 20%** → 判据与涨停带完全重叠。
（跨境 / 商品黄金 / 债券货币 ETF 均为 10%；北交所 30% 属制度预留、尚无品种。
完整分档依据见 `docs/参考_A股ETF涨跌幅限制.md`。）

实测（`data/stocks/*.csv` 原始未复权价）：

| 代码 | 日期 | 收益率 | 0.20 判据 | 真相 |
|---|---|---|---|---|
| `159949.SZ` | 2024-10-08 | 0.20019821 | ❌ **误判为折算** | 真实 +20.02% 涨停 |
| `159915.SZ`（★池内） | 2024-09-30 | 0.19999999999999996 | 未触发（浮点侥幸） | 真实 +20.00% 涨停 |
| `159915.SZ`（★池内） | 2024-10-08 | 0.19982078853046592 | 未触发 | 真实 +19.98% 涨停 |
| `588000.SH` | 2024-10-08 | 0.19999999999999996 | 未触发（浮点侥幸） | 真实 +20.00% 涨停 |

`159949` 已被真实误伤（`state/split_repairs.log` 有记录）：把该日之前的历史整体
×1.2 → **当天真实涨停被抹成 0%**。已用真实行情确证是涨停而非折算
（成交额 141.87 亿创上市新高、份额折算比例字段为 `--`、无折算公告、涨停价 = 收盘价）。

**本脚本的用途**：同一份文件、同一套引擎与参数，**只切换判定阈值**，
因此差异 100% 来自"复权判据的松紧"。改阈值前后都应跑一次，确认口径没漂。

历史结果（实盘配置 `etf_rotation` / `ETF全球` / topk=5 / 5日 / 2019~2026-09）
-----------------------------------------------------------------------------

| 阈值 | 159915 是否被误判 | 年化 | 夏普 | 累计 | 回撤 |
|---|---|---|---|---|---|
| 0.20（改前现状） | 否 | 3.33% | 0.47 | 28.39% | −9.89% |
| 0.1995 | **是（2 处）** | **2.65%** | **0.39** | 22.14% | −9.86% |
| 0.25 | 否 | 3.33% | 0.47 | 28.39% | −9.89% |
| **0.35（现用值）** | 否 | **3.33%** | **0.47** | 28.39% | −9.89% |

→ **误判代价 −0.67pp/年、夏普 −0.08。**
→ 0.25 / 0.35 与 0.20 的结果**逐位相同**（`3.3256788182161046`），
   说明修正只是把"浮点运气"变成"确定性"，没有任何副作用。
→ **为什么最终取 0.35 而不是 0.25**：必须为北交所 **30%** 档预留
   （北证50 ETF 上市那天 0.25 就会失效）；0.35 距 30% 预留档 5pp、
   距最小真实折算 49.18% 仍有 14.2pp 余量。

⚠️ 只读：不写任何数据文件，`log_path=None` 不污染 `state/split_repairs.log`。

用法
----
    PY="C:/Users/XiaoQi/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
    cd "E:/MyWorkAndProject/量化/agentskill"
    $PY -u tools/probe_split_thresh.py 0.35      # 现用值，应 = 3.33%
    $PY -u tools/probe_split_thresh.py 0.1995    # 应 = 2.65%（展示误判代价）
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config  # noqa: E402
import dataprovider.adjust as adjust  # noqa: E402
import dataprovider.store as store_mod  # noqa: E402

if len(sys.argv) < 2:
    print(__doc__)
    print("用法: python tools/probe_split_thresh.py <thresh>   （如 0.25）")
    sys.exit(2)

thresh = float(sys.argv[1])


def _patched_repair_frame(d, code="", log_path=None):
    """绕开 `adjust.repair_frame` 的默认参数绑定。

    ⚠️ Python 的默认参数在**函数定义时**求值，所以改 `adjust.THRESH` 不会影响
    `repair_frame(d, code, thresh=THRESH, ...)` 的默认值；必须替换
    `store` 命名空间里绑定的那个函数对象。
    """
    return adjust.repair_frame(d, code, thresh=thresh, log_path=None)


store_mod.repair_frame = _patched_repair_frame

from pipeline import run_backtest  # noqa: E402

codes = list(config.RECOMMENDED_POOLS["ETF全球"])
out = run_backtest(codes, strategy="etf_rotation", start="20190101",
                   end="20260911", topk=5, rebalance=5)
m = out["metrics"]
print("RESULT " + json.dumps(
    {"thresh": thresh,
     **{k: (float(v) if isinstance(v, (int, float)) else v) for k, v in m.items()}},
    ensure_ascii=False))
