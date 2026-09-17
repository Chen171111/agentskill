# -*- coding: utf-8 -*-
"""
个股线（股息率策略）· 聚宽回测移植版
=====================================

来源
----
本项目（E:\\MyWorkAndProject\\量化\\agentskill）个股线的**定稿**：
    docs/个股线_股息率实盘方案.md        ← 定稿形态（执行规则）
    docs/个股线_聚宽回测移植说明.md      ← 移植须知（**§六 必须读**）
    tools/backtest_dividend.py           ← 本地引擎（成本模型 / cond_col 等权口径）
    tools/test_industry_neutral.py       ← 行业内百分位的实现（plan_selections）
    tools/test_dividend_factor.py        ← `dy_ttm` / `n_div3` 的定义（build_yield_panel）

定稿形态（一句话）
------------------
全 A 池 → 剔 ST
  → 合格池：上市 ≥120 交易日 且 20 日均成交额 ≥3000万 且 不复权收盘价 ≥2 元 且 非停牌
  → 过滤：0.5% ≤ dy_ttm ≤ 10%  且  近 3 个自然年现金分红 ≥2 次
  → 排序：在【所属一级行业内】按 dy_ttm 算百分位 → 取全局前 20 名
  → 等权（每只 5%）→ 持有 60 个交易日（季度，每年 4 次）
  → 持有期内不做任何操作（不再平衡、不止损、不择时）

⚠️ 三件必须先知道的事（否则会把这个数字当成预期）
--------------------------------------------------
**① 本地样本外（税后 + 真实成本）年化 10.10%，但它是「层层加码」的结果 ——**
把定稿拆开看每一层的贡献（全区间 2019-01-02 ~ 2026-09-11，N=20）：

| 层 | 年化 |
|---|---|
| **裸**（只有「按股息率排序取前 20」） | **6.82%**（还跑输中证1000 −0.23pp） |
| ＋ `≤10%` 上限 | 15.46%（**+8.64pp**） |
| ＋ `≥2次` 分红持续性 | 16.08%（+0.62pp） |
| ＋ 行业内百分位（**定稿**） | 18.94%（+2.86pp） |

而 `≤10%` 上限与行业内排序**都是看过全区间结果才挑的**。
→ **聚宽上大概率会跑出偏乐观的数字，别把它当预期。**
→ 所以本文件留了 `FORM = 'naked'` 开关，**请务必把裸版也跑一遍做对照。**

**② 聚宽的数字不会和本地一致**（数据源 / 行业分类 / 分红口径 / 退市处理都不同）。
要看的是**方向与量级**，不是小数点。详见 docs/个股线_聚宽回测移植说明.md §6.2。

**③ 10 万 + A股 100 股整手 = 真约束。** 10万 ÷ 20 = 5000 元/笔
→ **股价 > 50 元的股票买不进 1 手，会被静默跳过**。本地引擎不建模整手，
这一条会让聚宽与本地分叉。每次调仓的日志里会打印被跳过的股票。

聚宽 API 事实（已对官方文档核实，非臆测）
-----------------------------------------
- `get_price(..., fields=['close','money','paused'], fq='none', panel=False)`
  → `fq='none'` 返回**不复权真实价**（股息率的分母必须是它）；`panel=False` 返回
  **长表**（列：`time / code / <fields>`）
- `finance.STK_XR_XD`：`a_xr_date`（A股除权除息日）、`bonus_ratio_rmb`（每10股派息·税前）、
  `dividend_ratio`（送股·每10股）、`transfer_ratio`（转增·每10股）、`bonus_cancel_pub_date`
- **聚宽因子库没有现成的 TTM 股息率因子**（`divyild` 是「风格因子pro」，仅本地 jqdatasdk 可用）
  → 股息率必须自建，本文件用 `STK_XR_XD` 复刻本地口径
- 成本可**精确对齐**本地模型：
  `OrderCost(open_commission=0.0005, close_commission=0.0005, close_tax=0.001, min_commission=5)`
  + `set_slippage(PriceRelatedSlippage(0.001))`（双边价差 0.1% → **单边 0.05%**）
- `get_current_data()` 的属性：`last_price / high_limit / low_limit / paused / is_st / day_open / name`
  ⚠️ **日线回测下 `day_open` 不可用**（官方原文：「天回测时…并不知道开盘价，请不要使用」）
  → 涨停开盘的跳过，交给**聚宽自己的撮合**：买单在涨停价上无法成交，未成交订单当日撤销。
    这与定稿规则「该股跳过，资金不补」等价。

与本地实现的两处**口径说明**（改代码前先看）
--------------------------------------------
1. **候选不足 N 只时 → 等权买满（不是留现金）。**
   本地引擎 `tools/backtest_stock.py` 的 `cond_col` 分支是
   `wv = ones(len(sel)); wv / wv.sum()` → 选到 12 只就每只 1/12，**满仓**。
   文档 §2.5 写的「剩余资金留现金」与实现不一致，**本文件按实现走**。
2. **`MIN_PICKS = 5`（候选 < 5 空仓）只有文档有、引擎里没有**（引擎是 `len(sel)==0` 才空仓）。
   实测候选池通常 ≥100，该规则不 binding，保留它只是照文档执行。

使用
----
1. 聚宽 → 策略研究/回测 → 新建策略 → 把本文件**整段**贴进去
2. 回测设置：**起始 2019-01-02 / 结束 2026-09-11 / 日线 / 初始资金 100000**
   （若改了区间，必须同步改下面的 `ANCHOR_DATE` / `TRADE_END`）
3. 先跑 `FORM = 'indpct'`（定稿），再把它改成 `'naked'`（裸版对照），**两次结果对比**
4. 看的是聚宽回测页面的 年化 / 夏普 / 最大回撤 / 基准对比

免责声明：研究与教学用途，不构成投资建议。所有回测数字均为历史模拟，不预示未来收益。

⚠️ 聚宽**回测环境 = Python 3 + 老 pandas/numpy**
   （2026-09-17 实跑证实：页面明确标注 `Python3`，但 pandas 0.23 系 / numpy 也老）。
   - 踩到的坑：`np.dtype(u'未分类')` 在**老 numpy** 里抛
     `UnicodeEncodeError: 'ascii' codec can't encode`（它的 dtype 解析器做 ascii 编码）。
     ⚠️ 本机 numpy 2.5 抛的是 `TypeError` —— **别拿本机行为推测线上**，这正是我一开始
     误判成「Python 2」的原因。
   - **必须守的**：不要把标量字符串传给 `pd.Series(..., index=...)`（老 pandas 会拿它走 dtype
     推断 → 上面那个崩）。正确写法：`pd.Series([UNKNOWN_IND] * len(pool), index=pool, dtype=object)`
   - 其余约束（不用 f-string / 类型注解 / 中文字面量加 `u`）**不是必需的**（Py3 支持本文件全部语法），
     保留无害；`u''` 在 Py3 下与 `''` 等价
   - ⚠️ **`finance.run_offset_query` 在聚宽平台上不存在**（它是 jqdatasdk 的函数）→
     必须保留 `_paged_query` 兜底，否则分红数据整段拿不到。
     实测：平台报 `'finance' object has no attribute 'run_offset_query'`，兜底接管后正常取到 **36345 条**
   - **已实测跑通**：`get_all_securities(date=)`、`get_extras(is_st)` 分批、`get_price(fq='none',
     panel=False)` 的**长表** + 分批、`get_industry` 分批、`pd.to_numeric`、
     `groupby().transform()` + 反向 `cumprod`、`_paged_query` 分页、中文 `log.info`、
     `run_daily(time='open', reference_security=...)`
   - **实测结果**（全区间 2019-01-02~2026-09-11 / 10 万 / 基准中证1000 67.66%）=>
     定稿 **158.86%**（Sharpe 0.52 / 回撤 19.04%）｜裸版 **60.45%**（跑输基准！）｜
     修正送转口径 **112.50%**（Sharpe 0.37）—— 见 `RUNSHEET.md`
"""
from jqdata import *                                        # noqa: F401,F403
from jqdata import finance                                  # noqa: F401
import time
import numpy as np
import pandas as pd


