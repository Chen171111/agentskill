"""数据层：指数/个股行情下载、缓存与读取。统一走 akshare。"""
from pathlib import Path

import pandas as pd

import config

# 指数代码 -> akshare symbol
_AK_SYMBOL_MAP = {
    "000300.SH": "sh000300", "000905.SH": "sh000905", "000852.SH": "sh000852",
    "000016.SH": "sh000016", "000688.SH": "sh000688", "000922.SH": "sh000922",
    "399006.SZ": "sz399006", "399324.SZ": "sz399324", "399997.SZ": "sz399997",
    "399989.SZ": "sz399989", "399967.SZ": "sz399967", "399986.SZ": "sz399986",
    "399808.SZ": "sz399808", "399673.SZ": "sz399673", "399005.SZ": "sz399005",
    "399975.SZ": "sz399975",
}

_OCCL = {"vol": "volume", "日期": "date", "开盘": "open", "最高": "high",
         "最低": "low", "收盘": "close", "成交量": "volume"}


def classify_code(code: str) -> str:
    """返回 'index' / 'etf' / 'stock'。"""
    code_up = code.upper()
    if code_up in _AK_SYMBOL_MAP:
        return "index"
    parts = code_up.split(".")
    base, market = parts[0], (parts[1] if len(parts) > 1 else None)
    # ETF：沪市 51/56/58 开头，深市 15/16 开头
    if market == "SH" and base.startswith(("51", "56", "58", "59")):
        return "etf"
    if market == "SZ" and base.startswith(("15", "16")):
        return "etf"
    if market == "SH":
        return "index" if base.startswith(("000", "899")) else "stock"
    if market == "SZ":
        return "index" if base.startswith("399") else "stock"
    return "stock" if base.isdigit() and len(base) == 6 else "index"


def to_ak_symbol(code: str) -> str:
    """代码 -> akshare symbol。"""
    code_up = code.upper()
    if code_up in _AK_SYMBOL_MAP:
        return _AK_SYMBOL_MAP[code_up]
    parts = code_up.split(".")
    base, market = parts[0], (parts[1] if len(parts) > 1 else None)
    if market == "SH":
        pfx = "sh"
    elif market == "SZ":
        pfx = "sz"
    else:
        pfx = "sh" if base.startswith(("6", "5", "9", "11", "13", "688", "000")) else "sz"
    return "{}{}".format(pfx, base)


def _z6(c) -> str:
    return str(c).zfill(6)


def fetch_index_cons_sample(index_code: str = "000300", n: int = 20) -> list:
    """拉取指数成分股并等距抽样生成个股池（用于横截面个股动量等策略）。"""
    import akshare as ak
    symbol = index_code.split(".")[0]
    cons = ak.index_stock_cons_csindex(symbol=symbol)
    raw = list(cons["成分券代码"])
    step = max(1, len(raw) // max(n, 1))
    sel = raw[::step][:max(n, 1)]
    out = []
    for c in sel:
        c6 = _z6(c)
        suffix = ".SH" if c6.startswith(("6", "9", "68")) else ".SZ"
        out.append(c6 + suffix)
    return out


def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns=_OCCL)
    if "date" not in df.columns:
        raise ValueError("数据缺少 date 列: {}".format(list(df.columns)))
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d")
    df = df[[c for c in ["date", "open", "high", "low", "close", "volume"]
             if c in df.columns]]
    df.sort_values("date", inplace=True)
    return df.reset_index(drop=True)


