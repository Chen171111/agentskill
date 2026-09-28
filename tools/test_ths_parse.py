# -*- coding: utf-8 -*-
"""同花顺表格 OCR 解析的回归夹具（**纯离线，64 位 Python 可跑**）。

夹具来源：2026-09-21 18:01 从本机模拟盘「资金股票」页实拍，
`state/diag_shots/pos_20260921_180126_1.png` 是同一帧的截图。

事故背景
--------
当日上午用户手工分批买入 601091（新股「C沈鼓」，上市未满 5 个交易日），
OCR 把名称读成拉丁串 `Cit`，旧守卫「行内必须含汉字」把**整行持仓静默丢弃**：
    读到 pos = {'510880': ...}  （只 1 只）
    资金栏股票市值 = 93,350.40  vs  calc = 5,342.40  → 偏差 95.61%
→ `scheduler/runner.py:185` 拒单，全天零成交。

用法:
    E:\\Python32\\python.exe tools/test_ths_parse.py    # 或任何 Python，无 win32 依赖
被 `tools/selftest.py`「券商读取」组调用。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trader.ths_parse import parse_position, parse_trades, pos_frame_sane   # noqa: E402

# ---- 夹具 1：持仓表（含一行「名称 OCR 全丢」的新股） ----
POS_OCR = "\n".join([
    '操作 ，证券代码 ish Ra FARR FSS ， 成本价     市价       BS      至亏比例(99)    SHES    当日盈亏比(99)      市什       仓位占比(99)    SB',
    '= 510880 ”红利ETF华                    1600                    1600                            0               3.424               3.385                   -61.730                         -1.124                        73.60                                     1.38               5416.000                                    2.84',
    '= 601091 Cit                               2400                             0                    2400            41.673            37.720             -9488.000                          -9.486                -9488.00                                   -9.49            90528.000                                  47.42                    24',
    '汇总                                                                                                                                                                                                                                   -9549.730                                                          -9414.40                                                           95944.000                                  50.26',
])

# ---- 夹具 2：当日成交（同一只新股，两笔分批买入） ----
TRADE_OCR = "\n".join([
    '成交时间 证券代码 证券名称 ”操作    成交数量 | 成交均价    成交金额    SARS 成交疙号',
    '09:54:43 601091 Cita            买入                             1600              41.500           66400.000 6268782... 6645264...',
    '09:54:37 601091 Cita             买入                                   800               41.980            33584.000 6268813... 6645262...',
    '汇总                                                                                                                   2400                                            99984.000',
])

# ---- 夹具 3：OCR 抖动的碎片行（必须被丢弃，否则伪造假持仓） ----
JUNK_OCR = "\n".join([
    '可用款额',
    '1600',
    '市价',
    '3.401',
    '= 510880 红利ETF 1600',                      # 数字不足 4 个 → 碎片行
    '股票市值 95944.000',                          # 命中禁用词 → 非持仓行
])

# ---- 夹具 4：2026-09-24 实际坏帧（弹窗遮挡表格 → 串列/丢行，须整帧判坏） ----
# 当天 14:50 fetch_position 连拍 4 帧全坏 → 对账失败、拒单一整天。
# 特征：513100 盈亏列被串位读成负市价 -192.39；513500 的可用/冻结列读成
# 别行的数字（500 ≠ 21200+21200）；510880 整行丢失（被弹窗盖住）。
BAD_FRAME_20260924 = {
    '513100': {'qty': 25000, 'available': 25000, 'frozen': 0,
               'cost': 2.319, 'price': -192.39},
    '513500': {'qty': 500, 'available': 21200, 'frozen': 21200,
               'cost': 0.0, 'price': 2.736},
}
# 对照：好帧（09-21 实拍口径）——行内自洽，应通过帧级校验
GOOD_FRAME_20260921 = {
    '510880': {'qty': 1600, 'available': 1600, 'frozen': 0,
               'cost': 3.424, 'price': 3.385},
    '601091': {'qty': 2400, 'available': 0, 'frozen': 2400,
               'cost': 41.673, 'price': 37.720},
}


def check_position_row_with_latin_name():
    """回归：名称被 OCR 读成拉丁串（'C沈鼓'→'Cit'）的持仓行不得被丢弃。"""
    pos = parse_position(POS_OCR)
    assert set(pos) == {"510880", "601091"}, \
        "应解析出 2 只持仓，实际：{}".format(sorted(pos))
    p = pos["601091"]
    assert p["qty"] == 2400, "601091 数量错：{}".format(p)
    assert abs(p["cost"] - 41.673) < 1e-6, "601091 成本价错：{}".format(p)
    assert abs(p["price"] - 37.720) < 1e-6, "601091 市价错：{}".format(p)
    assert p["frozen"] == 2400 and p["available"] == 0, \
        "601091 当日买入应为 可用0/冻结2400，实际：{}".format(p)
    assert pos["510880"]["qty"] == 1600
    # 市值自证：2400 × 37.720 = 90,528.000，与表内「市值」列逐位一致
    assert abs(2400 * p["price"] - 90528.0) < 1e-6


def check_position_ignores_header_summary_junk():
    """表头/汇总/碎片行不得被当成持仓（放宽守卫后的反向防护）。"""
    pos = parse_position(POS_OCR)
    assert "汇总" not in pos
    assert len(pos) == 2, "表头/汇总行混进了持仓：{}".format(sorted(pos))
    pos2 = parse_position(JUNK_OCR)
    assert pos2 == {}, "碎片行被误当持仓：{}".format(pos2)


def check_position_still_handles_cjk_rows():
    """常规含中文的持仓行仍须正常解析（防「放宽守卫」把原行为改坏）。"""
    txt = ('= 510880 ”红利ETF华泰柏瑞   1600   1600   0   3.424   3.385   -61.730   '
           '5416.000   2.84')
    pos = parse_position(txt)
    assert pos["510880"]["qty"] == 1600 and abs(pos["510880"]["cost"] - 3.424) < 1e-6


def check_trades_row_with_latin_name():
    """回归：成交/委托行名称为拉丁串时不得丢行（否则 sync_fill 拿不到真实成交价）。"""
    rows = parse_trades(TRADE_OCR)
    assert len(rows) == 2, "应解析出 2 笔成交，实际 {} 笔：{}".format(len(rows), rows)
    qs = sorted(r["qty"] for r in rows)
    assert qs == [800, 1600], "成交数量解析错：{}".format(qs)
    assert all(r["code"] == "601091" for r in rows)
    assert all(r["side"] == "买入" for r in rows)


def check_pos_frame_sanity():
    """回归：2026-09-24 坏帧（负价/串列）必须被整帧判坏；好帧必须通过。"""
    assert not pos_frame_sane(BAD_FRAME_20260924), \
        "负市价 -192.39 / available+frozen≠qty 的坏帧必须被判坏"
    assert pos_frame_sane(GOOD_FRAME_20260921), \
        "行内自洽的好帧（含当日买入 0+2400=2400）不得被误判为坏帧"
    assert pos_frame_sane({}) and pos_frame_sane(None), "空帧不应崩溃"


def regression_all() -> int:
    """跑全部夹具断言；返回通过数。失败即抛 AssertionError。"""
    fns = [check_position_row_with_latin_name,
           check_position_ignores_header_summary_junk,
           check_position_still_handles_cjk_rows,
           check_trades_row_with_latin_name,
           check_pos_frame_sanity]
    for fn in fns:
        fn()
        print("  ✓ {}".format(fn.__name__))
    return len(fns)


def main() -> int:
    print("同花顺 OCR 解析回归（离线夹具）")
    n = regression_all()
    print("全部通过：{} 项".format(n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