# ==========================================================================
# 一、参数（改这里 = 改策略；逻辑改动请同步 docs/个股线_股息率实盘方案.md）
# ==========================================================================
FORM = 'indpct'          # 'indpct' = 定稿（行业内百分位）
                         # 'naked'  = 裸版对照（只按股息率排序，无 ≤10% 上限、无 ≥2次）
                         # 'topn'   = 全局排名 + 两个过滤器（= 定稿去掉行业中性）

TOPN = 20                # 持仓数
HOLD = 60                # 调仓周期（交易日）≈ 季度
MIN_DY = 0.5             # 股息率下限（%）
MAX_DY = 10.0            # 股息率上限（%）—— 砍掉价值陷阱 / 一次性特别分红
MIN_DIV3 = 2             # 近 3 个自然年现金分红次数下限 —— 挡掉「今年分了明年不分」
MIN_PRICE = 2.0          # 收盘价下限（元，**不复权真实价**）
MIN_AMOUNT = 3e7         # 20 日均成交额下限（元）
MIN_LISTED = 120         # 上市交易日数下限
MIN_PICKS = 5            # 候选 < 5 → 视为「市场无高股息机会」→ 空仓，不硬凑（见上文口径说明 2）
BATCH = 800              # 行情/ST/行业 的**分批拉取**粒度（全A 5000+ 只一次请求易超时）

