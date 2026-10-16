"""2026-10-15 P0 修复验证（只读诊断，不修改任何数据/生产代码路径）。

背景（GPT 质检发现 + 人工复核证实）：
  A. quant 历史选股前视：store.read() 原先「全量复权 → 算 rate → 才按 end
     截断」，截止日之后的份额折算把折算比例乘到截止日之前的历史价格上
     （adjust.repair_splits 对跳变日之前整段价格乘未来比率）。多因子策略
     的 macd_hist 按绝对价差计算，对该缩放敏感 → 历史选股被未来信息改变。
  B. quant 单只止损误清仓：engine 非调仓日只传 {止损标的: 0}，
     account.trade 把未列出持仓也当成零目标卖出 → 一只止损、整组清仓。

本脚本用仓库真实数据（A）与内存合成数据（B）复现并验证修复效果。
运行：E:/Python32/python.exe tools/verify_p0_fix_20261015.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import config
from dataprovider.store import DataStore
from dataprovider.panel import build_panel, Panel
from dataprovider.adjust import scan_jumps
from factors.engine import compute_factors
from pipeline import DEFAULT_FACTORS
from strategies.registry import create_strategy
from backtest.engine import BacktestEngine
from backtest.account import BacktestAccount

POOL = config.RECOMMENDED_POOLS["科技进攻"]
END = "20251231"
WARMUP = "20150101"

A_CODE, B_CODE = "510300.SH", "510880.SH"


def _factors_for(panel):
    return compute_factors(panel, DEFAULT_FACTORS)


def part_a():
    print("=" * 78)
    print("A. 历史选股前视复现（multifactor 策略，topk=3，截止 {}）".format(END))
    print("=" * 78)
    store = DataStore()
    store.ensure(POOL)
    raw_store = DataStore(repair_splits=False)   # 未复权原始数据，用于扫描未来折算

    # 截止日之后发生的份额折算（未来信息源）—— 先列出来，证明污染真实存在
    # ⚠️ 必须扫**未复权**原始序列：复权是幂等的，修好后再扫不到跳变
    print("\n[1] 各标的数据中截止日之后的跳变（>35% 判据，即复权器会修的事件）:")
    any_future = False
    for c in POOL:
        df = raw_store.read(c)                   # 全量未复权
        jumps = scan_jumps(df)
        fut = [j for j in jumps if j[0] > END]
        if fut:
            any_future = True
            for dt, pc, cl, ret in fut:
                print("    {}  {}  {:.4f}->{:.4f}  {:+.1%}".format(c, dt, pc, cl, ret))
    if not any_future:
        print("    （无 —— 截止日之后没有折算，两条路径应无差异）")

    # 新路径（PIT）：read(end=END) 内部先截断再复权
    p_new = build_panel(store, POOL, start=WARMUP, end=END)
    f_new = _factors_for(p_new)
    strat = create_strategy("multifactor", topk=3, rebalance_every=1)
    d_last = p_new.dates[-1]
    w_new = strat.generate_weights(d_last, f_new, p_new)

    # 旧路径仿真：全量读取（复权作用于全量）→ 事后按 END 截断
    p_full = build_panel(store, POOL, start=WARMUP, end=None)
    f_full = _factors_for(p_full)
    p_old = p_full.slice(None, END)
    f_old = {k: v.reindex(p_old.dates) for k, v in f_full.items()}
    strat2 = create_strategy("multifactor", topk=3, rebalance_every=1)
    w_old = strat2.generate_weights(d_last, f_old, p_old)

    sel_new, sel_old = set(w_new or {}), set(w_old or {})
    # 排名顺序：直接用策略打分序列排序，复现「前三名/第三名」口径
    from strategies.base import weighted_score
    from strategies.builtin import MultiFactorStrategy
    specs = MultiFactorStrategy.DEFAULT_SPECS
    rk_new = weighted_score(f_new, d_last, specs).sort_values(ascending=False)
    rk_old = weighted_score(f_old, d_last, specs).sort_values(ascending=False)
    print("\n[2] {} 选股结果（topk=3）:".format(d_last))
    print("    修复后 PIT 路径 前三: {}".format(rk_new.head(3).index.tolist()))
    print("    旧  全量后截断 前三: {}".format(rk_old.head(3).index.tolist()))
    print("    512480.SH in PIT前三 = {} | in 旧路径前三 = {}".format(
        "512480.SH" in rk_new.head(3).index, "512480.SH" in rk_old.head(3).index))
    print("    588170.SH in PIT前三 = {} | in 旧路径前三 = {}".format(
        "588170.SH" in rk_new.head(3).index, "588170.SH" in rk_old.head(3).index))
    same = sel_new == sel_old
    print("    两条路径选股一致 = {}".format(same))
    if same and any_future:
        print("    ⚠️ 有未来折算但选股未变 —— 需检查该折算是否影响池内标的")
    return sel_new, sel_old


class _FakeStrat:
    """合成策略：只在指定日期返回目标权重，其余日子返回 None（非调仓日）。"""

    stops_enabled = True

    def __init__(self, plan):
        self.plan = plan

    def generate_weights(self, date, factors, panel):
        return self.plan.get(date)


def _mk_panel():
    dates = ["20250102", "20250103", "20250106", "20250107", "20250108"]
    px = {
        A_CODE: [1.00, 1.00, 0.90, 0.90, 0.91],   # 20250103 收盘跌破 -8% 止损线
        B_CODE: [2.00, 2.00, 2.00, 2.00, 2.00],
    }
    op = {c: list(v) for c, v in px.items()}
    close = pd.DataFrame(px, index=dates)
    open_ = pd.DataFrame(op, index=dates)
    vol = pd.DataFrame({c: [1000] * len(dates) for c in px}, index=dates)
    return Panel({"close": close, "open": open_, "volume": vol},
                 list(px), {c: "etf" for c in px})


def part_b():
    print()
    print("=" * 78)
    print("B. 单只止损误清仓复现（内存合成数据，2 只 ETF 等权）")
    print("=" * 78)
    panel = _mk_panel()
    plan = {"20250102": {A_CODE: 0.5, B_CODE: 0.5}}   # 首日调仓建仓
    eng = BacktestEngine(panel, {}, _FakeStrat(plan), init_cash=100000.0,
                         benchmark=None, timing=None, dd_circuit=False,
                         vol_target=None)
    res = eng.run()
    pos_after = {c: p["qty"] for c, p in eng.acc.positions.items() if p["qty"] > 0}
    print("\n[1] 修复后引擎跑完的持仓: {}".format(pos_after))
    ok_a = A_CODE not in pos_after
    ok_b = pos_after.get(B_CODE, 0) > 0
    print("    A({}) 已被止损强平 = {}".format(A_CODE, ok_a))
    print("    B({}) 持仓保留     = {}".format(B_CODE, ok_b))

    # 对照：旧 engine 路径产生的 pending={A:0} 直接喂给 account.trade 会怎样
    acc = BacktestAccount(init_cash=100000.0)
    acc.trade({A_CODE: 0.5, B_CODE: 0.5}, {A_CODE: 1.0, B_CODE: 2.0})
    held = {c: p["qty"] for c, p in acc.positions.items()}
    acc.trade({A_CODE: 0.0}, {A_CODE: 0.90, B_CODE: 2.0})   # 旧路径的 pending
    held2 = {c: p["qty"] for c, p in acc.positions.items() if p["qty"] > 0}
    print("\n[2] 旧路径危害对照（account.trade 收到 {{A:0}}）:")
    print("    交易前持仓: {}".format(held))
    print("    交易后持仓: {}  ← B 也未列出，被当成零目标一并卖出".format(held2))
    old_bug = B_CODE not in held2
    print("    旧路径会误清 B = {}".format(old_bug))
    return ok_a and ok_b and old_bug


if __name__ == "__main__":
    part_a()
    part_b()
    print("\n完成。")
