"""A股交易日历：拉取、缓存、判断交易日（替换 Scheduler 里粗糙的 weekday 判断）。"""
from datetime import datetime, date

import pandas as pd

import config

_CACHE = config.DATA_DIR / "trade_calendar.csv"


def _download():
    import akshare as ak
    df = ak.tool_trade_date_hist_sina()
    if df is None or df.empty:
        raise RuntimeError("交易日历拉取失败")
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")
    return sorted(df["trade_date"].tolist())


def trading_dates(force=False) -> list:
    """返回升序 A股交易日列表（'YYYYMMDD' 字符串）。缓存到当年年底视为新鲜。"""
    if not force and _CACHE.exists():
        try:
            cached = pd.read_csv(_CACHE, dtype=str)["trade_date"].tolist()
            if cached and max(cached) >= "{}1231".format(date.today().year):
                return cached
        except Exception:
            pass
    dates = _download()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"trade_date": dates}).to_csv(_CACHE, index=False)
    return dates


def _fmt(d):
    return d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d)


def is_trading_day(d) -> bool:
    return _fmt(d) in set(trading_dates())


def latest_closed_trading_day(now=None) -> str:
    """最近一个「已收盘」交易日：严格早于今天（今日未收盘，日线数据不可靠）。

    交易执行基准用这个日期，避免盘中拿到未收盘的实时 bar 当成完整数据。
    """
    today = _fmt(now or datetime.now())
    ds = trading_dates()
    prev = [d for d in ds if d < today]
    return prev[-1] if prev else today


def next_trading_day(d) -> str:
    """严格晚于 d 的下一个交易日。"""
    s = _fmt(d)
    ds = trading_dates()
    nxt = [x for x in ds if x > s]
    return nxt[0] if nxt else s