ANCHOR_DATE = '2019-01-02'   # 第一个选股日（调仓日历锚点，必须落在回测区间内）
TRADE_END = '2026-09-11'     # 与回测结束日一致（用于预取交易日历/分红）
SECTOR = 'sw_l1'             # 行业分类口径：'sw_l1' 申万一级 / 'jq_l1' 聚宽一级 / 'zjw' 证监会
BENCHMARK = '000852.XSHG'    # 中证1000（本地样本外超额就是跟它比的）
LOG_PICKS = True             # 是否打印每次调仓的入选明细

# ⚠️ 无行业标签的哨兵值 —— **必须用 unicode 字面量 `u'...'**
#    聚宽回测环境是老 pandas（0.23 系）+ Python 2 系（实测 2026-09-17 的 UnicodeEncodeError 可证）。
#    Py2 下 `u'银行' != '未分类'` 会拿 ascii 去 decode 那个 byte str → UnicodeDecodeError。
#    统一用 unicode 哨兵即可彻底避开（Py3 下 `u''` 与 `''` 等价，无副作用）。
UNKNOWN_IND = u'未分类'

# ⚠️ 送转调整口径（**这是本次移植查出来的一个真问题，见 README §6.1**）
#   'legacy'  = 复刻本地 `tools/test_dividend_factor.py`（保证与本地数字可比，**默认**）
#   'correct' = 价值中性口径（除送转因子、且只除到选股日为止）
#   判据：送转是价值中性的 → 纯送转除权日前后 dy 应当连续。
#   实测（`joinquant/_verify_core.py`，24 个纯送转样本）：
#      legacy  → dy 在送转日跳变的中位比值 **1.446**，偏离 (1+r) 仅 0.031（=方向错）
#      correct → 中位比值 **1.008**，即**连续** ✓
#   → 默认仍是 legacy，只为了「聚宽数字 vs 本地数字」可比；
#     若主人要测修正口径，把这里改成 'correct' 再跑一次即可。
ADJ_MODE = 'legacy'

# 裸版对照：只保留「按股息率排序取前 20」，去掉两个过滤器（与本地 6.82% 那一行对齐）
if FORM == 'naked':
    MAX_DY = None
    MIN_DIV3 = 0

# 本地成本模型（tools/backtest_dividend.py）：佣金万5 / 最低5元 / 印花税0.1% / 滑点单边0.05%
COMMISSION = 0.0005
MIN_COMMISSION = 5.0
STAMP_TAX = 0.001
SLIP_HALF_SPREAD = 0.001     # PriceRelatedSlippage 参数 = 双边价差；单边 = 0.05%


