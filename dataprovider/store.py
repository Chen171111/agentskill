"""数据层：指数/个股行情下载、缓存与读取。统一走 akshare。"""
from pathlib import Path

import pandas as pd

import config

from .adjust import repair_frame          # 份额折算自动复权（读取时生效）

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


def validate_tradeable(codes):
    """校验交易标的：指数不可直接买卖，含指数时抛出 ValueError。

    指数（如 000300.SH）只能当基准（--benchmark），真正能持仓的是 ETF/个股。
    """
    bad = [c for c in codes if classify_code(c) == "index"]
    if bad:
        raise ValueError(
            "指数不可直接交易：{}。请把指数放到 --benchmark 作基准，"
            "交易标的改用 ETF（如 510300.SH=沪深300ETF）或个股。".format("、".join(bad)))
    return list(codes)


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

    def __init__(self, force_download=False, repair_splits=True,
                 split_criterion="limit"):
        self.index_dir = Path(config.INDEX_DIR)
        self.stock_dir = Path(config.STOCK_DIR)
        self.force_download = force_download
        # 份额折算自动复权（默认开）。设为 False 可读**原始未复权**数据（诊断/对照用）。
        self.repair_splits = repair_splits
        # 复权判据口径（2026-10-16 老大决策：切换为按品种涨跌停）：
        #   "limit"（默认）= 按品种涨跌停（|ret| > limit + tick/前收 + 0.1pp），
        #     修复 512690 2021-12-31 −27.06% / 510500 2022-08-29 −12.74%
        #     两处 0.35 漏检的真实折算（后者在权威 ETF全球池内）。
        #     决策依据：tools/diag_split_limit_criterion.py（零误报零损失）+
        #     tools/cmp_split_criterion_impact.py（权威回测 26.84%→28.28%，+1.43pp）。
        #   "thresh" = 扁平 0.35 旧口径，保留作对照（A/B 脚本显式传它）。
        # ⚠️ 切到 limit 后四池历史数字已变（README §三已同步重跑更新）。
        if split_criterion not in ("thresh", "limit"):
            raise ValueError(
                "split_criterion 仅支持 'thresh'（扁平0.35，对照用）/ 'limit'（按品种涨跌停，默认），"
                "实得 {!r}".format(split_criterion))
        self.split_criterion = split_criterion
        self.split_log = Path(config.DATA_DIR).parent / "state" / "split_repairs.log"
        self._cache = {}

    def dir_for(self, kind: str) -> Path:
        return self.index_dir if kind == "index" else self.stock_dir

    def path_for(self, code: str) -> Path:
        return self.dir_for(classify_code(code)) / (code + ".csv")

    def ensure(self, codes):
        # 先确定需要下载的标的后才 import akshare（数据齐全时零依赖，32 位可无 akshare 运行）
        need = []
        for code in codes:
            self.dir_for(classify_code(code)).mkdir(parents=True, exist_ok=True)
            p = self.path_for(code)
            if not (p.exists() and not self.force_download):
                need.append(code)
        if not need:
            return []
        try:
            import akshare as ak
        except ImportError as e:
            print("[ensure] 需下载 {} 但缺少 akshare({})，请用 64 位 E:\\Python 先准备数据".format(
                ", ".join(need), e))
            return []
        added = []
        for code in need:
            try:
                df = _download_one(ak, code, classify_code(code))
                df.to_csv(self.path_for(code), index=False)
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
        - 仅当确有陈旧标的需要下载时才 import akshare（数据全新鲜时零依赖，32 位 Python 可无 akshare 直接运行）。
        """
        from .calendar import latest_closed_trading_day
        target = latest_closed_trading_day(now)
        stale = []
        for code in codes:
            kind = classify_code(code)
            self.dir_for(kind).mkdir(parents=True, exist_ok=True)
            p = self.path_for(code)
            last = self._last_date(p) if p.exists() else None
            if not force and last is not None and last >= target:
                continue  # 已新鲜，跳过
            stale.append(code)
        if not stale:
            return []

        # 只有当确实需要下载时才引入 akshare（避免 32 位环境因无 akshare 而失败）
        try:
            import akshare as ak
        except ImportError as e:
            print("[refresh] 需下载 {} 但缺少 akshare({})，请用 64 位 E:\\Python 先刷新数据".format(
                ", ".join(stale), e))
            return []

        refreshed = []
        for code in stale:
            kind = classify_code(code)
            p = self.path_for(code)
            try:
                df = _download_one(ak, code, kind)
                if df is None or df.empty:
                    continue
                df = df[df["date"] <= target]
                if df.empty:
                    continue
                df.to_csv(p, index=False)
                # 缓存键为 (code, end) 元组（2026-10-15 PIT 修复后）——
                # 必须清掉该 code 的**所有**截止档位，否则刷新后仍读到旧缓存
                for _k in [k for k in self._cache if k[0] == code]:
                    self._cache.pop(_k, None)
                refreshed.append(code)
            except Exception as e:
                print("[refresh] {} 刷新失败: {}".format(code, e))
        return refreshed

    def read(self, code: str, start=None, end=None):
        # ⚠️ PIT 修复（2026-10-15，历史选股前视缺陷）：
        # 原顺序是「读全量 CSV → 全量复权 → 算 rate → 最后才按 end 截断」。
        # 份额折算复权（adjust.repair_splits）会把**跳变日之前**的整段价格
        # 乘上跳变比率 —— 若该跳变（折算）发生在回测截止日 **end 之后**，
        # 等于用截止日之后才发生的事件修改截止日之前的历史价格：
        # 未来信息直接改变历史选股（实测 ETF全球池、截止 2025-12-31：
        # 仅用截止日已知数据前三名含 512480.SH，全量路径第三名变成
        # 588170.SH）。MACD 因子按绝对价差计算，对该缩放敏感。
        # 修法：先按 end 截断、再复权、再算 rate；缓存键带 end，
        # 不同截止各复权一次，互不串味。
        key = (code, str(end) if end else None)
        if key in self._cache:
            df = self._cache[key].copy()
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
            if end:
                df = df[df.index <= str(end)]
            # ⚠️ 份额折算自动复权 —— **必须在算 rate 之前**，且只允许使用
            # 截止日已知的数据（截断已在上面完成）。
            # 为什么放在读取时而不是下载后：`refresh()` 会用 akshare 的**原始未复权**
            # 全量历史覆盖 CSV，一次性打补丁会被下次刷新静默回退
            # （2026-09-16 真实事故：09-13 修好的 4 处，09-16 刷新后全部复发，
            #   回测年化 3.47% → 2.35%，差 1.12pp/年）。
            # 放在这里 → 无论文件被覆盖多少次，策略读到的永远连续；且**幂等**。
            if self.repair_splits:
                df, n_fix, _ = repair_frame(
                    df, code, log_path=self.split_log,
                    limit_aware=(self.split_criterion == "limit"))
                if n_fix:
                    print("[DataStore] {} 份额折算自动复权 {} 处"
                          .format(code, n_fix), flush=True)
            df["rate"] = df["close"].pct_change()
            self._cache[key] = df.copy()
        if start:
            df = df[df.index >= str(start)]
        return df