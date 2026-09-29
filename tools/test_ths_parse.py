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

from trader.ths_parse import (  # noqa: E402
    match_order_row, parse_position, parse_trades, pos_frame_sane)

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

# ---- 夹具 5：当日委托表，**同一代码买卖两行**（sync_fill 串向的真实场景） ----
# 场景：同日对同一只标的既有卖出又有买入委托（上午调仓卖出 → 下午重跑又买入，
# 或部分成交后补单、人工委托与自动委托撞在同一标的上）。
# 旧 sync_fill 只按 code 匹配、命中即返回第一行 → 买单会拿到**卖单**的
# 成交均价 3.498，`orders` 台账的「成交价」是假的。
ORDER_OCR_BOTH_SIDES = "\n".join([
    '委托时间 证券代码 证券名称 操作 状态 委托数量 成交数量 委托价格 成交均价 撤消数量 合同编号 交易市场',
    '09:35:12 510880 红利ETF华泰柏瑞 卖出 已成 100 100 3.500 3.498 0 625377491 上海A股',
    '14:50:03 510880 红利ETF华泰柏瑞 买入 已成 200 200 3.400 3.398 0 625377492 上海A股',
])

# ---- 夹具 6：2026-09-29 实盘「未成交卖单」原样 OCR（未删改） ----
# 真实事故：证券名称「标普500ETF」里的 500 被当成**委托数量**，整行数字左移一位，
# 于是 filled_qty 被写成 100（实际 0 成交）→ orders 台账写出**假成交**。
# 注意本行的「操作」列（卖出）被 OCR 漏读，side 为 None —— 这也一并回归。
ORDER_OCR_UNFILLED = "\n".join([
    '委托时间 《证券代码 ， 证券名称 操作           备注            委托数量 ame ， 委托价格 ， 成交均价 ” 撤漠数量        SaaS        交易市场',
    '14:56:59 513500 ，标普500ETF Sik            未成交                       100                    0             2.688             0.000                    0 6285045859 ”上海A股',
    '汇总                                                                                              100                 0',
])


# ---- 夹具 7：2026-09-29 实盘「已成交卖单」原样 OCR（未删改，3 帧） ----
# 背景：那笔 100 股 513500 卖单在收盘集合竞价成交后，连拍 5 帧「当日委托」页，
# **逐帧对照 OCR 原文**才发现旧解析的结构性错误：
#   「操作」列（应为「卖出」）5/5 帧全部被读坏（'Sik'/'32h'/'SH SS8aba'/'Si SHBatae'/'32k'）
#   「状态」列（应为「全部成交」）有 2 帧被读坏（'SS8aba'/'SHBatae'）
# → 任何「从操作/状态关键词起点抽数字」的方案都会**整行左移一位**：
#     名称「标普500ETF」的 500 被当成委托数量，
#     **「委托数量」被当成「成交数量」**（qty=500/filled_qty=100/price=100.0）
# → orders 台账的成交价与成交量全是假的。
# 修法：改「行尾市场词锚定」（尾部 6 列 5/5 帧稳定）。
# 三帧共同的真值：qty=100 filled_qty=100 price=2.688 avg_price=2.690 deal_id=6285045859

# 帧 4：状态列读出「全部成交」，最干净的一帧
ORDER_OCR_FILLED = "\n".join([
    '委托时间 《证券代码 ， 证券名称 操作           备注            委托数量 ame ， 委托价格 ， 成交均价 ” 撤漠数量        SaaS        交易市场',
    '14:56:59 513500 ，标普500ETF Sik  全部成交     100    100   2.688   2.690    0 6285045859 ”上海A股',
    '汇总                                                                                              100                 0',
])

# 帧 1：**状态列被 OCR 读坏**（'SS8aba'）→ 必须靠数量关系兜底判「已成」
ORDER_OCR_FILLED_BADSTATUS = "\n".join([
    '委托时间 《证券代码 ， 证券名称 操作           备注            委托数量 ame ， 委托价格 ， 成交均价 ” 撤漠数量        SaaS        交易市场',
    '14:56:59 513500 #@S500ETF SH SS8aba    100   100   2.688   2.690    0 6285045859 “上海A股',
])

# 帧 0：名称前还混进了 `#R@S500ETF 32h` 这类噪串（多一个数字 32）
ORDER_OCR_FILLED_NOISY = "\n".join([
    '14:56:59 513500 #R@S500ETF 32h  全部成交    100   100   2.688   2.690    0 6285045859 ”上海A股',
])


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