# ==========================================================================
# 二、初始化
# ==========================================================================
def initialize(context):
    set_benchmark(BENCHMARK)
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    # 成本：与本地模型逐项对齐
    set_order_cost(OrderCost(open_tax=0.0,
                             close_tax=STAMP_TAX,
                             open_commission=COMMISSION,
                             close_commission=COMMISSION,
                             close_today_commission=0.0,
                             min_commission=MIN_COMMISSION),
                   type='stock')
    set_slippage(PriceRelatedSlippage(SLIP_HALF_SPREAD), type='stock')
    log.set_level('order', 'error')          # 关掉 order 系列的低级别日志，免得刷屏

    # ---- 调仓日历：选股日 = ANCHOR_DATE 起每 HOLD 个交易日；执行日 = 其后的第一个交易日 ----
    days = [pd.Timestamp(d) for d in
            get_trade_days(start_date=ANCHOR_DATE, end_date=TRADE_END)]
    g.sel_dates = [d.strftime('%Y-%m-%d') for d in days[0::HOLD]]
    g.exec_map = {}
    for i in range(0, len(days) - 1, HOLD):
        g.exec_map[days[i + 1].strftime('%Y-%m-%d')] = days[i].strftime('%Y-%m-%d')

    # ---- 交易日历数组（算上市天数用；从 2000 年起足够覆盖全部在册股票）----
    g.trade_days = np.array(
        [np.datetime64(pd.Timestamp(d), 'D')
         for d in get_trade_days(start_date='2000-01-01', end_date=TRADE_END)],
        dtype='datetime64[D]')

    # ---- 分红数据**一次性预取**，使用时严格按 `a_xr_date <= 选股日` 过滤（不做前视）----
    #  为什么预取：单次调仓要查「近 3 年全市场分红」，逐次查 31 遍太慢。
    #  ⚠️ 预取本身不构成前视 —— 分红记录用除权除息日（a_xr_date）索引，
    #     而除权日一过，这笔分红就是既成事实。`_yield_at()` 里有显式过滤。
    g.div = _load_dividends(ANCHOR_DATE, TRADE_END)

    g.n_reb = 0
    g.px_last = {}
    g.t_start = time.time()          # 用于报「本次调仓耗时 / 累计耗时」（聚宽按 CPU 耗时分档计费）
    g.t_last = g.t_start
    log.info('=' * 78)
    log.info('个股线·股息率策略（聚宽移植版）  形态 = {} ｜ 送转口径 = {}'.format(
        FORM, ADJ_MODE))
    if ADJ_MODE == 'correct':
        log.info('  ⚠️ 已启用修正口径（价值中性）—— 与本地历史结论**不可直接比对**')
    log.info('  N={} / hold={} / dy∈[{}, {}] / ≥{}次 / 行业口径={}'.format(
        TOPN, HOLD, MIN_DY, MAX_DY if MAX_DY is not None else '∞', MIN_DIV3, SECTOR))
    log.info('  选股日 {} 个（{} ~ {}），首次执行 {}'.format(
        len(g.sel_dates), g.sel_dates[0], g.sel_dates[-1],
        sorted(g.exec_map)[0] if g.exec_map else '—'))
    log.info('  分红记录 {} 条 / {} 只'.format(
        len(g.div), g.div.code.nunique() if len(g.div) else 0))
    log.info('=' * 78)

    # ⚠️ 必须是**开盘时刻**：聚宽文档「如果在开盘时刻运行，最新价格为开盘价」
    #    → 用 'open' + reference_security（平台惯用法，见聚宽自带模板）
    #    若某天 'open' 报错，可换成具体时间：run_daily(rebalance, time='09:30')
    run_daily(rebalance, time='open', reference_security=BENCHMARK)


# ==========================================================================
# 三、调仓（T+1 开盘执行；持有期内不做任何操作）
# ==========================================================================
def rebalance(context):
    today = context.current_dt.strftime('%Y-%m-%d')
    sel_date = g.exec_map.get(today)
    if sel_date is None:
        return                                   # 非调仓日 → 什么都不做

    g.n_reb += 1
    picks, diag = _select(sel_date)
    cd = get_current_data()
    cur = set(context.portfolio.positions.keys())
    tgt = set(picks.code.tolist()) if len(picks) else set()

    log.info('─' * 78)
    log.info('[第 {} 次调仓] 选股日 {} → 执行日 {}（开盘）'.format(g.n_reb, sel_date, today))
    log.info('  全A {} → 上市足 {} → 非ST {} → 合格池 {} → 过滤后候选 {} → 入选 {}'.format(
        diag.get('全A', 0), diag.get('上市足', 0), diag.get('非ST', 0),
        diag.get('合格池', 0), diag.get('候选', 0), len(picks)))
    # ⚠️ 聚宽按「CPU 占用耗时」算配额（免费 60 分钟/天）→ 必须能看到耗时，
    #    否则跑到一半配额用光、连跑不完都说不清
    _now = time.time()
    log.info('  ⏱ 本次调仓耗时 {:.1f}s ｜ 累计 {:.1f}s（墙钟，非聚宽的 CPU 计费口径）'.format(
        _now - g.t_last, _now - g.t_start))
    g.t_last = _now

    if len(picks) < MIN_PICKS:
        # 定稿 7.2：候选 < 5 → 市场无高股息机会 → 空仓等待，不硬凑
        log.info('  ⚠️ 候选池 < {} → 按规则空仓（不硬凑）'.format(MIN_PICKS))
        for s in sorted(cur):
            _order_out(cd, s, 0, '清仓', context)
        return

    # ---- 先卖后买（卖出腾出现金再买）----
    for s in sorted(cur - tgt):
        _order_out(cd, s, 0, '卖出', context)
    # ---- 买入：等权，每只 1/N（候选不足 N 只时同样等权买满，见上文口径说明 1）----
    w = 1.0 / len(picks)
    tv = context.portfolio.total_value
    ok_n, fail = 0, []
    for s in picks.code.tolist():
        if _order_out(cd, s, tv * w, '买入', context):
            ok_n += 1
        else:
            fail.append(s)
    log.info('  已下单 {} 只（每只目标 {:.0f} 元 = {:.1f}%）｜ 未成交 {} 只'.format(
        ok_n, tv * w, w * 100, len(fail)))
    if fail:
        log.info('  ⚠️ 未成交 = 涨停开盘 / 跌停 / 停牌 / 资金不足 1 手：')
        for c in fail:
            log.info('     {}  参考价 {} 元'.format(
                c, '%.2f' % g.px_last[c] if c in g.px_last else '—'))
        log.info('     注：总资产 {:.0f} 元 ÷ {} 只 = {:.0f} 元/笔 → 股价 > {:.0f} 元的股票'
                 '买不进 1 手（A股 100 股整手）'.format(
                     tv, len(picks), tv * w, tv * w / 100.0))
    if LOG_PICKS and len(picks):
        log.info('  入选明细（按股息率降序）：')
        for r in picks.sort_values('dy', ascending=False).itertuples(index=False):
            # ⚠️ 模板必须是**纯 ASCII**！`r.ind` 是聚宽返回的中文 unicode，
            #    Py2 下「非 ASCII 字节模板 + unicode 参数」会抛 UnicodeDecodeError。
            log.info('    {:<12} dy={:>6.2f}%  ind={}'.format(r.code, r.dy, r.ind))


