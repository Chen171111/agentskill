"""交易成本模型 —— **单一来源**（改费率只改这里）。

为什么要单独成一个文件
----------------------
`MIN_COMMISSION / NOMINAL_FEE / SLIP / STAMP / MODELED_ROUND` 曾经在
`backtest_dividend` / `sweep_hyst` / `sweep_scaling` / `sweep_feasibility` /
`sweep_poolsize` 里**各自写一遍**，而且**已经真的分叉过一次**：

    `sweep_poolsize.py` 的 `NOMINAL_FEE` 停在 0.0003（万3），
    其余四处是 0.0005（万5）→ 它的「临界单笔金额」被算成 16,667 元（正确 10,000 元）。

这正是 `tools/README.md` 铁律 14 说的病：**同一判据在多处实现 = 迟早分叉；
改对了 ≠ 改完了。** 根治办法只有一个 —— 收敛到单一来源，其它地方 import。

主人实际费率
------------
| 项 | 费率 | 收取方向 | 常量 |
|---|---|---|---|
| 佣金 | **万5** | 买入 + 卖出**各一次** | `NOMINAL_FEE` |
| 单笔最低佣金 | **5 元/笔** | 买入 + 卖出**各一次** | `MIN_COMMISSION` |
| 印花税 | **万10** | **仅卖出**（ETF 免征、个股不免） | `STAMP` |
| 滑点 | **单边万5** | 买入 + 卖出**各一次** | `SLIP` |

round-trip（一买一卖）合计 = `(FEE + SLIP) + (FEE + STAMP + SLIP)` = **0.0030**

引擎自洽性
----------
`tools/backtest_stock.run()` 的默认参数**必须**等于 `COST_BUY / COST_SELL`：

    买入 `1 + COST_BUY + SLIP` ／ 卖出 `1 - COST_SELL - SLIP`
    → 双边合计 = `COST_BUY + COST_SELL + SLIP*2` = `MODELED_ROUND`

2026-09-17 之前引擎默认是万3（0.0003 / 0.0013），而事后调整式按 `MODELED_ROUND`
（万5）扣 → 所有「10 万真实年化」**少扣 `0.0004 × 换手`**。现已自洽，
`tools/selftest.py` 会断言这一条，**以后不会再静默分叉**。

⚠️ 「有效佣金率」不等于费率
--------------------------
10 万 ÷ 20 只 = **5,000 元/笔** → 按万5 应收 **2.5 元 < 最低 5 元** → 实收 5 元
→ **有效佣金率 = 万10**。这是最低佣金规则的必然结果，**不是费率超预算**。
用 `effective_commission(ticket)` 算；用 `min_commission_penalty()` 折算成年化 pp。
"""
from __future__ import annotations

# ---- 费率（主人实际）----
MIN_COMMISSION = 5.0          # 元/笔，买、卖各收一次
NOMINAL_FEE = 0.0005          # 佣金 万5
SLIP = 0.0005                 # 滑点 单边 万5
STAMP = 0.001                 # 印花税 万10，仅卖出

# ---- 引擎侧单边成本（必须与上面自洽，见模块 docstring）----
COST_BUY = NOMINAL_FEE                    # 买入佣金
COST_SELL = NOMINAL_FEE + STAMP           # 卖出佣金 + 印花税

# ---- 派生量 ----
# 纯费率口径下的双边合计 —— 事后调整式以它为「引擎已经扣过的基线」
MODELED_ROUND = COST_BUY + COST_SELL + SLIP * 2
# 临界单笔金额：低于它就会被最低佣金抬高有效费率
BREAK_EVEN_TICKET = MIN_COMMISSION / NOMINAL_FEE
# 兜底：引擎的滑点默认值（`backtest_stock.run(slippage=...)`）
ENGINE_SLIPPAGE = SLIP


def effective_commission(ticket: float) -> float:
    """单笔金额 `ticket`（元）下的**有效**单边佣金率（含最低佣金）。"""
    if ticket <= 0:
        return float("inf")
    return max(NOMINAL_FEE, MIN_COMMISSION / ticket)


def round_cost(ticket: float | None = None) -> float:
    """双边成本率。

    `ticket=None` → 纯费率口径（= `MODELED_ROUND`）；
    给定单笔金额时按**有效**佣金率算（含最低佣金）。
    """
    if ticket is None:
        return MODELED_ROUND
    c = effective_commission(ticket)
    return (c + SLIP) + (c + STAMP + SLIP)


def min_commission_penalty(turnover: float, ticket: float) -> float:
    """「最低佣金惩罚」：相对纯费率口径**多付**的年化 pp。

    `turnover` = 年单边换手（次/年）；`ticket` = 单笔金额（元）。
    """
    return turnover * (round_cost(ticket) - MODELED_ROUND) * 100.0