def check_order_row_matches_side():
    """回归：sync_fill 必须按【方向】匹配委托行，同日同代码多笔时不得串向。

    背景（2026-09-29 核对 `Order` 语义时发现）：`UiaThsBroker.sync_fill()` 旧实现
    只按 code 匹配并 `return` 第一行。当日委托表里同一代码可能有多行且方向相反
    （部分成交补单 / 同日重跑 / 人工委托撞车），于是买单会被写进卖单的
    成交价与成交量 —— `orders` 表是审计事实记录，串向即失真
    （资金与持仓有 reconcile 兜底，所以不会错单，但台账会说谎）。
    """
    rows = parse_trades(ORDER_OCR_BOTH_SIDES)
    assert len(rows) == 2, "应解析出 2 行委托，实际 {}：{}".format(len(rows), rows)
    assert {r["side"] for r in rows} == {"买入", "卖出"}, \
        "「操作」列未解析出来：{}".format([r["side"] for r in rows])

    # 买单必须选中**买入**行（成交均价 3.398），而不是卖单的 3.498
    r = match_order_row(rows, "510880.SH", "buy", 200)
    assert r is not None and r["side"] == "买入", "买单选中了卖单行：{}".format(r)
    assert abs(r["avg_price"] - 3.398) < 1e-9, \
        "买单成交均价串到了卖单：{}".format(r)

    # 卖单必须选中**卖出**行
    r2 = match_order_row(rows, "510880.SH", "sell", 100)
    assert r2 is not None and r2["side"] == "卖出", "卖单选中了买单行：{}".format(r2)
    assert abs(r2["avg_price"] - 3.498) < 1e-9

    # 数量对不上但方向唯一 → 仍按方向匹配（OCR 可能把数量读错）
    r3 = match_order_row(rows, "510880", "buy", 999)
    assert r3 is not None and r3["side"] == "买入"

    # 只剩反向行 → 必须返回 None（宁可台账停在 submitted，也不用错行）
    only_sell = [x for x in rows if x["side"] == "卖出"]
    assert match_order_row(only_sell, "510880.SH", "buy", 100) is None, \
        "只有反向委托时不得回退取第一行（会串向）"

    # OCR 丢了「操作」列（side=None）→ 兜底匹配，否则台账永远停在 submitted
    blind = [dict(rows[0], side=None)]
    assert match_order_row(blind, "510880.SH", "buy", 100) is not None, \
        "OCR 丢方向时应兜底匹配"

    # 代码不存在 / 空输入 / None
    assert match_order_row(rows, "999999.SH", "buy", 100) is None
    assert match_order_row([], "510880.SH", "buy", 100) is None
    assert match_order_row(None, "510880.SH", "buy", 100) is None


def check_trades_name_digits_not_treated_as_qty():
    """回归：证券名称里的数字（「标普500ETF」的 500）不得被当成委托数量。

    背景（2026-09-29 实盘暴露）：旧实现对 code 之后的**整段**抽数字，
    名称里的 500 变成 nums[0] → 整行数字左移一位 →
    **「委托数量」被当成「成交数量」**（filled_qty=100，而实际 0 成交）
    → `orders` 台账写出**假成交**。这是「台账说谎」类问题的典型，
    只有用真实 OCR 原文才能回归到。
    """
    rows = parse_trades(ORDER_OCR_UNFILLED)
    assert len(rows) == 1, "应解析出 1 行委托，实际 {}：{}".format(len(rows), rows)
    r = rows[0]
    assert r["code"] == "513500"
    assert r["qty"] == 100, "委托数量错（名称里的 500 混进来了）：{}".format(r)
    assert r["filled_qty"] == 0, "成交数量错（把委托数量当成了成交量）：{}".format(r)
    assert abs(r["price"] - 2.688) < 1e-9, "委托价格错：{}".format(r)
    assert not r["avg_price"], "未成交时成交均价应为 0/空：{}".format(r)
    assert r["deal_id"] == "6285045859", "合同编号错：{}".format(r)
    # 该行「操作」列被 OCR 漏读 → side 为 None，但不得因此丢行
    assert r["side"] is None, "该行操作列本就读不到，应为 None：{}".format(r)
    # 状态：文本为「未成交」→ 归入「已报」（未成交）
    assert r["status"] == "已报", "未成交应归为已报：{}".format(r)