def _order_out(cd, security, value, tag, context):
    """下单并返回是否真的成交。停牌直接跳过；失败只记日志，不中断回测。"""
    if cd[security].paused:
        log.info('    {} {} —— 停牌，跳过（顺延）'.format(tag, security))
        return False
    try:
        o = order_target_value(security, value)
    except Exception as e:                       # 聚宽在异常标的上会抛错
        # ⚠️ 模板保持纯 ASCII —— 异常消息可能是中文 unicode（Py2 下会 UnicodeDecodeError）
        log.info('    {} {} -- order error: {}'.format(tag, security, e))
        return False
    if o is None or getattr(o, 'filled', 0) == 0:
        return False
    return True


# ==========================================================================
# 四、选股（严格复刻本地 plan_selections / build_mask 的 indpct 分支）
# ==========================================================================
def _select(sel_date):
    """在 `sel_date` 的收盘数据上选股，返回 (DataFrame[code,dy,ind], 诊断计数)。

    ⚠️ 只用 ≤ sel_date 的数据；股息率用「已除权」口径（a_xr_date ≤ sel_date），
       不依赖公告时点，避免前视。
    """
    diag = {}

    # ---- 1) point-in-time 全 A（含后来退市的股票，避免生存者偏差）----
    allsec = get_all_securities(types=['stock'], date=sel_date)
    # 剔除 B 股（沪B 900xxx / 深B 200xxx）—— 本地池子只有 A 股
    allsec = allsec[~allsec.index.str.match(r'^(900|200)')]
    diag['全A'] = len(allsec)
    if not len(allsec):
        return _empty_picks(), diag

    # ---- 2) 上市 ≥ MIN_LISTED 个交易日 ----
    ni = np.searchsorted(g.trade_days, np.datetime64(sel_date, 'D'), side='right')
    sd = pd.to_datetime(allsec.start_date).values.astype('datetime64[D]')
    ns = np.searchsorted(g.trade_days, sd, side='left')
    codes = list(allsec.index[(ni - ns + 1) >= MIN_LISTED])
    diag['上市足'] = len(codes)
    if not codes:
        return _empty_picks(), diag

    # ---- 3) 剔 ST（含 *ST / 退市整理期）----
    st = _batched(lambda cs: get_extras('is_st', cs, start_date=sel_date,
                                        end_date=sel_date), codes, axis=1)
    if st is not None and len(st):
        row = st.iloc[0]
        codes = list(row.index[~row.astype(bool)])
    else:
        # ⚠️ 静默失效是这类项目最大的坑 → 拉不到就明说，别假装剔过了
        log.warning('⚠️ {} ST 标记拉取为空 → 本次**未剔 ST**，结果会偏离定稿'.format(sel_date))
    diag['非ST'] = len(codes)
    if not codes:
        return _empty_picks(), diag

    # ---- 4) 行情：不复权真实价 + 成交额 + 停牌（近 20 个交易日）----
    px = _batched(lambda cs: get_price(cs, end_date=sel_date, count=20,
                                       frequency='daily',
                                       fields=['close', 'money', 'paused'],
                                       fq='none', skip_paused=False, panel=False),
                  codes, axis=0)
    if px is None or not len(px):
        log.warning('⚠️ {} 行情为空 → 本次不调仓'.format(sel_date))
        return _empty_picks(), diag
    px = _as_long(px, codes)
    px = px.sort_values(['code', 'time'])
    amt_ma20 = px.groupby('code')['money'].mean()          # 含当日的 20 日均值（与本地一致）
    lastrow = px.groupby('code').tail(1).set_index('code')
    close = lastrow['close'].astype(float).reindex(codes)   # 不复权真实收盘价
    paused = lastrow['paused'].astype(bool).reindex(codes)

    keep = ((amt_ma20.reindex(codes).fillna(0.0) >= MIN_AMOUNT)
            & (close.fillna(0.0) >= MIN_PRICE)
            & (~paused.fillna(True)))
    pool = [c for c in codes if bool(keep.get(c, False))]
    diag['合格池'] = len(pool)
    if not pool:
        return _empty_picks(), diag

    # ---- 5) dy_ttm（= 近 365 天已除权现金分红 ÷ 不复权真实价）+ 近 3 年分红次数 ----
    dps, n3 = _yield_at(sel_date, pool)
    s_px = close.reindex(pool)
    dy = dps.reindex(pool).fillna(0.0) / s_px.replace(0, np.nan) * 100.0
    n_div3 = n3.reindex(pool).fillna(0).astype(int)
    g.px_last = {c: float(v) for c, v in s_px.dropna().items()}

    # ---- 6) 过滤 ----
    ok = dy.notna() & (dy >= MIN_DY)
    if MAX_DY is not None:
        ok &= (dy <= MAX_DY)
    if MIN_DIV3:
        ok &= (n_div3 >= MIN_DIV3)

    # ---- 7) 行业标签（只有 indpct 需要）----
    # ⚠️ 不能写 `pd.Series('未分类', index=pool, dtype=object)`！
    #    老 pandas 会把**标量字符串**当 list-like 走 dtype 推断 →
    #    `np.dtype('未分类')` → UnicodeEncodeError: 'ascii' codec can't encode ...
    #    （2026-09-17 聚宽实跑就是这个栈：series.py:275 _sanitize_array → maybe_cast_to_datetime）
    #    → 必须显式给**等长列表**。
    ind = pd.Series([UNKNOWN_IND] * len(pool), index=pool, dtype=object)
    if FORM == 'indpct':
        ind = pd.Series(_industries(pool, sel_date)).reindex(pool).fillna(UNKNOWN_IND)
        ok &= (ind != UNKNOWN_IND)     # 无行业标签无法中性化（与本地一致）
    ok = ok.fillna(False).astype(bool)

    dy2 = dy[ok]
    diag['候选'] = len(dy2)
    if dy2.empty:
        return _empty_picks(), diag
    ind2 = ind.reindex(dy2.index).fillna(UNKNOWN_IND)

    # ---- 8) 排序取前 N ----
    if FORM == 'indpct':
        # 行业内百分位（每行业都是 [0,1] 均匀分布）→ 全局取前 N → 天然行业近似等权
        # ⚠️ 用 `ind2.values`（按位置分组）而不是传 Series（依赖索引对齐）——
        #    `ind2` 本来就是 reindex 到 `dy2.index` 的，两者等价，但按位置在老 pandas 上更稳
        score = dy2.groupby(ind2.values).rank(pct=True)
    else:
        # 'naked' / 'topn'：全局按 dy_ttm 排名
        score = dy2
    n = min(TOPN, len(score))
    sel = score.sort_values(ascending=False).index[:n].tolist()

    return pd.DataFrame({'code': sel,
                         'dy': dy2.reindex(sel).values,
                         'ind': ind2.reindex(sel).values}), diag


