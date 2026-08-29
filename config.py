"""agentskill 统一配置：路径、交易成本、风控、调度参数。"""
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent

DATA_DIR = PROJECT_ROOT / "data"
INDEX_DIR = DATA_DIR / "indexes"
STOCK_DIR = DATA_DIR / "stocks"
RESULT_DIR = PROJECT_ROOT / "results"
DB_PATH = PROJECT_ROOT / "state" / "trading.db"

RECOMMENDED_POOLS = {
    "宽基成长": ["000300.SH", "000905.SH", "000852.SH", "000688.SH", "399006.SZ",
                 "399673.SZ", "399005.SZ"],
    "行业轮动": ["399997.SZ", "399989.SZ", "399967.SZ", "399986.SZ",
                 "399808.SZ", "000688.SH", "399673.SZ", "399975.SZ"],
    "default": ["000300.SH", "000905.SH", "399006.SZ", "399324.SZ"],
}

TRADING_COST = {
    "commission_rate": 0.0003,
    "min_commission": 5.0,
    "sell_tax_rate": 0.001,
    "slippage_rate": 0.0005,
}

INIT_CASH = 1_000_000.0

# ---- 年化交易日数（A股约 244，美股约 252）----
TRADING_DAYS_PER_YEAR = 244

RISK = {
    "max_position_weight": 0.30,
    "max_total_weight": 0.95,
    "stop_loss": -0.08,
    "take_profit": 0.30,
    "trailing_stop": 0.15,
}

SCHEDULE = {
    "run_time": "15:05",
    "timezone": "Asia/Shanghai",
}

DEFAULT_STRATEGY = "momentum"
DEFAULT_START = "20190101"
DEFAULT_END = "20251231"
DEFAULT_TOP_K = 5
DEFAULT_REBALANCE = 5

# ---- 默认回撤控制方案（回撤熔断 + 波动率目标为默认甜点组合）----
DEFAULT_TIMING = None        # 大盘择时：None / "ma20" / "abs_mom" / "rsrs"
DEFAULT_DD_CIRCUIT = True    # 回撤熔断：默认开启（深阈值+滞回）
DEFAULT_VOL_TARGET = 0.15    # 波动率目标仓位：默认 15% 年化波动


def ensure_dirs():
    for d in (DATA_DIR, INDEX_DIR, STOCK_DIR, RESULT_DIR, DB_PATH.parent):
        d.mkdir(parents=True, exist_ok=True)
    return PROJECT_ROOT