def check_trades_filled_row_tail_anchor():
    """回归：**已成交**委托行的 6 个尾部数字列不得错位（2026-09-29 实盘 3 帧）。

    事故链
    ------
    「操作」列 5/5 帧被 OCR 读坏 → 旧「从关键词起点抽数字」的锚点失效 →
    整行数字左移一位：名称「标普500ETF」的 500 变成委托数量，
    「委托数量」变成「成交数量」。旧解析（帧 4）：
        qty=500 / filled_qty=100 / price=100.0 / avg_price=2.688 / deal_id='0'
    真值：
        qty=100 / filled_qty=100 / price=2.688 / avg_price=2.690 / deal_id=6285045859

    最危险的后果不是价格错，而是 **filled_qty 说谎** —— 对「未成交」行
    （夹具 6）会写成 filled_qty=100 而实际 0 成交，`orders` 台账出现**假成交**。
    """
    for tag, txt in (("干净帧", ORDER_OCR_FILLED),
                     ("状态列读坏帧", ORDER_OCR_FILLED_BADSTATUS),
                     ("名称前噪串帧", ORDER_OCR_FILLED_NOISY)):
        rows = parse_trades(txt)
        assert len(rows) == 1, "[{}] 应解析出 1 行，实际 {}：{}".format(
            tag, len(rows), rows)
        r = rows[0]
        assert r["code"] == "513500", "[{}] 代码错：{}".format(tag, r)
        assert r["qty"] == 100, \
            "[{}] 委托数量错（名称里的 500 混进来了？）：{}".format(tag, r)
        assert r["filled_qty"] == 100, \
            "[{}] 成交数量错（整行左移了？）：{}".format(tag, r)
        assert abs(r["price"] - 2.688) < 1e-9, \
            "[{}] 委托价格错：{}".format(tag, r)
        assert abs(r["avg_price"] - 2.690) < 1e-9, \
            "[{}] 成交均价错（被委托价顶替了？）：{}".format(tag, r)
        assert r["deal_id"] == "6285045859", \
            "[{}] 合同编号错：{}".format(tag, r)
        # 状态：帧 1 的「全部成交」被读成 'SS8aba' → 必须靠「委托数量==成交数量」
        # 兜底判「已成」，否则 sync_fill 的 status 永远停在 submitted
        assert r["status"] == "已成", \
            "[{}] 已成交行状态错（状态列读坏时未用数量关系兜底）：{}".format(tag, r)


def check_trades_filled_status_from_qty_fallback():
    """回归：状态列被 OCR 读坏时，用「成交数量 >= 委托数量」兜底判「已成」。

    帧 1 实测：状态列读出 'SS8aba'（「全部成交」被读坏），但委托数量/成交数量
    都是 100 —— 若不做兜底，`sync_fill` 里 `r["status"] == "已成"` 永远为假，
    `order.status` 就永远停在 `submitted`，台账看不出这笔已经成交了。
    """
    rows = parse_trades(ORDER_OCR_FILLED_BADSTATUS)
    assert rows and rows[0]["status"] == "已成", \
        "状态列读坏时应由数量关系兜底：{}".format(rows)
    # 反向：部分成交（100 委托 / 40 成交）不得被兜底成「已成」
    partial = ('14:56:59 513500 标普500ETF Sik  部分成交   100    40   '
               '2.688   2.690    0 6285045859 ”上海A股')
    r = parse_trades(partial)[0]
    assert r["status"] == "部分成交", "部分成交被误判：{}".format(r)
    # 反向：已撤优先于「已成」文本
    cancelled = ('14:56:59 513500 标普500ETF Sik  已撤      100     0   '
                 '2.688   0.000    0 6285045859 ”上海A股')
    r = parse_trades(cancelled)[0]
    assert r["status"] == "已撤", "已撤被误判：{}".format(r)
    assert r["filled_qty"] == 0, "已撤行成交数量应为 0：{}".format(r)


def check_trades_sync_fill_gets_real_avg_price():
    """回归：sync_fill 从已成交行取到的是**成交均价**，不是委托价。

    帧 4 的委托价 2.688 与成交均价 2.690 **不同**（收盘集合竞价成交价高于
    委托价）—— 这正是「用委托价冒充成交价」这类静默缺陷的照妖镜：
    若取错，`orders.filled_price` 会写 2.688，与券商真值差 0.002/股。
    """
    rows = parse_trades(ORDER_OCR_FILLED)
    r = match_order_row(rows, "513500.SH", "sell", 100)
    assert r is not None, "已成交卖单应能被 match_order_row 选中"
    assert abs(r["avg_price"] - 2.690) < 1e-9, \
        "sync_fill 会取到 avg_price，它必须是成交均价 2.690：{}".format(r)
    assert abs(r["price"] - 2.688) < 1e-9, "委托价应为 2.688：{}".format(r)


def regression_all() -> int:
    """跑全部夹具断言；返回通过数。失败即抛 AssertionError。"""
    fns = [check_position_row_with_latin_name,
           check_position_ignores_header_summary_junk,
           check_position_still_handles_cjk_rows,
           check_trades_row_with_latin_name,
           check_pos_frame_sanity,
           check_order_row_matches_side,
           check_trades_name_digits_not_treated_as_qty,
           check_trades_filled_row_tail_anchor,
           check_trades_filled_status_from_qty_fallback,
           check_trades_sync_fill_gets_real_avg_price]
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
