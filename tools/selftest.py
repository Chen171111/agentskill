"""快速回归自检 —— **改动前后都跑一遍**（默认 < 60 秒，不跑长回测）。

为什么需要它
------------
本项目反复吃同一类亏，而每一次都是**事后偶然发现**的：

- ETF 份额折算被每日刷新**静默回退**（代价 0.98pp/年）
- 回撤熔断的滞回状态机失效（dd 回到 10~15% 就解除，「≤10% 才恢复」从未生效）
- NaN 行情价穿透全部校验（nan 的比较运算全部返回 False）
- 停牌持仓被按 0 元计价 → 假回撤误触发熔断
- 多笔买单各按交易前现金核算 → 超配现金

它们的共同点：**破坏是静默的，而发现靠运气**。
本脚本把"靠运气"换成"一条命令"。

> 2026-09-29：个股多因子研究线已迁至独立工作区 `quant2`，其专属检查
> （成本单一来源 / `--adj-mode` 口径 / 涨跌停分档 / 个股数据新鲜度 / M1~M4 自优化）
> 随线迁移，由 quant2 的 selftest 覆盖。本文件只保留 **ETF 线与交易链路**检查。

检查项
------
| 组 | 内容 |
|---|---|
| 复权 | `THRESH` 四处实现值一致（且等于 `dataprovider.adjust.THRESH`） |
| 稳定性 | 下单脚本拒绝未知参数；`daily_job` 重试窗口限在收盘前；不硬编码本仓库绝对路径；可执行脚本有 `__main__` 守卫 |
| 文档 | `audit_doc_commands` 四类问题全 0 |
| CLI | `argparse` 的 `help`/`description` 里没有「裸 `%`」（否则 `--help` 直接崩，且是静默的） |
| 风控 | 回撤熔断滞回状态机；行情价 NaN/inf 入口闸门 + 熔断状态跨运行持久化 |
| 回测 | 停牌持仓按最后已知价计价，不按 0 元（防假回撤误触发熔断） |
| 执行 | 多笔买单不超配现金（卖出回款计入预算，逐单扣减） |
| 券商读取 | 同花顺 OCR 解析五项判据（持仓行不丢 / 幽灵放行 / 当前页过滤 / 池外定价 / mark 持久化） |

用法
----
    $PY tools/selftest.py              # 全部（约 30~60 秒）
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_CHECKS: list[tuple[str, str, bool]] = []   # (组, 名称, 是否需要重数据)


def check(group: str, name: str, heavy: bool = False):
    """注册一个检查。被装饰函数返回值会被忽略；抛异常 = 失败。"""
    def deco(fn):
        _CHECKS.append((group, f"{name}", heavy))
        fn._selftest = (group, name, heavy)
        return fn
    return deco


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
@check("稳定性", "会下单的脚本必须拒绝未知参数（防「--help 触发交易」）")
def _trade_script_arg_guard():
    """⚠️ 2026-09-19 新增，起因是一次**真实事故**。

    `tools/daily_job.py` 会真的走下单链路（内部执行 `main.py simulate --ths`）。
    它原本对参数**不做任何检查** —— 于是 `daily_job.py --help` **不会打印用法**，
    而是**照常执行整条交易链路**：

        2026-09-19 23:57 有人用 `--help` 试探 → 触发一次真实运行 →
        失败在 broker 连接阶段（**未产生任何交易**，`trading.db` 未动），
        但覆盖了 `state/last_run.json` 并写出 `state/ALERT.txt`
        → `check_alerts.py` 误报「自动交易失败」。

    这条检查要求：**未知参数必须被拒绝执行**（白名单 `_ALLOWED_ARGS`），
    而且守卫必须出现在**构造下单命令之前** —— 否则等于没有守卫。
    """
    src = open(os.path.join(ROOT, "tools", "daily_job.py"), encoding="utf-8").read()
    assert "_ALLOWED_ARGS" in src, "daily_job.py 缺少参数白名单 _ALLOWED_ARGS"
    assert "--manual" in src and "--simulate-failure" in src, (
        "daily_job.py 的白名单必须至少含 --manual 与 --simulate-failure")
    i_guard = src.find("unknown = [a for a in sys.argv[1:]")
    i_trade = src.find("argv = [sys.executable")
    assert i_guard != -1, "daily_job.py 缺少未知参数守卫（unknown = ... 那一段）"
    assert i_trade != -1, "daily_job.py 找不到下单 argv 的构造处"
    assert i_guard < i_trade, (
        "参数守卫必须写在构造下单命令**之前**（否则 --help 照样会触发真实交易）")


@check("稳定性", "daily_job 的连接重试必须限窗（收盘后不得重试）")
def _daily_job_retry_window():
    """⚠️ 2026-09-20 新增（D5）。

    「同花顺没开」类失败会重试；但**收盘后重试毫无意义**（没法按当日价成交），
    还平白增加「重复下单」的风险 → 必须有窗口上限；
    且**只重试连接类失败**（别的失败重试不会变好，只会用重试掩盖真问题）。
    """
    src = open(os.path.join(ROOT, "tools", "daily_job.py"), encoding="utf-8").read()
    assert "_RETRY_WINDOW_END" in src and "_in_retry_window" in src, (
        "daily_job.py 缺少重试窗口上限（收盘后还会重试 = 危险）")
    assert "_RETRY_KEYWORDS" in src and "_is_conn_failure" in src, (
        "daily_job.py 缺少「只重试连接类失败」的判定")
    m = re.search(r"_RETRY_WINDOW_END\s*=\s*\((\d+)\s*,\s*(\d+)\)", src)
    assert m, "解析不出 _RETRY_WINDOW_END 的值"
    assert (int(m.group(1)), int(m.group(2))) < (15, 0), (
        "重试窗口上限 {}:{} 不得晚于 15:00 收盘".format(m.group(1), m.group(2)))


@check("稳定性", "代码里不许硬编码【本仓库】的绝对路径（公开仓库的可复现性）")
def _no_hardcoded_repo_path():
    """⚠️ 2026-09-21 新增。

    仓库是**公开**的、且对外承诺「clone 下来能跑」。代码里写死
    `ROOT = r"E:\\MyWorkAndProject\\量化\\agentskill"` 在本机能跑，
    换台机器就指向不存在的目录 —— 而且**比"报错"更糟**：
    它通常表现为「静默用了错误的路径」或「明明算了 ROOT 又被覆盖」。

    实测踩过（2026-09-21 自检发现 **2 个文件**）：
    `tools/verify_div_tax.py` / `tools/faber_binding_diag.py` —— 两份都是
    先算好了 `ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`，
    **后面又被一行 `ROOT = r"E:\\..."` 覆盖掉**，还附带重复的 `import os/sys`。

    ⚠️ **只查代码，不查 docstring** —— 用法示例里写作者机器的路径是**说明性**的，
    不参与运行（`tools/README.md` 顶部也明说了 PY 是作者环境）。
    做法：AST 里把「模块/函数/类的首条字符串语句」（即 docstring）排除掉。
    ⚠️ **放行解释器路径**（`E:\\Python` / `E:\\Python32`）—— 那是**运维说明**，
    本项目的三 Python 分工是硬约束，不属"仓库位置"。
    """
    import ast
    import re

    repo = os.path.basename(ROOT)                       # 例：agentskill
    pat = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s'\"]*" + re.escape(repo), re.I)
    allow = re.compile(r"[A-Za-z]:[\\/](?:Python|Python32|Python3\d*)[\\/]", re.I)

    def docstring_ids(tree):
        ids = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.Module, ast.FunctionDef,
                              ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(n, "body", None)
                if body and isinstance(body[0], ast.Expr) \
                        and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    ids.add(id(body[0].value))
        return ids

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
        ds = docstring_ids(tree)
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                    and id(n) not in ds:
                if pat.search(n.value) and not allow.search(n.value):
                    bad.append(f"tools/{fn}:{n.lineno}  {n.value[:60]!r}")
    assert not bad, ("以下代码里硬编码了本仓库的绝对路径（换机器即失效；"
                     "用法示例请放进 docstring）：\n  " + "\n  ".join(bad))


@check("稳定性", "可执行脚本必须有 `if __name__ == \"__main__\"` 守卫")
def _exec_scripts_have_main_guard():
    """⚠️ 2026-09-21 新增，起因是一次实测（见 HANDOFF §5.1 第 21 条）。

    `tools/` 下有脚本把**全部工作写在模块层**，既没有 `main()` 也没有 `__main__` 守卫。
    后果有两条，都是静默的：
    1. **`--help` 不会打帮助，而是直接开跑**（实测 `verify_div_tax.py` 27 秒仍未返回）；
    2. **`import tools.xxx` 会触发一次完整回测**（隐性副作用）。

    **怎么区分"可执行脚本"与"库模块"**：用**是否被别人 import** 自动推导 ——
    被 `from tools.X import ...` / `import tools.X` 引用过的是库模块，**不要求**守卫；
    其余一律视为可执行脚本，**必须有**守卫。这样白名单不用手工维护。
    """
    import ast
    import re

    tdir = os.path.join(ROOT, "tools")
    names = [f for f in os.listdir(tdir) if f.endswith(".py")]
    # 全仓库扫「谁被 import 了」——范围含仓库根、dataprovider/、trader/，避免漏判
    roots = [ROOT, os.path.join(ROOT, "tools"),
             os.path.join(ROOT, "dataprovider"), os.path.join(ROOT, "trader")]
    imported = set()
    for d in roots:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if not f.endswith(".py"):
                continue
            try:
                s = open(os.path.join(d, f), encoding="utf-8").read()
            except OSError:
                continue
            imported.update(re.findall(
                r"(?:from|import)\s+tools\.([A-Za-z_][A-Za-z0-9_]*)", s))

    bad = []
    for fn in sorted(names):
        if fn[:-3] in imported:                 # 库模块，豁免
            continue
        tree = ast.parse(open(os.path.join(tdir, fn), encoding="utf-8").read())
        guard = any(isinstance(n, ast.If) and "main" in ast.dump(n.test)
                    for n in tree.body)
        if not guard:
            bad.append(fn)
    assert not bad, (
        "这些可执行脚本没有 `if __name__ == \"__main__\":` 守卫"
        "（`--help` 会直接开跑、`import` 有副作用）：\n  " + "\n  ".join(bad))


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


# ============================ 风控（组合级） ============================
@check("风控", "回撤熔断滞回：进入后 dd 回到 10~15% 应保持降仓，≤10% 才恢复")
def _dd_circuit_hysteresis():
    """2026-09-29 修复回归（原滞回带完全失效）。

    原实现 active 期间把 dd 直接喂 levels 循环，而 levels 最低档就是 15% →
    dd 落回 (10%,15%) 时循环落空返回 1.0，熔断立即解除，「≤10% 才恢复」
    从未生效（回撤在阈值附近往返 → 仓位在 0.65/1.0 间反复开关）。
    走公开 API `scale(nav_history)` 断言，顺便覆盖 dd 计算与状态转换；
    实盘侧的状态跨运行持久化由 runner 的 dd_circuit_active 负责（见下一条）。
    """
    from risk.portfolio import PortfolioRisk
    pr = PortfolioRisk(dd_circuit=True, vol_target=None)     # 隔离波动率目标
    assert pr.scale([100.0, 84.0]) == 0.65, "dd=16% 应进入熔断（0.65 档）"
    assert pr.scale([100.0, 84.0, 88.0]) == 0.65, \
        "dd=12% 在滞回带内，应保持 0.65（原实现在此解除熔断）"
    assert pr.scale([100.0, 84.0, 88.0, 89.0]) == 0.65, "dd=11% 仍在带内"
    assert pr.scale([100.0, 84.0, 88.0, 89.0, 91.0]) == 1.0, \
        "dd=9% ≤10% 才恢复满仓"
    assert pr.scale([100.0, 84.0, 88.0, 89.0, 91.0, 89.0]) == 1.0, \
        "恢复后 dd=11% 未再触 15% 阈，不得重入"
    assert pr.scale([100.0, 84.0, 88.0, 89.0, 91.0, 89.0, 85.0]) == 0.65, \
        "dd=15% 重新触发熔断"
    pr2 = PortfolioRisk(dd_circuit=True, vol_target=None)
    assert pr2.scale([100.0, 70.0]) == 0.0, "dd=30% 应落 0.0 清仓档"
    assert pr2.scale([100.0, 70.0, 79.0]) == 0.40, "dd=21% 应落 0.40 档"


@check("风控", "行情价 NaN/inf 入口闸门 + 熔断状态跨运行持久化")
def _runner_finite_and_circuit_state():
    """2026-09-29：nan 的比较运算全部为 False → calc=nan 时 _pos_ok 五个判据
    全"通过"、止损/熔断静默失效、nan 成本价写进账本（检验时实测复现）。
    runner 必须在 prices 入口拒绝非有限值。行为回归见 tools/test_pos_ok.py
    场景 8（须 32 位）；这里只做 64 位可跑的结构断言。
    另：熔断滞回状态必须存 DB state（dd_circuit_active），否则实盘每次新建
    RiskManager 实例、滞回带在实盘从未生效。
    """
    src = open(os.path.join(ROOT, "scheduler", "runner.py"), encoding="utf-8").read()
    assert "isfinite" in src and "NaN/inf" in src, \
        "runner 必须在 prices 入口加 isfinite 闸门（否则 nan 穿透全部校验）"
    assert "dd_circuit_active" in src, \
        "熔断滞回状态必须跨运行持久化（DB state dd_circuit_active）"
    msrc = open(os.path.join(ROOT, "risk", "manager.py"), encoding="utf-8").read()
    assert "set_circuit_active" in msrc and "circuit_active" in msrc, \
        "RiskManager 必须暴露熔断状态的存取方法（供 runner 持久化）"


@check("回测", "停牌持仓按最后已知价计价，不按 0 元（防假回撤误触发熔断）")
def _backtest_suspension_valuation():
    """2026-09-29：`engine._row` 会把 NaN（停牌）从行里剔除，原
    `market_value` 的 `prices.get(c, 0.0)` 让停牌持仓当日按 **0 元**计：
      · 净值凭空蒸发该持仓全额 → 假回撤，可能误触发 15% 回撤熔断；
      · `trade()` 的 total 被低估 → 调仓日目标市值算小、买入偏少。
    停牌只是"没有新报价"，不是"市值归零"。交易路径（要求当日价 > 0）
    不受影响——没有实时价确实不能撮合。
    ⚠️ 本修复会**改动研究数值**（停牌日净值不再假跌），历史回测对比需重跑。
    """
    from backtest.account import BacktestAccount
    acc = BacktestAccount(init_cash=100000.0)
    acc.positions = {"600000.SH": {"qty": 1000, "cost": 10.0, "peak": 10.0}}
    assert acc.market_value({"600000.SH": 11.0}) == 11000.0, "正常日应按当日价"
    assert acc.market_value({}) == 11000.0, \
        "停牌（行内无该 code）被按 0 元计价 → 假回撤"
    assert acc.market_value({"600000.SH": float("nan")}) == 11000.0, \
        "显式 NaN 也要兜底（NaN 是 truthy，`if px` 挡不住）"
    acc.positions["000001.SZ"] = {"qty": 100, "cost": 5.0, "peak": 5.0}
    assert acc.market_value({}) == 11000.0, "从未知道价的标的才落 0"
    acc.mark_to_close("20260929", {})            # 停牌日记账不得蒸发
    assert acc._equity[-1] == 111000.0, acc._equity[-1]


@check("执行", "多笔买单不超配现金（卖出回款计入预算，逐单扣减）")
def _execution_cash_budget():
    """2026-09-29：真实券商 submit() 只返回 submitted、不当场成交，
    `account.cash` 在整批订单生成期间不会变 —— 原实现多笔买单都按交易前
    现金核算会**超配**（broker 拒单/部分成交，策略目标失真；PaperBroker
    甚至会把现金打成负数）。修法：cash_left 逐单扣减 + 卖出回款按 A 股
    当日可用计入预算（先卖后买的提交顺序保证这一点）。
    """
    from account.portfolio import PortfolioAccount
    from trader.execution import ExecutionEngine
    # 场景 A：满仓现金、两个目标各 90% → 买入总额不得超现金（旧实现会按
    # 交易前现金给每只都下 90% 的大单，合计 180%，PaperBroker 打成负现金）
    acc = PortfolioAccount(init_cash=50000.0)
    prices = {"513100.SH": 2.0, "513500.SH": 4.0}
    orders = ExecutionEngine().rebalance(
        acc, {"513100.SH": 0.9, "513500.SH": 0.9}, prices)
    buys = [o for o in orders if o.side == "buy"]
    spent = sum(o.qty * o.price for o in buys)
    assert spent <= 50000.0 * 1.001, \
        "买入总额 {:.0f} 超现金 50000（多笔超配）".format(spent)
    assert acc.cash >= 0, "现金被超配成负数：{:.2f}".format(acc.cash)
    # 场景 B：持仓可卖 → 回款计入预算，两只都成交且总额不超预算
    # （prices 必须覆盖持仓标的——生产里 prices 就是整个策略池，与执行路径一致）
    acc2 = PortfolioAccount(init_cash=10000.0)
    acc2.positions = {"510880.SH": {"qty": 20000, "cost": 3.0,
                                    "peak": 3.0, "mark": 3.0}}
    prices2 = {"510880.SH": 3.0, "513100.SH": 2.0, "513500.SH": 4.0}
    orders2 = ExecutionEngine().rebalance(
        acc2, {"513100.SH": 0.9, "513500.SH": 0.9}, prices2)
    buys2 = [o for o in orders2 if o.side == "buy"]
    assert len(buys2) == 2, "卖出回款应让两只都成交，实际 {} 笔".format(len(buys2))
    budget = 10000.0 + 20000 * 3.0
    spent = sum(o.qty * o.price for o in buys2)
    assert spent <= budget * 1.001, "买入总额 {:.0f} 超预算 {:.0f}".format(spent, budget)
    assert acc2.cash >= 0, "现金被超配成负数：{:.2f}".format(acc2.cash)


# ============================ 券商读取（同花顺） ============================
# 2026-09-21 事故复盘：14:50 自动交易被拒单（持仓/资金读取未通过校验，全天零成交）。
# 拆开看是**三个互相耦合的静默缺陷**同时发作，因此这里三个方向各留一条常驻断言。
@check("券商读取", "持仓/成交行解析：名称被 OCR 读成拉丁串时不得丢行")
def _ths_parse_fixture():
    """真实夹具回归（`tools/test_ths_parse.py`，纯离线、无 win32 依赖）。

    事故：601091「C沈鼓」（新股『C』前缀）被 OCR 读成 'Cit'，旧守卫
    「行内必须含汉字」把整行持仓静默丢弃 → 只读到 1 只 → 偏差 95.61% 拒单。
    """
    from tools.test_ths_parse import regression_all
    n = regression_all()
    print(f"      {n} 项真实 OCR 夹具断言通过（含 601091 2400 股 / 90,528 元那行）")


@check("券商读取", "判据 4：市值自洽 <2% 时本地多余持仓按幽灵放行（防小幽灵死锁）")
def _pos_ok_ghost_branch():
    """事故（2026-09-23~28，交易线锁死 4 个交易日）：判据 4 原用固定
    `mv×1.05` 容差区分漏读/真清仓，本地幽灵 510880 占 mv 的 4.7% < 5%
    → `calc+missing <= mv×1.05` 恒成立 → 永远判「漏读」→ 永久死锁，
    且重试无效（确定性坏帧，4 连拍得到 4 次相同拒单）。
    修订（2026-09-28，主人拍板）：calc 与 mv 自洽（<2%）即认定券商侧读全
    → 本地多余持仓按幽灵处理、允许覆盖；差额 ≥2% 才回到市值反证。
    ⚠️ 数值回归（真数字 7 场景）在 `tools/test_pos_ok.py`，需 32 位 Python
    （import trader.ths_uia 依赖 win32）；这里只做 64 位可跑的结构断言，
    防有人把幽灵分支回退掉、把死锁带回来。
    """
    src = open(os.path.join(ROOT, "trader", "ths_uia.py"), encoding="utf-8").read()
    assert "def _pos_calc(" in src and "def _missing_positions(" in src, \
        "calc/missing 必须有共用 helper（铁律 14：同一判据不允许两处实现）"
    m = re.search(r"def _pos_ok\(self.*?\n(.*?)\n    def ", src, re.S)
    assert m, "找不到 _pos_ok 定义（是否被改名/移动？）"
    body = m.group(1)
    assert "0.02" in body, \
        "判据 4 必须有「calc 与 mv 自洽 <2% → 幽灵放行」分支（否则小幽灵死锁复发）"
    assert "mv * 1.05" in body, \
        "差额 ≥2% 时必须保留市值反证兜底（calc+missing <= mv×1.05 判漏读拒单）"
    assert "_missing_positions(pos, old, prices)" in body, \
        "判据 4 必须走 _missing_positions helper"


@check("券商读取", "资金栏控件按「当前显示页」过滤（防读隐藏页陈旧值）")
def _ths_balance_ctrl_scope():
    """事故：`_read_static` 取 `vals[-1]`（枚举顺序最后一个同名 ID 实例），
    读到隐藏页陈旧值 —— 总资产 219,157.24（真实 190,912.44）、
    股票市值 121,595.20（真实 93,350.40），对账偏差 95.61% 直接拒单。
    真实值可由持仓表自证：95,944.00 / 190,912.44 = 50.26% == 表格「仓位占比」汇总。
    """
    src = open(os.path.join(ROOT, "trader", "ths_uia.py"), encoding="utf-8").read()
    m = re.search(r"def _read_static\(self, cid([^)]*)\)", src)
    assert m, "找不到 _read_static 定义（是否被改名？）"
    assert "prefer_page" in m.group(1), \
        "_read_static 必须带 prefer_page（否则会读到隐藏页同名控件的陈旧值）"
    assert "_page_label_of" in src and "on_page" in src, \
        "_read_static 必须按「所属页面」把当前显示页的实例排到最前"
    assert "prefer_page=page" in src, "fetch_balance 必须把当前页标签传给 _read_static"


@check("券商读取", "池外持仓可被定价（mark 兜底，防总资产虚低）")
def _pool_out_position_priced():
    """事故：`prices` 只有策略池 11 只 ETF，账户里**人工买入的个股**按 0 元计价 →
    总资产 190,912.44 被算成 102,904.44（虚低 46%）→ 净值序列被污染、
    仓位按虚低的 equity 计算、回撤熔断会把「净值腰斩」误判成巨亏而清仓。
    """
    from account.portfolio import PortfolioAccount
    acc = PortfolioAccount(init_cash=97562.04)
    acc.positions = {
        "510880.SH": {"qty": 1600, "cost": 3.424, "peak": 3.5, "mark": 3.385},
        "601091.SH": {"qty": 2400, "cost": 41.673, "peak": 41.7, "mark": 37.720},
    }
    prices = {"510880.SH": 3.339}          # 池内有行情价；601091 是池外人工持仓
    mv = acc.market_value(prices)
    exp = 1600 * 3.339 + 2400 * 37.720
    assert abs(mv - exp) < 1e-6, f"持仓市值错：{mv} vs 期望 {exp}"
    eq = acc.total_equity(prices)
    assert eq > 150000.0, f"总资产被低估为 {eq}（应约 193,432）"
    snap = acc.snapshot(prices)
    got = {p["code"]: p["price"] for p in snap["positions"]}
    assert abs(got["601091.SH"] - 37.720) < 1e-9, f"快照里池外标的价错：{got}"


@check("券商读取", "positions.mark 可持久化（新库建列 + 旧库自动迁移）")
def _positions_mark_roundtrip():
    """mark 必须能跨运行存活：否则重启后池外持仓又变 0 元，总资产再次虚低。"""
    import tempfile
    import sqlite3
    from storage.db import TradeDB
    with tempfile.TemporaryDirectory() as d:
        db = TradeDB(db_path=os.path.join(d, "new.db"))
        db.save_positions({"601091.SH": {"qty": 2400, "cost": 41.673,
                                         "peak": 41.7, "mark": 37.72}})
        got = db.load_positions()
        assert abs(got["601091.SH"]["mark"] - 37.72) < 1e-9, got
        # 旧库（positions 无 mark 列）→ 自动 ALTER 迁移后仍可读写
        p = os.path.join(d, "old.db")
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE positions (code TEXT PRIMARY KEY, qty INTEGER, "
                  "cost REAL, peak REAL)")
        c.execute("INSERT INTO positions VALUES ('510880.SH', 1600, 3.424, 3.5)")
        c.commit()
        c.close()
        db2 = TradeDB(db_path=p)
        got2 = db2.load_positions()
        assert got2["510880.SH"]["qty"] == 1600, got2
        assert got2["510880.SH"]["mark"] == 0.0, f"旧行 mark 应为 0：{got2}"
        db2.save_positions(got2)
        assert db2.load_positions()["510880.SH"]["qty"] == 1600


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