def _empty_picks():
    return pd.DataFrame({'code': [], 'dy': [], 'ind': []})


def _batched(fn, items, axis=0):
    """把「一次要 5000+ 只股票」的调用切成 `BATCH` 只一批。

    为什么要这个：全 A 一次性 `get_price` / `get_extras` / `get_industry`
    在聚宽上很容易变慢甚至超时 —— 第一次跑就挂在这种地方最浪费时间。
    某批失败只跳过该批，不影响整体（并在日志里提示）。
    """
    frames = []
    for i in range(0, len(items), BATCH):
        chunk = list(items[i:i + BATCH])
        try:
            part = fn(chunk)
        except Exception as e:
            # ⚠️ 模板保持纯 ASCII（异常消息可能是中文 unicode）
            log.warning('batch {}~{} failed: {}'.format(i, i + len(chunk), e))
            continue
        if part is None or not len(part):
            continue
        frames.append(part)
    if not frames:
        return None
    return pd.concat(frames, axis=axis)


def _as_long(df, codes):
    """`panel=False` 多标的返回长表（time/code/字段）；单标的可能只有 index+字段，统一成长表。"""
    if df is None:
        return pd.DataFrame(columns=['time', 'code'])
    d = df
    if 'code' not in d.columns:
        d = d.reset_index()
        if len(d.columns) and d.columns[0] != 'time':
            d = d.rename(columns={d.columns[0]: 'time'})
        d['code'] = codes[0]
    return d