def _download_one(ak, code: str, kind: str) -> pd.DataFrame:
    if kind == "index":
        df = ak.stock_zh_index_daily(symbol=to_ak_symbol(code))
        if df is None or df.empty:
            raise RuntimeError("{} 无数据返回".format(code))
        return _normalize_ohlcv(df)

    if kind == "etf":
        # ETF 走新浪接口（东财 fund_etf_hist_em 易被限流）
        df = ak.fund_etf_hist_sina(symbol=to_ak_symbol(code))
        if df is None or df.empty:
            raise RuntimeError("{} 无数据返回".format(code))
        return _normalize_ohlcv(df)

    base = code.split(".")[0]
    for adj in ("qfq", ""):
        try:
            df = ak.stock_zh_a_hist(symbol=base, period="daily",
                                    start_date="19900101", end_date="22240101",
                                    adjust=adj)
            if df is not None and not df.empty:
                return _normalize_ohlcv(df)
        except Exception:
            continue
    try:
        df = ak.stock_zh_a_daily(symbol=to_ak_symbol(code),
                                 start_date="19900101", end_date="22240101")
        if df is not None and not df.empty:
            return _normalize_ohlcv(df)
    except Exception as e:
        raise RuntimeError("{} 数据下载失败: {}".format(code, e)) from e
    raise RuntimeError("{} 无数据".format(code))


class DataStore:
    """行情数据仓库（下载 + 缓存 + 读取）。"""

    def __init__(self, force_download=False):
        self.index_dir = Path(config.INDEX_DIR)
        self.stock_dir = Path(config.STOCK_DIR)
        self.force_download = force_download
        self._cache = {}

    def dir_for(self, kind: str) -> Path:
        return self.index_dir if kind == "index" else self.stock_dir

    def path_for(self, code: str) -> Path:
        return self.dir_for(classify_code(code)) / (code + ".csv")

    def ensure(self, codes):
        import akshare as ak
        added = []
        for code in codes:
            self.dir_for(classify_code(code)).mkdir(parents=True, exist_ok=True)
            p = self.path_for(code)
            if p.exists() and not self.force_download:
                continue
            try:
                df = _download_one(ak, code, classify_code(code))
                df.to_csv(p, index=False)
                added.append(code)
            except Exception as e:
                print("[DataStore] 下载失败 {}: {}".format(code, e))
        return added

    def _last_date(self, p: Path):
        try:
            df = pd.read_csv(p, dtype={"date": str})
            if df.empty:
                return None
            return str(df["date"].max())
        except Exception:
            return None

    def refresh(self, codes, now=None, force=False):
        """强制刷新缓存到「最近已收盘交易日」（含交易日历）。

        - 每个标的数据陈旧（最新日期 < 已收盘基准）时重新下载覆盖；
        - 下载后裁剪掉晚于已收盘基准的行，避免盘中未收盘的实时 bar 混入；
        - 个股/指数/ETF 统一走全量覆盖，保证复权口径一致（后续可优化为个股增量）。
        """
        import akshare as ak
        from .calendar import latest_closed_trading_day
        target = latest_closed_trading_day(now)
        refreshed = []
        for code in codes:
            kind = classify_code(code)
            self.dir_for(kind).mkdir(parents=True, exist_ok=True)
            p = self.path_for(code)
            last = self._last_date(p) if p.exists() else None
            if not force and last is not None and last >= target:
                continue  # 已新鲜，跳过
            try:
                df = _download_one(ak, code, kind)
                if df is None or df.empty:
                    continue
                df = df[df["date"] <= target]
                if df.empty:
                    continue
                df.to_csv(p, index=False)
                self._cache.pop(code, None)
                refreshed.append(code)
            except Exception as e:
                print("[refresh] {} 刷新失败: {}".format(code, e))
        return refreshed

    def read(self, code: str, start=None, end=None):
        if code in self._cache:
            df = self._cache[code].copy()
        else:
            p = self.path_for(code)
            if not p.exists():
                self.ensure([code])
            if not p.exists():
                raise FileNotFoundError(p)
            df = pd.read_csv(p)
            df["date"] = df["date"].astype(str)
            df.index = df["date"]
            df.sort_index(inplace=True)
            df["rate"] = df["close"].pct_change()
            self._cache[code] = df.copy()
        if start:
            df = df[df.index >= str(start)]
        if end:
            df = df[df.index <= str(end)]
        return df