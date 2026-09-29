# -*- coding: utf-8 -*-
"""`_pos_ok` 判据 4 的数值回归（**真数字场景，需 32 位 Python**）。

为什么单独一个文件（而不是并进 `test_ths_parse.py`）
------------------------------------------------
`_pos_ok` 住在 `trader/ths_uia.py`，模块顶层 `import win32gui/pywinauto`
→ 只有 32 位 Python（交易线同款解释器）能 import。`test_ths_parse.py` 是
纯解析层（无 win32 依赖，64 位可跑）。两者分工不同，**别合并**。

事故背景（2026-09-23 ~ 09-28，交易线锁死 4 个交易日）
----------------------------------------------------
判据 4 原用「把消失的持仓加回去是否超出 mv×1.05」区分漏读/真清仓。
本地幽灵持仓 510880（09-22 卖出未落账）市值 5,371 只占 mv 的 4.7% < 5%
→ `calc+missing <= mv×1.05` 恒成立 → 永远判「漏读」→ **永久死锁**，
重试无效（同一帧是确定性结果，4 连拍只会得到 4 次相同的拒单）。

修订（2026-09-28，主人拍板）
--------------------------
新规则先问「读到的持仓」能否解释资金栏市值，再追究差额：
  ① calc 与 mv 自洽（<2%）→ 券商侧读全 → 本地多余持仓按**幽灵**处理、允许覆盖；
  ② 差额 ≥ 2%（确有东西没读到）→ 回到市值反证：
     `calc + missing <= mv×1.05` 判漏读并拒单。

本文件把 8 个场景钉死防回归：
  1. 幽灵 4.6% < 5%（09-28 实况口径）     → 必须放过（**旧规则在此死锁**）
  2. 幽灵 78% > 5%（池外个股）            → 必须放过（旧规则也放过，防改坏）
  3. 真清仓（09-14 实测口径）             → 必须放过
  4. 漏读 2.9%（≥2%）                    → 必须拒单（市值反证兜底仍有效）
  5. 漏读 1.5%（<2%）                    → 按幽灵放过（**已知代价**，记录在案）
  6. 判据 3 的 5% 闸门（qty=500 串位帧）  → 必须拒单
  7. 无消失持仓（含 qty=0 旧行）          → 必须放过
  8. 行情价 NaN/inf（09-29 新增）        → 必须拒单（nan 比较全 False 会穿透全部判据）

用法:
    E:\\Python32\\python.exe tools/test_pos_ok.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trader.ths_uia import UiaThsBroker   # noqa: E402

# 绕过 __init__（不连客户端）：_pos_ok 只用 _mk_price/_guess_full_code，
# 均为无实例状态的方法（2026-09-14 同款用法）。
B = UiaThsBroker.__new__(UiaThsBroker)

# ---- 场景 1 fixture：2026-09-28 实况口径（券商真值 = 513100 + 513500） ----
# positions 最终状态 @2.319 / @2.736；资金栏 mv=115,778.0（自洽 0.17%，
# 与当日对账「市值偏差 0.17%」一致）；幽灵 510880 1600 股 @3.357 = 5,371.2，
# 占 mv 的 4.64% < 5% —— 旧规则的死锁区。
P_LIVE = {
    "513100.SH": 2.319,
    "513500.SH": 2.736,
    "510880.SH": 3.357,
}
POS_LIVE = {
    "513100": {"qty": 25000, "available": 25000, "frozen": 0,
               "cost": 2.30, "price": 2.319},
    "513500": {"qty": 21200, "available": 21200, "frozen": 0,
               "cost": 2.70, "price": 2.736},
}
OLD_LIVE = {
    "513100.SH": {"qty": 25000, "cost": 2.30, "peak": 2.4, "mark": 2.319},
    "513500.SH": {"qty": 21200, "cost": 2.70, "peak": 2.8, "mark": 2.736},
    "510880.SH": {"qty": 1600, "cost": 3.424, "peak": 3.5, "mark": 3.357},
}
BAL_LIVE = {"cash": 74776.0, "market_value": 115778.0}


def check_ghost_small_deadlock_zone():
    """场景 1（核心回归）：幽灵占比 4.6% < 5% 必须放过。

    旧规则在此必死锁：calc+missing = 115,978.20 + 5,371.20 = 121,349.40
    <= mv×1.05 = 121,566.90 → 永远判「漏读」→ 每天拒单。先断言死锁条件
    确实成立（确认夹具踩在旧规则雷区里），再断言新规则放过。
    """
    calc = B._pos_calc(POS_LIVE, P_LIVE)
    missing, ghosts = B._missing_positions(POS_LIVE, OLD_LIVE, P_LIVE)
    assert len(ghosts) == 1 and ghosts[0][0] == "510880.SH", ghosts
    assert abs(missing - 5371.2) < 1e-6, missing
    assert missing / BAL_LIVE["market_value"] < 0.05, \
        "夹具必须踩在 5% 死锁区内（否则测不到修订点）"
    assert calc + missing <= BAL_LIVE["market_value"] * 1.05, \
        "旧规则死锁条件应成立（否则本场景测不到修订点）"
    assert B._pos_ok(POS_LIVE, BAL_LIVE, P_LIVE, OLD_LIVE), \
        "幽灵 4.6% < 5% 必须按幽灵放行（旧规则在此永久死锁）"


def check_ghost_large():
    """场景 2：幽灵 601091 2400 股（池外，靠 mark 计价）= 90,528，占 mv 78%。

    旧规则也放过（calc+missing 远超 mv×1.05）—— 防本次修订把原本正确的
    「大幽灵放行」行为改坏。
    """
    old = dict(OLD_LIVE)
    old["601091.SH"] = {"qty": 2400, "cost": 41.673, "peak": 41.7, "mark": 37.72}
    assert B._pos_ok(POS_LIVE, BAL_LIVE, P_LIVE, old)


def check_true_sell():
    """场景 3：真清仓（2026-09-14 实测口径，合计数照抄当时诊断输出）。

    calc=5,470（510880 1600×3.419）+ missing=47,710（511010+518880 已卖光）
    = 53,180 ≫ mv×1.05=5,713.68 → 市值反证判「真清仓」放过。
    （missing 两只的拆分只为结构复现，合计数与 09-14 实测一致。）
    """
    pos = {"510880": {"qty": 1600, "available": 1600, "frozen": 0,
                      "cost": 3.424, "price": 3.419}}
    old = {
        "510880.SH": {"qty": 1600, "cost": 3.424, "peak": 3.5, "mark": 3.419},
        "511010.SH": {"qty": 300, "cost": 140.0, "peak": 141.0, "mark": 115.7},
        "518880.SH": {"qty": 4000, "cost": 3.3, "peak": 3.4, "mark": 3.25},
    }
    prices = {"510880.SH": 3.419, "511010.SH": 115.7, "518880.SH": 3.25}
    bal = {"cash": 5000.0, "market_value": 5441.6}
    missing, _ = B._missing_positions(pos, old, prices)
    assert abs(missing - 47710.0) < 1e-6, missing      # 与 09-14 实测一致
    assert B._pos_ok(pos, bal, prices, old)


def check_missed_read_over_2pct():
    """场景 4：漏读 2.9%（≥2%）必须拒单 —— 市值反证兜底仍然有效。

    OCR 漏掉 510880 800 股（1,739.2，占 mv 2.91%）：calc 与 mv 偏差
    2.91% ≥ 2% → 不进幽灵分支；calc+missing = mv ≤ mv×1.05 → 判漏读拒单。
    """
    pos = {"513100": {"qty": 25000, "available": 25000, "frozen": 0,
                      "cost": 2.30, "price": 2.319}}
    old = {
        "513100.SH": {"qty": 25000, "cost": 2.30, "peak": 2.4, "mark": 2.319},
        "510880.SH": {"qty": 800, "cost": 2.174, "peak": 2.2, "mark": 2.174},
    }
    prices = {"513100.SH": 2.319, "510880.SH": 2.174}
    bal = {"cash": 1000.0, "market_value": 57975.0 + 1739.2}
    assert not B._pos_ok(pos, bal, prices, old), "漏读 2.9% 必须拒单"


def check_missed_read_under_2pct_accepted_hole():
    """场景 5（已知代价，记录在案）：漏读 1.5% < 2% 会按幽灵放过。

    这是新规则**有意接受**的洞：读到的持仓已能解释市值（偏差 <2%），
    本地「消失」的小仓位无法与幽灵区分。代价有界且自愈——账本少记一天，
    下次读取到该行即恢复，幅度被 2% 天然封顶。若未来实盘出现因此丢账本的
    情况，应回头把阈值压到噪声底（实测 0.06%~0.53%）之上、1% 附近。
    """
    pos = {"513100": {"qty": 25000, "available": 25000, "frozen": 0,
                      "cost": 2.30, "price": 2.319}}
    old = {
        "513100.SH": {"qty": 25000, "cost": 2.30, "peak": 2.4, "mark": 2.319},
        "510880.SH": {"qty": 400, "cost": 2.174, "peak": 2.2, "mark": 2.174},
    }
    prices = {"513100.SH": 2.319, "510880.SH": 2.174}
    bal = {"cash": 1000.0, "market_value": 57975.0 + 869.6}
    assert B._pos_ok(pos, bal, prices, old), \
        "漏读 <2% 按幽灵放过是新规则的有意行为（见 docstring 已知代价）"


def check_calc_mv_gate_5pct():
    """场景 6：判据 3 的 5% 闸门必须在判据 4 之前把关（09-28 串位帧口径）。

    513500 被读成 qty=500 → calc=59,343 vs mv=115,778 → 偏差 48.75%
    → 判据 3 直接拒（真实坏帧还会先被 pos_frame_sane 拦下，这里单测闸门）。
    """
    pos = dict(POS_LIVE)
    pos["513500"] = {"qty": 500, "available": 500, "frozen": 0,
                     "cost": 2.70, "price": 2.736}
    assert not B._pos_ok(pos, BAL_LIVE, P_LIVE, OLD_LIVE), \
        "calc 与 mv 偏差 48.75% > 5% 必须拒单"


def check_normal():
    """场景 7：无消失持仓（qty=0 的旧行不算消失；old 为 None/{} 不炸）→ 放过。"""
    old = dict(OLD_LIVE)
    old["511010.SH"] = {"qty": 0, "cost": 140.0, "peak": 141.0, "mark": 140.6}
    assert B._pos_ok(POS_LIVE, BAL_LIVE, P_LIVE, old)
    assert B._pos_ok(POS_LIVE, BAL_LIVE, P_LIVE, None)
    assert B._pos_ok(POS_LIVE, BAL_LIVE, P_LIVE, {})


def check_nan_price_rejected():
    """场景 8（2026-09-29 新增）：行情价 NaN/inf 必须拒单。

    nan 的比较运算**全部返回 False** —— 不挡它，判据 3（5% 闸门）和判据 4
    （幽灵分支/市值反证）会全部静默放行（本场景修复前实测：五个判据全"通过"），
    nan 还会经 reconcile 写进账本（cost/mark=nan → 市值/净值全污染）。
    入口闸门在 runner（prices isfinite），这里是校验器本体的最后一道防线。
    """
    for bad in (float("nan"), float("inf"), float("-inf")):
        px = dict(P_LIVE)
        px["513100.SH"] = bad
        assert not B._pos_ok(POS_LIVE, BAL_LIVE, px, OLD_LIVE), \
            "行情价 {!r} 必须拒单（nan/inf 会穿透全部判据）".format(bad)


def regression_all() -> int:
    """跑全部场景断言；返回通过数。失败即抛 AssertionError。"""
    fns = [check_ghost_small_deadlock_zone,
           check_ghost_large,
           check_true_sell,
           check_missed_read_over_2pct,
           check_missed_read_under_2pct_accepted_hole,
           check_calc_mv_gate_5pct,
           check_normal,
           check_nan_price_rejected]
    for fn in fns:
        fn()
        print("  ✓ {}".format(fn.__name__))
    return len(fns)


def main() -> int:
    print("_pos_ok 判据 4 数值回归（真数字场景，含 09-28 死锁实况）")
    n = regression_all()
    print("全部通过：{} 项".format(n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