def _yield_at(sel_date, pool):
    """近 365 天已除权的每股现金分红（送转调整到当前股本口径）+ 近 3 年现金分红次数。

    与本地 `tools/test_dividend_factor.py::build_yield_panel` 完全同口径：
      dps_ttm(t) = Σ_{t-365 < a_xr_date ≤ t}  (bonus_ratio_rmb/10) × Π_{j>i}(1 + r_j)
      n_div3(t)  = #{ t-1095 < a_xr_date ≤ t 且 现金分红 > 0 }
    其中 `r_j = (送股比例 + 转增比例)/10`，`adj_i = Π_{j>i}(1+r_j)` 把历史 DPS 换算到
    **当前股本口径**（否则与当前价不可比 —— 老股票送转过之后每股分红口径会变）。

    ⚠️ 只用 `a_xr_date <= sel_date` 的分红（已除权口径），不依赖公告时点 → 无前视。
    """
    d = g.div
    if d is None or not len(d):
        return pd.Series(dtype=float), pd.Series(dtype=int)
    t = pd.Timestamp(sel_date)
    lo365, lo3y = t - pd.Timedelta(days=365), t - pd.Timedelta(days=1095)
    dd = d[d.a_xr_date <= t]

    if ADJ_MODE == 'correct':
        # 每股分红换算到 **t 时刻** 的股本口径：
        #   dps_i(t) = cash_i × Π_{k<i}(1+r_k) / Π_{k≤t}(1+r_k)
        # ⚠️ `P(t)` 必须**按股票分别连乘** —— 全市场一起乘会直接溢出（本次踩过）
        pt = dd.groupby('code')['r'].transform(lambda s: float(np.prod(1.0 + s.values)))
        dd = dd.assign(dpv=(dd.dps_cc / pt.replace(0.0, np.nan)).fillna(0.0))
    else:
        dd = dd.assign(dpv=dd.dps)

    win = dd[dd.a_xr_date > lo365]
    dps = win.groupby('code')['dpv'].sum()

    win3 = dd[(dd.a_xr_date > lo3y) & dd.is_cash]
    n3 = win3.groupby('code').size()
    return dps, n3


def _industries(codes, sel_date):
    """一级行业名（分批拉，避免单次请求过大）。

    ⚠️ 与本地一样是「当前」标签、非 point-in-time（轻微前视，见 §6.3）。
    """
    out = {}
    for i in range(0, len(codes), BATCH):
        chunk = list(codes[i:i + BATCH])
        try:
            info = get_industry(chunk, date=sel_date)
        except Exception as e:
            # ⚠️ 模板保持纯 ASCII（异常消息可能是中文 unicode）
            log.warning('get_industry batch {}~{} failed ({}); -> unknown'.format(
                i, i + len(chunk), e))
            continue
        for c in chunk:
            seg = (info.get(c) or {}).get(SECTOR) or {}
            out[c] = seg.get('industry_name') or UNKNOWN_IND
    return out


