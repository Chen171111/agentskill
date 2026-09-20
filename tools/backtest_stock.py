"""个股多因子模型：因子构建 + 横截面打分 + 周频回测。

为什么不用 agentskill 的 BacktestEngine
--------------------------------------
引擎吃的是**宽表面板**（date × code）。个股票池 5000 只 × 2100 日 × 5 字段的宽表
约 600MB+，再叠加 20 个因子面板会到 GB 级，风险太大。故改用**长表**实现，
内存可控；同时把引擎里的 A 股机制照搬过来：

- **T+1**：T 日收盘出信号，T+1 **开盘**成交
- **涨跌停不可成交**：一字涨停买不进、一字跌停卖不出（主板 ±10%、创业板/科创板 ±20%）
- **停牌**：当日成交量 0 视为停牌，不可成交
- **成本**：买 佣金+滑点，卖 佣金+印花税+滑点
- **股票池**：逐日可得（上市满 N 日、20 日均成交额、价格下限、非 ST），避免前视

持仓用**单位数（units）**追踪，组合价值 = 现金 + Σ units×close，精确反映成本与
不可成交的影响。

用法
----
    PY=.../python.exe
    $PY tools/backtest_stock.py --bars data/stockbars/bars.parquet \
        --universe data/stockbars/universe.csv --start 20200101 --end 20260911
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.costs import COST_BUY, COST_SELL, ENGINE_SLIPPAGE  # noqa: E402

TRADING_DAYS = 244.0

# 调仓所需的**最低候选池规模**。候选池（池子过滤 + 因子非空后）少于该值时，
# 横截面排名没有意义，本次不调仓。
#
# ⚠️ 原实现是 `if len(cs) >= topk * 2:`，缺陷是：池子偏小时会**静默跳过整个调仓**，
# 组合滞留在旧持仓（建仓初期甚至是**满仓现金**），且不报错、不计入 meta。
# 实测（2026-09-15）：`topk=800 @ hold=20` 因建仓初期池子 1427 < 1600 而空仓错过
# 2019-01~03（该区间全池 +39.5%），年化从 16.31% 被压到 11.26%。
# 这直接伪造出「topk 超过 700 后 alpha 转负」的假象（见 docs/落地路径与资金门槛.md §12）。
MIN_POOL = 100

DEFAULT_FACTORS = [
    ("rev20", 1.0), ("rev60", 1.0), ("rev120", 1.0),
    ("rev5", 1.0), ("vol20", 1.0), ("max20", 1.0),
    ("turn20", 1.0), ("illiq20", 1.0),
]
BIGORDER_FACTORS = [("bo_n_rec", 1.0), ("bo_flow_ratio", 1.0),
                    ("bo_tail_share", 1.0)]


# ================================================================ 因子构建
def build_features(bars: pd.DataFrame) -> pd.DataFrame:
    """长表 -> 加入各因子列。所有因子统一为「越大越看好」。

    ⚠️ 方向依据（全量 4985 只 / 1985 交易日实测，见 tools/test_stock_factors.py）：
    A 股 2018~2026 是**中期反转市**，20/60/120 日动量 IC **全为负**且分组严格
    单调递减 → 故这里把动量**取负号**并命名为 `revN`（反转因子）。
    非流动性溢价存在（`illiq20` 取正号），故 `illiq20 = +mean(|ret|/成交额)`。
    """
    df = bars.sort_values(["code", "date"]).copy()
    g = df.groupby("code", sort=False)

    df["ret1"] = g.close.pct_change()
    # 反转族：过去 N 日涨幅取负（A 股中期反转）
    df["rev5"] = -g.close.transform(lambda s: s / s.shift(5) - 1)
    df["rev20"] = -g.close.transform(lambda s: s / s.shift(20) - 1)
    df["rev60"] = -g.close.transform(lambda s: s / s.shift(60) - 1)
    df["rev120"] = -g.close.transform(lambda s: s / s.shift(120) - 1)

    df["vol20"] = -g.ret1.transform(lambda s: s.rolling(20).std())
    df["vol60"] = -g.ret1.transform(lambda s: s.rolling(60).std())
    df["max20"] = -g.ret1.transform(lambda s: s.rolling(20).max())

    df["amt"] = df.close * df.volume * 100.0          # 元（腾讯 volume 单位=手）
    # 有**真实成交额**时优先用它。旧 fqkline 只返回 6 个字段（无成交额），
    # 故历史上只能用 close×volume×100 估算（与真实值差一个 VWAP/close 的比例，
    # 通常几个百分点）。newfqkline 直接给成交额，见 tools/fetch_stock_bfq.py。
    if "amount" in df.columns and df.amount.notna().any():
        df["amt"] = df.amount.astype(float).fillna(df["amt"])
    df["amt_ma20"] = g.amt.transform(lambda s: s.rolling(20).mean())
    df["turn20"] = -g.volume.transform(lambda s: s / s.rolling(20).mean())

    # Amihud 非流动性：|ret| / 成交额（取正号 → 非流动性溢价）
    df["_illiq"] = df.ret1.abs() / df.amt.replace(0, np.nan)
    df["illiq20"] = df.groupby("code", sort=False)._illiq.transform(
        lambda s: s.rolling(20).mean())
    df.drop(columns=["_illiq"], inplace=True)

    df["listed"] = df.groupby("code", sort=False).cumcount() + 1
    df["ma60"] = g.close.transform(lambda s: s.rolling(60).mean())

    df["prev_close"] = g.close.shift(1)

    # ── 涨跌停判定（2026-09-17 重写：原实现有两处缺陷）──────────────────────
    # 缺陷① **分档不完整**。原为「68/30 前缀 → 20%，其余 10%」。实际制度：
    #   · 科创板 688/689        → 20%（2019-07-22 起）
    #   · 创业板 30x            → **2020-08-24 注册制起才是 20%**，之前是 10%
    #   · 北交所 BJ43/83/87/88  → 30%
    #   · 其余（沪深主板）      → 10%
    #   （⚠️ ST 的 5% **未建模**：池子已按 `universe.is_st` 剔除，但那是**当前快照**、
    #     非 point-in-time → 池内中途转 ST 的会漏判。已知残留，见重验报告 §六。）
    # 缺陷② **`.round(2)` 打在"价格"上**。本数据是**总收益指数价**（含分红回放），
    #   不是真实价；而「涨停价按分四舍五入到分」只对真实价成立。在指数价上 round
    #   会让阈值有约一半概率偏高一格 → **实测漏判 40.6% 的一字涨停**
    #   （主板判据从应捕获的 17,929 行掉到 10,721 行）。
    #   → 改为**比例判据 + 半格容差**：真实涨停价的相对涨幅 ∈ [lim − 0.005/prev_real, …)，
    #     故门槛取 `lim − 0.005/prev_real`。`prev_real` 由当日 `px_real / close`
    #     换算（`prepare()` 会并入真实价；同一天同一比例，非除权日等价）。
    dat = df.date.astype(str).str.replace("-", "", regex=False)
    p2_5 = df.code.str[2:5]
    board = np.where(df.code.str[:2] == "BJ", "北交",
                     np.where(p2_5.str.startswith("68"), "科创",
                              np.where(p2_5.str.startswith("30"), "创业", "主板")))
    lim = np.select(
        [board == "北交",
         board == "科创",
         (board == "创业") & (dat >= "20200824"),
         board == "创业"],
        [0.30, 0.20, 0.20, 0.10],
        default=0.10,
    ).astype(float)
    # 半格容差（0.005 元 ÷ 昨日价格，折算成比例）。优先用真实价，缺失时退回指数价
    ref = df["prev_close"].abs()
    if "px_real" in df.columns:
        cand = ref * (df["px_real"] / df["close"]).abs()
        ref = cand.where(cand.notna() & (cand > 0), ref)
    tol = (0.005 / ref.clip(lower=0.01)).clip(upper=0.05)
    r_open = df["open"] / df["prev_close"] - 1.0
    df["limit"] = lim
    df["buy_blocked"] = (r_open >= (lim - tol)).fillna(False)
    df["sell_blocked"] = (r_open <= (-lim + tol)).fillna(False)
    df["suspended"] = df.volume.fillna(0) <= 0
    return df


def attach_bigorder(df: pd.DataFrame, path: str) -> pd.DataFrame:
    """并入精灵大单特征（仅 2022-10~2024-07 有值，其余 NaN）。"""
    f = pd.read_parquet(path)
    cols = {"n_rec": "bo_n_rec", "flow_ratio": "bo_flow_ratio",
            "tail_share": "bo_tail_share", "am_net_ratio": "bo_am_net"}
    keep = ["date", "code"] + [c for c in cols if c in f.columns]
    f = f[keep].rename(columns=cols)
    # 大单笔数多 / 参与率高 / 尾盘集中 → 均为负面，取负号统一方向
    for c in ("bo_n_rec", "bo_flow_ratio", "bo_tail_share"):
        if c in f.columns:
            f[c] = -f[c]
    return df.merge(f, on=["date", "code"], how="left")


# ================================================================ 回测
def _date_view(cur: pd.DataFrame) -> dict:
    """把某日横截面转成若干 dict，便于 O(1) 查询。"""
    idx = cur.set_index("code")
    return {
        "open": idx["open"].to_dict(),
        "close": idx["close"].to_dict(),
        "susp": idx["suspended"].to_dict(),
        "bb": idx["buy_blocked"].to_dict(),
        "sb": idx["sell_blocked"].to_dict(),
    }


# ⚠️ 成本口径（2026-09-17 修正，**改动会牵动全部个股线的绝对数字**）
# ---------------------------------------------------------------------------
# 默认值原为 `cost_buy=0.0003 / cost_sell=0.0013`（佣金**万3**），但
# `tools/backtest_dividend.py::MODELED_ROUND` 与所有文档都按**万5**建模
# （`config.TRADING_COST.commission_rate = 0.0005`，主人实际费率）。
# 而 `cost_buy/cost_sell` 是 keyword-only，**全库没有任何调用方传过它**
# （`grep -rn cost_buy tools/*.py | grep -v backtest_stock` 为空）
# → 引擎实际 round-trip 0.0026，而事后调整式按 0.0030 扣 → 所有「10 万真实」
#   数字**少扣 0.0004 × 年单边换手**（定稿 −0.11pp/年、多因子线 −0.33pp/年）。
# 现改为与 MODELED_ROUND 自洽：
#     (0.0005 + 0.0005) + (0.0015 + 0.0005) = 0.0030 = MODELED_ROUND ✓
# 拆解：买入佣金 万5 + 卖出佣金 万5 + 卖出印花税 万10（法定）+ 滑点 单边 万5。
def run(df, factor_specs, *, start, end, topk=50, hold=5, min_listed=120,
        min_amount=3e7, min_price=2.0, cost_buy=COST_BUY, cost_sell=COST_SELL,
        slippage=ENGINE_SLIPPAGE, trend_filter=False, weight_mode="equal",
        tilt_min=0.55, buffer=0.0, writeoff_factor=1.0, cond_col=None,
        div_tax=None, tax_rate=0.0, verbose=False):
    """调仓回测。

    cond_col（条件选股模式，社区策略用）
    ------------------------------------
    传入一个**布尔列名**时切换到「条件选股」模式：每日取该列为 True 的股票
    **等权持有**，无信号则清仓。此时 `factor_specs` 传空列表，`hold=1`
    （持有期已由信号掩码编码，见 `tools/tdx_strategies.py::build_signal`）。
    默认 `None` = 原因子打分模式，行为不变。

    weight_mode
    -----------
    - `"equal"`：TopK 等权（原行为）
    - `"rank"` ：TopK 内按综合分**排名加权**（分数越高权重越大）
    - `"tilt"` ：全池按综合分**倾斜加权**（分数 > `tilt_min` 的全部持有，
                 权重 ∝ 分数），保留基准 beta 的同时施加因子倾斜

    buffer（仅 tilt 模式生效）
    -------------------------
    **滞回缓冲带**，专治阈值附近反复进出的换手。tilt 每周对全池重算分数，
    分数在 `tilt_min` 上下来回蹭的股票会被反复买卖（换手的主要来源）。
    设 buffer 后引入不对称阈值：

    - 已持仓：分数跌破 `tilt_min − buffer` 才卖出（更晚卖）
    - 未持仓：分数升破 `tilt_min + buffer` 才买入（更晚买）

    中间地带的股票维持原状。`buffer=0` 时退化为原逻辑。

    writeoff_factor（退市处置价）
    ---------------------------
    连续 `MAX_GAP` 个交易日无数据即视为退市，按 `最后收盘价 × writeoff_factor`
    了结头寸。`1.0` = 假设退市前能全身而退（**偏乐观**，是默认值）；
    `0.0` = 假设血本无归（最保守）。真实情况介于两者之间：退市整理期已计入
    行情（跌幅已在最后收盘价里），但退市后转三板、流动性枯竭，实际变现价通常更低。
    本参数用于量化这个假设对结论的影响幅度。

    div_tax / tax_rate（2026-09-20 新增，D4「引擎内扣红利税」）
    ---------------------------------------------------------
    **让「税后」不再需要换一份数据文件。**
    - `div_tax`：DataFrame（列 `date, code, dps_adj`），由 `tools/build_div_tax.py` 生成 ——
                 给出**除权日**该股「按本价格口径换算后的每股税前派现」（送转不计税）。
    - `tax_rate`：现金分红的红利税率（0~0.2）。**`0` = 不扣税 = 默认 = 与旧版行为完全一致。**

    ⚠️ **为什么必须做成「引擎内」，而不是「换一份税后价格」**：
    原先的税后口径是 `--bars bars_total_tax10.parquet`，那份 `close` 已经按税后金额回放了分红。
    而 `close` 会进入**价格类过滤与动量因子**（`min_price >= 2.0`、`rev20`/`rev60`、涨跌停判据）
    → **税后版与无税版选出来的股票不一样**，于是长窗口 `税后 − 无税` 的 Δ
    **混杂了「换了组合」的成分，不能读作「税成本」**（见 `docs/HANDOFF_项目总结与下一步.md` §5.1-7）。

    引擎内扣税时**价格路径完全不变**（仍用 `bars_total`，税前）→ 两边**组合恒等**，
    Δ 才干净地等于税负。扣税发生在**除权日**、用**该日开盘前的持仓**（= 前一日收盘持仓，
    与「股权登记日 T−1 收盘持有者可分红」一致），直接从 `cash` 扣除。
    口径换算（`dps_adj = D × bars_total.close(t−1)/bars_bfq.close(t−1)`）见 `tools/build_div_tax.py`。
    """
    d = df[(df.date >= start) & (df.date <= end)]
    dates = sorted(d.date.unique())
    if len(dates) < hold * 4:
        raise ValueError("区间过短: {} 天".format(len(dates)))

    # 只保留必需列，显著降低内存（大票池下这一步很关键）
    need = ["date", "code", "open", "close", "prev_close", "suspended",
            "buy_blocked", "sell_blocked", "listed", "amt_ma20", "ret1", "ma60"]
    need += [f for f, _ in factor_specs if f not in need]
    if cond_col and cond_col not in need:
        need.append(cond_col)
    d = d[[c for c in need if c in d.columns]].copy()

    # 逐日候选池掩码（向量化，避免循环里重算）—— 必须在切分 by_date 之前加列
    uni = ((d.listed >= min_listed) & (d.amt_ma20 >= min_amount)
           & (d.close >= min_price) & (~d.suspended))
    d = d.assign(_in_uni=uni)
    by_date = {dt: g for dt, g in d.groupby("date", sort=True)}
    cache = {}

    def view(dt):
        if dt not in cache:
            cache[dt] = _date_view(by_date[dt])
            if len(cache) > 8:                     # 控制内存，只留最近几个
                for k in list(cache)[:-4]:
                    cache.pop(k, None)
        return cache[dt]

    tot_w = sum(w for _, w in factor_specs) or 1.0
    cash, units = 1.0, {}
    nav_hist, nav_dates = [], []
    hold_sizes = []
    trades = []
    pending = None
    last_close = {}
    last_seen = {}
    n_writeoff = 0
    n_skip = 0                        # 因候选池过薄而未调仓的次数（原实现静默，现计数）
    tax_paid = 0.0                    # 引擎内累计扣掉的红利税（占初始净值比）
    n_tax_events = 0                  # 发生扣税的 (除权日 × 持仓股) 次数
    MAX_GAP = 60                      # 连续无数据超过该天数视为退市，了结头寸

    # 引擎内扣税：把 div_tax 预处理成 {date: {code: dps_adj}}（tax_rate=0 时完全跳过）
    div_by_date = {}
    if tax_rate and div_tax is not None and len(div_tax):
        for _dt, _g in div_tax.groupby("date", sort=False):
            div_by_date[str(_dt)] = dict(zip(_g.code, _g.dps_adj))

    rebal_pos = set(range(0, len(dates) - 1, hold))

    for i, dt in enumerate(dates):
        cur = by_date[dt]
        v = view(dt)

        # ---------- 0) 退市安全：长期无数据的持仓按最后价格了结 ----------
        for c in list(units):
            if c in v["close"]:
                last_seen[c] = i
            elif i - last_seen.get(c, i) > MAX_GAP:
                px = last_close.get(c, 0.0) * writeoff_factor
                cash += units.pop(c) * px * (1 - cost_sell - slippage)
                trades.append((dt, c, "writeoff"))
                n_writeoff += 1

        # ---------- 0.5) 除权日扣红利税（引擎内；价格路径不变）----------
        # 用**本日开盘前**的持仓（= 前一日收盘持仓），与「股权登记日 T−1 收盘持有者
        # 才享受分红」的规则一致。只动 cash，不动 units —— 组合与无税版逐位相同。
        if div_by_date:
            _ev = div_by_date.get(dt)
            if _ev:
                for c in list(units):
                    _dps = _ev.get(c)
                    if _dps:
                        _t = units[c] * _dps * tax_rate
                        if _t > 0:
                            cash -= _t
                            tax_paid += _t
                            n_tax_events += 1

        # ---------- 1) 开盘执行上一交易日收盘产生的信号 ----------
        if pending is not None:
            sel_w = pending                      # [(code, weight), ...]
            sel = {c for c, _ in sel_w}
            pending = None
            tot_open = cash
            for c, u in units.items():
                px = v["open"].get(c) or last_close.get(c)
                tot_open += u * px if px else 0.0
            # 先卖：不在新组合里的
            for c in list(units):
                if c in sel:
                    continue
                if v["susp"].get(c, True) or v["sb"].get(c, True):
                    continue                        # 停牌/一字跌停卖不出
                op = v["open"].get(c)
                if not op:
                    continue
                cash += units.pop(c) * op * (1 - cost_sell - slippage)
                trades.append((dt, c, "sell"))
            # 再买：按目标权重分配
            for c, w in sel_w:
                if c in units:
                    continue
                if v["susp"].get(c, True) or v["bb"].get(c, True):
                    continue                        # 停牌/一字涨停买不进
                op = v["open"].get(c)
                if not op or op <= 0:
                    continue
                alloc = min(tot_open * w, cash)
                if alloc <= 1e-9:
                    continue
                units[c] = alloc / (op * (1 + cost_buy + slippage))
                cash -= alloc
                trades.append((dt, c, "buy"))

        # ---------- 2) 收盘记账 ----------
        nav = cash
        for c, u in units.items():
            px = v["close"].get(c)
            if px:
                last_close[c] = px
            nav += u * last_close.get(c, 0.0)
        nav_hist.append(nav)
        nav_dates.append(dt)
        hold_sizes.append(len(units))

        # ---------- 3) 收盘后出信号（每 hold 日一次） ----------
        if i in rebal_pos:
            cs = cur[cur._in_uni]
            if trend_filter:
                cs = cs[cs.close > cs.ma60]
            if cond_col is not None:
                # ── 条件选股模式（社区策略）：满足条件的等权持有，无信号清仓 ──
                cs = cs.dropna(subset=[cond_col])
                sel = cs[cs[cond_col].astype(bool)]
                if len(sel):
                    wv = np.ones(len(sel))
                    wv = wv / wv.sum()
                    pending = list(zip(sel.code.tolist(), wv.tolist()))
                else:
                    pending = []            # 空列表 → 执行阶段全部卖出
            else:
                cs = cs.dropna(subset=[f for f, _ in factor_specs])
                if len(cs) < MIN_POOL:
                    # 候选池过薄 → 本次不调仓。计数进 meta，不再静默（原为 `>= topk*2`）
                    n_skip += 1
                elif len(cs) >= 1:
                    score = None
                    for f, w in factor_specs:
                        rk = cs[f].rank(pct=True) * (w / tot_w)
                        score = rk if score is None else score + rk
                    cs = cs.assign(_score=score)
                    if weight_mode == "tilt":
                        if buffer > 0:
                            # 滞回：已持仓放宽卖出线，未持仓抬高买入线
                            held = cs.code.isin(set(units))
                            sel = cs[(held & (cs._score > tilt_min - buffer))
                                     | ((~held) & (cs._score > tilt_min + buffer))]
                        else:
                            sel = cs[cs._score > tilt_min]
                        if len(sel) < topk:
                            sel = cs.nlargest(topk, "_score")
                        wv = np.clip(sel._score.values - tilt_min, 1e-6, None)
                    else:
                        sel = cs.nlargest(topk, "_score")
                        if weight_mode == "rank":
                            wv = sel._score.values - sel._score.values.min() + 1e-9
                        else:
                            wv = np.ones(len(sel))
                    wv = wv / wv.sum()
                    pending = list(zip(sel.code.tolist(), wv.tolist()))

    eq = pd.DataFrame({"date": nav_dates, "equity": nav_hist}).set_index("date")
    eq["rate"] = eq.equity.pct_change().fillna(0.0)
    meta = {"n_writeoff": n_writeoff, "n_trades": len(trades),
            "n_skip": n_skip,
            "tax_paid": tax_paid, "n_tax_events": n_tax_events,
            "avg_hold": float(np.mean(hold_sizes)) if hold_sizes else 0.0}
    return eq, pd.DataFrame(trades, columns=["date", "code", "side"]), meta


def metrics(eq: pd.Series) -> dict:
    if len(eq) < 2:
        return {}
    ret = eq.pct_change().dropna()
    years = len(eq) / TRADING_DAYS
    cum = eq.iloc[-1] / eq.iloc[0] - 1
    ann = (1 + cum) ** (1 / years) - 1 if years > 0 else 0.0
    vol = ret.std() * np.sqrt(TRADING_DAYS)
    sharpe = ret.mean() / ret.std() * np.sqrt(TRADING_DAYS) if ret.std() > 0 else 0.0
    mdd = float((eq / eq.cummax() - 1).min())
    return {
        "累计收益": cum * 100, "年化收益": ann * 100, "年化波动": vol * 100,
        "夏普比率": sharpe, "最大回撤": mdd * 100,
        "卡玛比率": (ann / abs(mdd)) if mdd else 0.0,
        "胜率": float((ret > 0).mean()) * 100,
    }


def fmt(tag, m):
    return ("  {:<22} 年化 {:>8.2f}%  累计 {:>9.2f}%  波动 {:>7.2f}%  "
            "夏普 {:>6.2f}  回撤 {:>8.2f}%  卡玛 {:>5.2f}").format(
        tag, m.get("年化收益", 0), m.get("累计收益", 0), m.get("年化波动", 0),
        m.get("夏普比率", 0), m.get("最大回撤", 0), m.get("卡玛比率", 0))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="个股多因子回测")
    ap.add_argument("--bars", required=True)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--bigorder", default=None, help="精灵大单特征 parquet（可选）")
    ap.add_argument("--start", default="20200101")
    ap.add_argument("--end", default="20260911")
    ap.add_argument("--topk", type=int, default=50)
    ap.add_argument("--hold", type=int, default=5)
    ap.add_argument("--min-amount", type=float, default=3e7)
    ap.add_argument("--min-listed", type=int, default=120)
    ap.add_argument("--min-price", type=float, default=2.0)
    ap.add_argument("--trend-filter", action="store_true")
    ap.add_argument("--weight-mode", default="equal",
                    choices=["equal", "rank", "tilt"],
                    help="equal=TopK等权 / rank=TopK排名加权 / tilt=全池倾斜加权")
    ap.add_argument("--tilt-min", type=float, default=0.55,
                    help="tilt 模式下的分数下限（综合分 0~1）")
    ap.add_argument("--buffer", type=float, default=0.0,
                    help="tilt 模式滞回缓冲带（0~1），压掉阈值附近反复进出的换手")
    ap.add_argument("--writeoff-factor", type=float, default=1.0,
                    help="退市处置价系数：1.0=按最后收盘价了结（偏乐观），0.0=血本无归")
    ap.add_argument("--with-bigorder", action="store_true",
                    help="把精灵大单因子加入组合（需 --bigorder）")
    ap.add_argument("--dump", default=None, help="把逐日净值写到该 parquet")
    ap.add_argument("--tax-rate", type=float, default=0.0,
                    help="现金分红红利税率（0~0.2）。**0 = 不扣税 = 默认**（与旧版行为一致）。"
                         "引擎内扣税：价格路径不变，只在除权日从现金扣除 → "
                         "税后与无税**组合恒等**，Δ 才可读作税成本")
    ap.add_argument("--div-tax", default="data/stockbars/div_tax_table.parquet",
                    help="引擎内扣税用的每股派现表（由 tools/build_div_tax.py 生成）")
    args = ap.parse_args(argv)

    bars = pd.read_parquet(args.bars)
    bars["date"] = bars.date.astype(str).str.replace("-", "", regex=False)
    if args.universe and os.path.exists(args.universe):
        u = pd.read_csv(args.universe, dtype=str)
        st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
        n0 = bars.code.nunique()
        bars = bars[~bars.code.isin(st)]
        n1 = bars.code.nunique()
        if n0 == n1:
            # ⚠️ 这是**正常情况**而非异常：`bars_all.parquet` 里本就不含 ST/退市股
            # （universe_all.csv 里 245 只 is_st=True 的代码在 bars 中一条都没有），
            # 故 ST 过滤在此退化为 no-op。原打印 "剔除 245 只：5260 -> 5260 只"
            # 会让人误以为过滤生效了 —— 静默 no-op 必须说清楚。
            print(f"ST/退市名单 {len(st)} 只均不在数据中（bars 已不含 ST）"
                  f"→ 过滤为 no-op：{n0} 只")
        else:
            print(f"剔除 ST/退市：{n0} -> {n1} 只（名单 {len(st)} 只）")
    print(f"日线: {len(bars):,} 行, {bars.code.nunique()} 只, "
          f"{bars.date.nunique()} 交易日 ({bars.date.min()} ~ {bars.date.max()})")

    print("计算因子…", flush=True)
    df = build_features(bars)
    factors = list(DEFAULT_FACTORS)
    if args.with_bigorder:
        if not args.bigorder:
            raise SystemExit("--with-bigorder 需要 --bigorder 指定特征文件")
        df = attach_bigorder(df, args.bigorder)
        have = df[["bo_n_rec"]].notna().mean().iloc[0]
        print(f"  并入精灵大单特征，非空比例 {have:.2%}（仅历史区间）")
        factors = factors + list(BIGORDER_FACTORS)
    print(f"  因子: {', '.join(f for f, _ in factors)}")

    div_tax = None
    if args.tax_rate:
        if not os.path.exists(args.div_tax):
            raise SystemExit(
                "--tax-rate 需要 --div-tax 指定的派现表，但未找到 {}："
                "先跑 $PY tools/build_div_tax.py".format(args.div_tax))
        div_tax = pd.read_parquet(args.div_tax)
        div_tax["date"] = div_tax.date.astype(str)
        print("引擎内扣红利税：税率 {:.0f}%，派现表 {}（{:,} 个除权日）".format(
            args.tax_rate * 100, args.div_tax, len(div_tax)))

    common = dict(start=args.start, end=args.end, topk=args.topk, hold=args.hold,
                  min_listed=args.min_listed, min_amount=args.min_amount,
                  min_price=args.min_price, trend_filter=args.trend_filter,
                  weight_mode=args.weight_mode, tilt_min=args.tilt_min,
                  buffer=args.buffer, writeoff_factor=args.writeoff_factor,
                  div_tax=div_tax, tax_rate=args.tax_rate)

    print(f"\n回测 {args.start} ~ {args.end}  topk={args.topk}  "
          f"调仓({args.hold}日)  趋势过滤={args.trend_filter}  "
          f"权重={args.weight_mode}  tilt_min={args.tilt_min}  "
          f"buffer={args.buffer}  退市处置价×{args.writeoff_factor}  "
          f"红利税率={args.tax_rate*100:.0f}%")
    print("=" * 104)

    eq, tr, meta = run(df, factors, **common)
    m = metrics(eq.equity)
    print(fmt("多因子组合", m))
    if len(tr):
        n_buy = int((tr.side == "buy").sum())
        n_sell = int((tr.side == "sell").sum())
        print(f"  交易 {len(tr)} 笔（买 {n_buy} / 卖 {n_sell} / 退市了结 {meta['n_writeoff']}）")
    if meta.get("n_skip"):
        print(f"  ⚠️ 有 {meta['n_skip']} 次调仓因候选池 < {MIN_POOL} 只被跳过"
              f"（组合滞留旧持仓）")

    # 基准：同期等权全池（同一股票池过滤，逐日等权）
    bench = []
    for dt, g in df[(df.date >= args.start) & (df.date <= args.end)].groupby("date"):
        u = ((g.listed >= args.min_listed) & (g.amt_ma20 >= args.min_amount)
             & (g.close >= args.min_price) & (~g.suspended))
        bench.append((dt, float(g.loc[u, "ret1"].mean())))
    b = pd.DataFrame(bench, columns=["date", "r"]).set_index("date").dropna()
    beq = (1 + b.r).cumprod()
    bm = metrics(beq)
    print(fmt("等权全池基准", bm))

    print("\n  超额: 年化 {:+.2f}pp  夏普 {:+.2f}  回撤 {:+.2f}pp".format(
        m.get("年化收益", 0) - bm.get("年化收益", 0),
        m.get("夏普比率", 0) - bm.get("夏普比率", 0),
        m.get("最大回撤", 0) - bm.get("最大回撤", 0)))

    print("\n=== 分年度 ===")
    eq2 = eq.copy()
    eq2["yr"] = eq2.index.str[:4]
    b2 = beq.copy()
    b2.index = pd.Index(b2.index, name="date")
    bdf = b2.to_frame("equity")
    bdf["yr"] = bdf.index.str[:4]
    for yr in sorted(eq2.yr.unique()):
        s = eq2[eq2.yr == yr].equity
        sb = bdf[bdf.yr == yr].equity
        if len(s) < 20:
            continue
        ms, mb = metrics(s), metrics(sb)
        print("  {}: 策略年化 {:>+8.2f}%  基准 {:>+8.2f}%  超额 {:>+8.2f}pp  "
              "(回撤 {:.1f}% vs {:.1f}%)".format(
                  yr, ms.get("年化收益", 0), mb.get("年化收益", 0),
                  ms.get("年化收益", 0) - mb.get("年化收益", 0),
                  ms.get("最大回撤", 0), mb.get("最大回撤", 0)))

    if args.dump:
        out = eq.copy()
        out["bench"] = beq.reindex(out.index)
        out.to_parquet(args.dump)
        print(f"\n净值已写出 {args.dump}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
