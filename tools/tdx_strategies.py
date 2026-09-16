"""通达信社区选股策略 → Python 翻译层 + 策略库

数据约束（决定了哪些公式能回测）
--------------------------------
`bars_all.parquet` 只有 OHLCV 五列，**没有**流通股本、财务数据、成本分布、
动态行情快照。因此凡使用下列函数的公式**无法回测**，不纳入策略库，
只在筛选报告里标注原因：

    CAPITAL / FINANCE(...) / COST(...) / DYNAINFO(...) / INDEXA / INDEXC

翻译偏差（必须在报告里如实披露）
--------------------------------
1. 含 `CAPITAL<=300000000`（流通盘限制）的公式，翻译时**剔除该条件**
   → 回测比原公式**更宽松**，选出的股票更多、质量可能更低。
2. 含 `HSL:=VOL/CAPITAL*100>2`（换手率）的，用 `VOL/MA(VOL,20)>1.5` 替代
   → 量纲不同，但方向一致（都表达"放量"）。
3. 含 `INDEXC>0.98*REF(INDEXC,1)`（大盘过滤）的，剔除该条件。
4. `BARSLAST` 类回溯条件用**简化形式**替代（见各策略 note）。
5. `DIFF<-0.1` 这类**绝对价位阈值**按 close 归一化（不同股价不可比）。

用法
----
    from tools.tdx_strategies import STRATEGIES, build_signal
    mask = build_signal(df, "ma_squeeze_3", hold=5)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ================================================================ 翻译层
def _gb(df):
    return df.groupby("code", sort=False)


def REF(df, s, n):
    """通达信 REF(X,N)：N 周期前的值"""
    return s.groupby(df["code"], sort=False).shift(n)


def MA(df, col, n):
    """简单移动平均"""
    return _gb(df)[col].transform(lambda x: x.rolling(n).mean())


def EMA(df, col, n):
    """指数移动平均"""
    return _gb(df)[col].transform(lambda x: x.ewm(span=n, adjust=False).mean())


def EMA_S(df, s, n):
    """对任意 Series 求 EMA（通达信 EMA(X,N) 作用于表达式时用）"""
    return s.groupby(df["code"], sort=False).transform(
        lambda x: x.ewm(span=n, adjust=False).mean())


def SMA(df, s, n, m):
    """通达信 SMA(X,N,M) = (M*X + (N-M)*SMA_prev)/N，等价于 ewm(alpha=M/N)"""
    return s.groupby(df["code"], sort=False).transform(
        lambda x: x.ewm(alpha=m / n, adjust=False).mean())


def HHV(df, col, n):
    return _gb(df)[col].transform(lambda x: x.rolling(n).max())


def LLV(df, col, n):
    return _gb(df)[col].transform(lambda x: x.rolling(n).min())


def COUNT(df, cond, n):
    """N 周期内条件成立的次数"""
    return cond.astype(float).groupby(df["code"], sort=False).transform(
        lambda x: x.rolling(n).sum())


def CROSS(df, a, b):
    """a 上穿 b（b 可为 Series 或标量）"""
    if np.isscalar(b):
        b = pd.Series(float(b), index=df.index)
    ap = a.groupby(df["code"], sort=False).shift(1)
    bp = b.groupby(df["code"], sort=False).shift(1)
    return (a > b) & (ap <= bp)


def ANGLE(df, s):
    """通达信 角度:=ATAN((MA/REF(MA,1)-1)*100)*180/3.1416"""
    prev = s.groupby(df["code"], sort=False).shift(1)
    return np.arctan((s / prev - 1) * 100) * 180 / np.pi


def MACD(df, fast=12, slow=26, sig=9):
    diff = EMA(df, "close", fast) - EMA(df, "close", slow)
    dea = EMA_S(df, diff, sig)
    return diff, dea, 2 * (diff - dea)


def KDJ(df, n=9, m1=3, m2=3):
    low_n, high_n = LLV(df, "low", n), HHV(df, "high", n)
    rsv = (df.close - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    k = SMA(df, rsv, m1, 1)
    d = SMA(df, k, m2, 1)
    return k, d


def _squeeze(df, series_list, pct):
    """多线粘合：最高线 / 最低线 - 1 < pct"""
    cat = pd.concat(series_list, axis=1)
    return (cat.max(axis=1) / cat.min(axis=1) - 1) < pct


# ================================================================ 策略库
def macd_low_gold(df):
    """低位金叉：MACD 零轴下方金叉"""
    diff, dea, _ = MACD(df)
    # 原式 DIFF<-0.1 是绝对价位，对不同股价不可比 → 按 close 归一化
    return CROSS(df, diff, dea) & (diff / df.close < -0.002)


def macd_second_gold(df):
    """二次金叉：零轴下方、21 日内第二次金叉"""
    diff, dea, _ = MACD(df)
    gold = CROSS(df, diff, dea)
    # 原式用 COUNT(JCCOUNT=2,21)=1，此处简化为「21日内至少2次金叉」
    return gold & (dea < 0) & (COUNT(df, gold, 21) >= 2)


def ma_squeeze_3(df):
    """三线粘合：5/10/20 日均线收敛 + 放量收阳"""
    m5, m10, m20 = MA(df, "close", 5), MA(df, "close", 10), MA(df, "close", 20)
    v5 = MA(df, "volume", 5)
    return (_squeeze(df, [m5, m10, m20], 0.03)
            & (df.volume > REF(df, df.volume, 1) * 1.5)
            & (df.volume > v5) & (df.close > df.open))


def ma_squeeze_4(df):
    """四线粘合：EMA 5/10/15/30/60 收敛后快线上穿"""
    e5, e10 = EMA(df, "close", 5), EMA(df, "close", 10)
    e15, e30, e60 = EMA(df, "close", 15), EMA(df, "close", 30), EMA(df, "close", 60)
    # 原式分三组比较 EMA10/20/30 与 EMA60 的比值，此处用五线整体收敛近似
    return _squeeze(df, [e5, e10, e15, e30, e60], 0.02) & CROSS(df, e5, e10)


def xushi_daifa(df):
    """蓄势待发：短中期 EMA 高度粘合后快线上穿（严格照搬原式）"""
    v0, e10 = EMA(df, "close", 5), EMA(df, "close", 10)
    e20, e30, e60 = EMA(df, "close", 20), EMA(df, "close", 30), EMA(df, "close", 60)
    m1 = (1000 * e10 / e60 <= 1015) & (1000 * e10 / e60 >= 975)
    m2 = (1000 * e20 / e60 <= 1020) & (1000 * e20 / e60 >= 980)
    m3 = (1000 * e30 / e60 <= 1015) & (1000 * e30 / e60 >= 985)
    m4 = m1 & m2 & m3 & CROSS(df, v0, e10)
    m5 = m1 & m2 & m3 & CROSS(df, v0, e30)
    t1 = (1000 * e10 / e30 <= 1010) & (1000 * e10 / e30 >= 990)
    t2 = (1000 * e20 / e30 <= 1010) & (1000 * e20 / e30 >= 990)
    t3 = t1 & t2 & CROSS(df, v0, e10)
    t4 = t1 & t2 & CROSS(df, v0, e30)
    return m4 | m5 | t3 | t4


def multi_ma_angle(df):
    """强势股四均线角度：3/5/10/20 日均线角度达阈值 + 年线向上（严格照搬）"""
    m3, m5 = MA(df, "close", 3), MA(df, "close", 5)
    m10, m20 = MA(df, "close", 10), MA(df, "close", 20)
    m250 = MA(df, "close", 250)
    return ((ANGLE(df, m3) > 45) & (ANGLE(df, m5) > 45)
            & (ANGLE(df, m10) > 60) & (ANGLE(df, m20) > 45)
            & (m250 > REF(df, m250, 1)))


def break_ma21(df):
    """紫色冲关：多均线向上 + 突破 21 日线 + 收阳 >1.5% + 放量"""
    m3, m5, m8 = MA(df, "close", 3), MA(df, "close", 5), MA(df, "close", 8)
    m13, m21 = MA(df, "close", 13), MA(df, "close", 21)
    qsxs = (m8 > REF(df, m8, 1)) & (m3 > REF(df, m3, 1)) & (m5 > REF(df, m5, 1))
    dxjc = CROSS(df, df.close, m21) & (df.close > m13) & (df.close / df.open > 1.015)
    # 原式 HSL:=VOL/CAPITAL*100>2 需要流通股本 → 用「量/20日均量>1.5」替代
    hsl = df.volume / MA(df, "volume", 20) > 1.5
    return qsxs & dxjc & hsl


def kd_macd_bull(df):
    """横盘是银：2日内KD金叉 + 5日内MACD金叉 + 均线多头 + 站上5日线（严格照搬）"""
    k, d = KDJ(df)
    _, _, macd2 = MACD(df)
    m5, m10, m20 = MA(df, "close", 5), MA(df, "close", 10), MA(df, "close", 20)
    return (COUNT(df, CROSS(df, k, d), 2) >= 1) & (COUNT(df, CROSS(df, macd2, 0.0), 5) >= 1) \
        & (m5 > m10) & (m10 > m20) & (df.close > m5)


def high20_break(df):
    """枪挑小梁王：突破 20 日高点且 5 日内首次"""
    hhv20_prev = REF(df, HHV(df, "close", 20), 1)
    brk = df.close > hhv20_prev
    # 原式用 BARSLAST 表达「回踩后突破」，此处简化为「突破前高且5日内首次」
    return brk & (COUNT(df, brk, 5) == 1)


def low_pos_surge(df):
    """短线之王：58日低位 + 涨停 + 开盘低于5日线"""
    low58 = LLV(df, "close", 58)
    # 原式含 INDEXC>0.98*REF(INDEXC,1)（大盘过滤），无指数数据 → 剔除
    return ((df.close >= 1.099 * REF(df, df.close, 1))
            & (df.open < MA(df, "close", 5))
            & (df.close <= 1.47 * low58))


def black_horse_start(df):
    """黑马起步：100日区间位置平滑线上穿 15/20/25（严格照搬）"""
    low100, high100 = LLV(df, "low", 100), HHV(df, "high", 100)
    raw = (df.close - low100) / (high100 - low100).replace(0, np.nan) * 100
    v5 = EMA_S(df, SMA(df, raw, 8, 1), 3)
    return CROSS(df, v5, 15.0) | CROSS(df, v5, 20.0) | CROSS(df, v5, 25.0)


def qipan(df):
    """起攀选股：27日区间相对位置线上穿加权平滑线 + 涨幅>2%（严格照搬）"""
    v6 = (2 * df.close + df.high + df.low) / 4
    l27, h27 = LLV(df, "low", 27), HHV(df, "high", 27)
    raw = (v6 - l27) / (h27 - l27).replace(0, np.nan) * 100
    climb = EMA_S(df, raw, 13) - 50
    jinshan = EMA_S(df, 0.618 * REF(df, climb, 1) + 0.382 * climb, 3)
    return CROSS(df, climb, jinshan) & (df.close / REF(df, df.close, 1) > 1.02)


def black_horse_cradle(df):
    """黑马摇篮：价格同时突破箱底/13日线/箱顶（严格照搬）"""
    ss1 = (df.low + df.high + df.close * 2) / 4
    ss2 = _gb(df)["_ss1"].transform(lambda x: x.rolling(4).mean()) if False else \
        ss1.groupby(df["code"], sort=False).transform(lambda x: x.rolling(4).mean())
    ss3 = ss2.groupby(df["code"], sort=False).transform(lambda x: x.rolling(10).max())
    ss4 = ss3.groupby(df["code"], sort=False).transform(lambda x: x.rolling(3).mean())
    ss5 = 1.25 * ss4 - 0.25 * ss3
    xkkj = pd.concat([ss5, ss3], axis=1).min(axis=1)
    ff1 = ss2.groupby(df["code"], sort=False).transform(lambda x: x.rolling(10).min())
    ff2 = ff1.groupby(df["code"], sort=False).transform(lambda x: x.rolling(3).mean())
    ff3 = 1.25 * ff2 - 0.25 * ff1
    dkkj = pd.concat([ff3, ff1], axis=1).max(axis=1)
    ma13 = MA(df, "close", 13)
    zdhm = CROSS(df, df.close, dkkj) & CROSS(df, df.close, ma13) & CROSS(df, df.close, xkkj)
    zhm = CROSS(df, df.close, ma13) & CROSS(df, df.close, xkkj)
    return zdhm | zhm


def vol_price_gold(df):
    """金叉选股指标：四重 EMA 快慢线金叉 + 放量收阳"""
    f = EMA(df, "close", 2)
    for _ in range(3):
        f = EMA_S(df, f, 2)
    slow = EMA_S(df, REF(df, f, 1), 2)
    v21 = MA(df, "volume", 21)
    # 原式含 CAPITAL<=300000000，已剔除
    return (CROSS(df, f, slow) & (df.close > df.open)
            & (df.volume >= v21) & (df.volume > REF(df, df.volume, 1)))


def mid_short_wave(df):
    """中短波：EMA13 平滑线金叉"""
    hz = EMA(df, "close", 13)
    short = EMA_S(df, hz, 1)
    hz2 = EMA_S(df, hz, 8)
    return CROSS(df, short, hz2)


def fund_resonance(df):
    """筹码+资金共振（今日头条 2026-04）—— **部分复现**

    原式含 `COST(85)` / `COST(15)` / `COST(50)` / `WINNER(C)`（筹码分布）
    与 `CAPITAL`，本机无此数据 → **剔除全部筹码条件**，只保留资金与量能部分。

    保留的条件：
    - 资金向好：`主力强度 > 资金慢线` 且 `主力强度` 上行
      （`DDX` = 涨幅 × 换手率，`主力强度` = EMA(DDX,13)，`资金慢线` = EMA(强度,34)）
    - 量能健康：`VOL > 前一日` 且 `VOL < 前一日 × 2`（温和放量，排除爆量）
    """
    # VV := VOL/CAPITAL*100（换手率）→ 用「量 / 20 日均量」替代
    vv = df.volume / MA(df, "volume", 20)
    ddx = (df.close / REF(df, df.close, 1) - 1) * vv * 10
    strength = EMA_S(df, ddx, 13)
    slow = EMA_S(df, strength, 34)
    fund_ok = (strength > slow) & (strength > REF(df, strength, 1))
    vol_ok = ((df.volume > REF(df, df.volume, 1))
              & (df.volume < REF(df, df.volume, 1) * 2))
    return fund_ok & vol_ok


# ================================================================ 注册表
STRATEGIES = [
    {"key": "macd_low_gold", "name": "低位金叉", "cat": "超跌反转",
     "fn": macd_low_gold, "src": "知乎合集 #3",
     "note": "DIFF<-0.1 绝对价位已按 close 归一化为 <-0.002"},
    {"key": "macd_second_gold", "name": "二次金叉", "cat": "超跌反转",
     "fn": macd_second_gold, "src": "知乎合集 #4",
     "note": "COUNT(JCCOUNT=2,21) 简化为 21 日内≥2 次金叉"},
    {"key": "ma_squeeze_3", "name": "三线粘合", "cat": "趋势形态",
     "fn": ma_squeeze_3, "src": "知乎合集 #7",
     "note": "剔除 CAPITAL<=3亿 流通盘限制（数据不可得）"},
    {"key": "ma_squeeze_4", "name": "四线粘合", "cat": "趋势形态",
     "fn": ma_squeeze_4, "src": "知乎合集 #30",
     "note": "原式分三组比较 EMA 比值，此处用五线整体收敛<2% 近似"},
    {"key": "xushi_daifa", "name": "蓄势待发", "cat": "趋势形态",
     "fn": xushi_daifa, "src": "知乎合集 #19", "note": "严格照搬"},
    {"key": "multi_ma_angle", "name": "强势股四均线角度", "cat": "趋势形态",
     "fn": multi_ma_angle, "src": "知乎合集 #13", "note": "严格照搬"},
    {"key": "break_ma21", "name": "紫色冲关", "cat": "量价异动",
     "fn": break_ma21, "src": "知乎合集 #15",
     "note": "换手率 VOL/CAPITAL 用 量/20日均量>1.5 替代"},
    {"key": "kd_macd_bull", "name": "横盘是银", "cat": "量价异动",
     "fn": kd_macd_bull, "src": "知乎合集 #23", "note": "严格照搬"},
    {"key": "high20_break", "name": "枪挑小梁王", "cat": "趋势形态",
     "fn": high20_break, "src": "知乎合集 #21",
     "note": "BARSLAST 回踩逻辑简化为「突破前高且5日内首次」"},
    {"key": "low_pos_surge", "name": "短线之王", "cat": "量价异动",
     "fn": low_pos_surge, "src": "知乎合集 #20",
     "note": "剔除大盘 INDEXC 过滤条件"},
    {"key": "black_horse_start", "name": "黑马起步", "cat": "超跌反转",
     "fn": black_horse_start, "src": "知乎合集 #24", "note": "严格照搬"},
    {"key": "qipan", "name": "起攀选股", "cat": "超跌反转",
     "fn": qipan, "src": "知乎合集 #25", "note": "严格照搬"},
    {"key": "black_horse_cradle", "name": "黑马摇篮", "cat": "趋势形态",
     "fn": black_horse_cradle, "src": "知乎合集 #27", "note": "严格照搬"},
    {"key": "vol_price_gold", "name": "金叉选股指标", "cat": "量价异动",
     "fn": vol_price_gold, "src": "知乎合集 #10",
     "note": "剔除 CAPITAL<=3亿 流通盘限制"},
    {"key": "mid_short_wave", "name": "中短波选股", "cat": "趋势形态",
     "fn": mid_short_wave, "src": "知乎合集 #26", "note": "严格照搬"},
    {"key": "fund_resonance", "name": "资金共振", "cat": "量价异动",
     "fn": fund_resonance, "src": "今日头条 2026-04「筹码+资金共振」",
     "note": "**部分复现**：原式含 COST()/WINNER()/CAPITAL 筹码条件，"
             "本机无筹码分布数据 → 全部剔除，只保留资金+量能条件"},
]

BY_KEY = {s["key"]: s for s in STRATEGIES}

# 使用不可复现函数、被排除在回测之外的公式（报告里要列出来）
EXCLUDED = [
    ("绝地反弹(1)(2)", "含未定义变量、源码残缺，且用 BACKSET（未来函数）"),
    ("聚宝盆", "源码末行残缺（REF 缺参数）"),
    ("次日涨停", "用 BARSLAST 回溯前高，且与「枪挑小梁王」重复"),
    ("财务突破选股", "用 FINANCE() 财务数据，本机数据无此字段"),
    ("拉升在即", "用 INDEXA 大盘指数与 CAPITAL，数据不可得"),
    ("HMYZ黑马易找", "用 COST() 成本分布，数据不可得"),
    ("黑马摇篮之小黑马", "20 阶加权 REF 展开，源码在网页转换中残缺"),
    ("黄转紫选股", "源码残缺（紫柱条件未闭合）"),
    ("三线乾坤/3线粘合等主图", "含 BACKSET 未来函数，且属绘图指标非选股"),
    ("黄金坑/资金流向/变色MACD/至尊MACD", "副图绘图指标，非选股；且源码有截断"),
]


def build_signal(df: pd.DataFrame, key: str, hold: int = 5) -> pd.Series:
    """生成持有掩码。

    信号在 T 日收盘触发 → T+1 开盘买入 → 持有 `hold` 个交易日。
    实现：`hold_mask[e] = (信号在 [e-hold, e-1] 内出现过)`，
    这样可直接复用 `backtest_stock.run()` 的「每日选股」逻辑。
    """
    spec = BY_KEY[key]
    sig = spec["fn"](df).fillna(False).astype(float)
    m = sig.groupby(df["code"], sort=False).transform(
        lambda x: x.rolling(hold).max())
    m = m.groupby(df["code"], sort=False).shift(1)
    return (m.fillna(0.0) > 0)