# ==========================================================================
# 五、分红数据（聚宽 STK_XR_XD；字段名以官方数据字典为准）
# ==========================================================================
def _load_dividends(anchor, trade_end):
    """预取 [anchor-3年, trade_end] 的分红送配记录，并预处理成逐条 `dps`。

    取用字段：
        code                   股票代码（带后缀）
        a_xr_date              A股除权除息日  ← **point-in-time 的锚**
        bonus_ratio_rmb        派息比例（人民币），单位「每 10 股派 XX 元」
        dividend_ratio         送股比例（每 10 股送 XX 股）
        transfer_ratio         转增比例（每 10 股转增 XX 股）
        bonus_cancel_pub_date  取消分红公告日期（非空 → 该方案已取消，剔除）
    """
    start = (pd.Timestamp(anchor) - pd.Timedelta(days=1120)).strftime('%Y-%m-%d')
    q = query(finance.STK_XR_XD.code,
              finance.STK_XR_XD.a_xr_date,
              finance.STK_XR_XD.bonus_ratio_rmb,
              finance.STK_XR_XD.dividend_ratio,
              finance.STK_XR_XD.transfer_ratio,
              finance.STK_XR_XD.bonus_cancel_pub_date).filter(
        finance.STK_XR_XD.a_xr_date >= start,
        finance.STK_XR_XD.a_xr_date <= trade_end)
    try:
        raw = finance.run_offset_query(q)        # 自动分页（上限 20 万行）
    except Exception as e:
        # ⚠️ 模板保持纯 ASCII（异常消息可能是中文 unicode，Py2 下会 UnicodeDecodeError）
        log.warning('run_offset_query unavailable ({}); fallback to limit/offset'.format(e))
        raw = _paged_query(q)
    if raw is None or not len(raw):
        log.warning('⚠️ 分红记录为空 —— 股息率会全为 0，请检查 STK_XR_XD 权限/区间')
        return pd.DataFrame(columns=['code', 'a_xr_date', 'dps', 'is_cash'])

    d = raw.copy()
    d = d[d.a_xr_date.notna()]
    if 'bonus_cancel_pub_date' in d.columns:
        # ⚠️ 已知近似：该字段是「最终状态」，用它过滤带极轻微前视；
        #    但不剔除会把已取消的分红算进股息率（错误更严重）
        d = d[d.bonus_cancel_pub_date.isna()]
    d['a_xr_date'] = pd.to_datetime(d.a_xr_date)
    d['cash'] = pd.to_numeric(d.bonus_ratio_rmb, errors='coerce').fillna(0.0) / 10.0
    d['r'] = (pd.to_numeric(d.dividend_ratio, errors='coerce').fillna(0.0)
              + pd.to_numeric(d.transfer_ratio, errors='coerce').fillna(0.0)) / 10.0
    d = d[(d.cash > 0) | (d.r > 0)]
    d = d.sort_values(['code', 'a_xr_date']).reset_index(drop=True)
    if not len(d):
        return pd.DataFrame(columns=['code', 'a_xr_date', 'dps', 'is_cash'])

    # ---- 两种送转调整口径（见顶部 ADJ_MODE 说明）----
    grp = d.groupby('code', sort=False)['r']
    cum_incl = grp.transform(lambda s: (1.0 + s)[::-1].cumprod()[::-1])   # Π_{k>=i}
    cum_le = grp.transform(lambda s: (1.0 + s).cumprod())                 # Π_{k<=i}
    d['adj'] = cum_incl / (1.0 + d['r'])                # legacy: Π_{k>i}(1+r_k)
    d['dps'] = d['cash'] * d['adj']                     # legacy 口径的逐笔每股分红
    d['cum_prev'] = cum_le / (1.0 + d['r'])             # Π_{k<i}(1+r_k)
    d['dps_cc'] = d['cash'] * d['cum_prev']             # correct 口径的分子（用时再除以 P(t)）
    d['is_cash'] = d['cash'] > 0
    # ⚠️ 防「静默截断」：行数上限如果生效，最新的除权日会比 TRADE_END 早很多
    mx = d.a_xr_date.max().strftime('%Y-%m-%d') if len(d) else '—'
    log.info('  分红记录已就绪 {} 条 / {} 只（最新除权日 {}）'.format(
        len(d), d.code.nunique(), mx))
    if len(d) and mx < (pd.Timestamp(trade_end) - pd.Timedelta(days=150)).strftime('%Y-%m-%d'):
        log.warning('  ⚠️ 最新除权日 {} 距 {} 超过 150 天 → 可能被行数上限截断，请检查'.format(
            mx, trade_end))
    return d[['code', 'a_xr_date', 'r', 'dps', 'dps_cc', 'is_cash']]


def _paged_query(q, page=3000, hard_limit=200000):
    """`finance.run_query` 单次有行数上限 → 用 limit/offset 分页拼全。"""
    frames, off = [], 0
    while off < hard_limit:
        part = finance.run_query(q.limit(page).offset(off))
        if part is None or not len(part):
            break
        frames.append(part)
        if len(part) < page:
            break
        off += page
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
