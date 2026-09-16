"""agentskill 统一配置：路径、交易成本、风控、调度参数。"""
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent

DATA_DIR = PROJECT_ROOT / "data"
INDEX_DIR = DATA_DIR / "indexes"
STOCK_DIR = DATA_DIR / "stocks"
RESULT_DIR = PROJECT_ROOT / "results"
DB_PATH = PROJECT_ROOT / "state" / "trading.db"

RECOMMENDED_POOLS = {
    # 指数池（指数不可直接交易，仅作基准/指标参考，选作交易标的会被 validate_tradeable 拦截）
    "宽基成长": ["000300.SH", "000905.SH", "000852.SH", "000688.SH", "399006.SZ",
                 "399673.SZ", "399005.SZ"],
    "行业轮动": ["399997.SZ", "399989.SZ", "399967.SZ", "399986.SZ",
                 "399808.SZ", "000688.SH", "399673.SZ", "399975.SZ"],
    # 默认池：可交易的 ETF（指数不可直接交易）
    "default": ["510300.SH", "510500.SH", "159915.SZ", "510880.SH"],
    # ===== ETF 池 =====
    "ETF宽基": ["510300.SH", "510500.SH", "510050.SH", "159915.SZ", "159949.SZ",
                "512100.SH"],
    "ETF行业": ["512880.SH", "512690.SH", "159928.SZ", "512800.SH", "512660.SH",
                "512010.SH", "512170.SH", "515030.SH"],
    # 防御型 ETF（黄金+红利，回测验证可显著降回撤）
    "ETF防御": ["518880.SH", "510880.SH", "511010.SH"],
    # 推荐：宽基 + 行业 + 防御 混合，稳健组合
    "ETF稳健": ["510300.SH", "510500.SH", "510050.SH", "159915.SZ", "159949.SZ",
                "510880.SH", "518880.SH", "512880.SH", "512690.SH", "159928.SZ"],
    # 全局 ETF 轮动池（2015 前上市，含跨境/商品/债券等低相关资产，回测跨周期稳健）
    "ETF全球": ["510300.SH", "510500.SH", "510050.SH", "159915.SZ", "159928.SZ",
                "159920.SZ", "513100.SH", "513500.SH", "518880.SH", "510880.SH",
                "511010.SH"],
    # ===== 科技进攻型 ETF 池 =====
    # 聚焦 2026 年科技最强主线：科创板50 / 半导体 / 人工智能 / 机器人 / 通信算力
    # 注：2026-09 社区复测后移除 159819（与 589010 同跟踪中证人工智能主题、高相关冗余，
    #     7 池在 2026 样本外弱于 6 池：样本外 4.21% vs 8.42%，全区间 63.61% vs 56.99%）
    "科技进攻": [
        "588000.SH",   # 科创50ETF — 科创板硬科技宽基
        "588170.SH",   # 科创半导体ETF — 半导体材料设备
        "512480.SH",   # 半导体ETF — 全指半导体
        "589010.SH",   # 人工智能ETF — AI算力/应用
        "562500.SH",   # 机器人ETF — 人形机器人/自动化
        "515880.SH",   # 通信设备ETF — AI算力/光通信/液冷
    ],
}

# ETF 名称映射（供展示/输出用）
ETF_NAMES = {
    "510300.SH": "沪深300ETF", "510500.SH": "中证500ETF", "510050.SH": "上证50ETF",
    "159915.SZ": "创业板ETF", "159928.SZ": "消费ETF", "159920.SZ": "恒生ETF",
    "513100.SH": "纳指ETF", "513500.SH": "标普500ETF", "518880.SH": "黄金ETF",
    "510880.SH": "红利ETF", "511010.SH": "国债ETF",
    # 科技进攻池
    "588000.SH": "科创50ETF", "588170.SH": "科创半导体ETF",
    "512480.SH": "半导体ETF", "589010.SH": "人工智能ETF",
    "562500.SH": "机器人ETF", "515880.SH": "通信设备ETF",
}

TRADING_COST = {
    # 2026-09-13 修正：原为 0.0003（万3），主人实际费率是 **万5**。
    # 只影响回测/研究口径的真实性；实盘走 UiaThsBroker，费率以同花顺为准。
    # 实测影响：ETF 轮动线年化 −0.41pp（全区间 3.18%→2.77%）。
    "commission_rate": 0.0005,
    "min_commission": 5.0,
    "sell_tax_rate": 0.001,
    "slippage_rate": 0.0005,
}

# ✅ 2026-09-16 已改：主人实际资金约 **10 万**，原先误写为 20 万。
#
# 改动安全性（已核对代码，不是猜的）：
#   `scheduler/runner.py::__init__` 先 `PortfolioAccount(init_cash=...)`，
#   紧接着用 `self.db.load_latest_equity()` **覆盖 cash**、用 `load_positions()` 恢复持仓。
#   → **已有 `state/trading.db` 的账户完全不受影响**（现金/持仓从库里恢复，与 INIT_CASH 无关）。
#   → INIT_CASH 只决定两件事：① **新建账户**的起始资金；② 未显式传 `init_cash` 的**回测**口径。
#   ⚠️ 注意：现有模拟盘的历史净值仍是 **20 万量级**（起始 20 万、现值 <金额略>），
#      这是历史事实，**不会被追溯改写**；想让它变成 10 万量级需要 `main.py reset` 重建账户。
#
# 对回测的影响：ETF 轮动线年化 **−0.28pp**（最低佣金惩罚随资金变小而变大）。
# 大多数 `tools/` 脚本本来就显式传 `init_cash=100_000.0`，此次改动只影响漏传的路径。
INIT_CASH = 100_000.0

# 同花顺经典版客户端路径（模拟炒股下单用；需已登录、窗口保持打开不可最小化）
THS_EXE_PATH = r"D:\同花顺软件\同花顺\xiadan.exe"

# Web 访问令牌：设置后 /api/* 敏感接口需请求头 X-Access-Token 匹配（公网分享前务必设置）
# 建议通过环境变量 AGENTSKILL_TOKEN 设置；None = 不鉴权（仅本地开发）
ACCESS_TOKEN = os.environ.get("AGENTSKILL_TOKEN")

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
    "run_time": "14:50",   # 尾盘下单，当日可成交（A股 15:00 收盘）
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

# ---- Faber 趋势过滤（绝对趋势：只保留站上自身 MA(trend_window) 的标的）----
# 实盘当前：**开启**（trend_window=60，2026-09 社区研究落地时开的）。
# 待决项（HANDOFF §三 第 2 项）：修复份额折算数据后实测为**负贡献**
#   （−1.81pp / 夏普 −0.27），但它只有一个 12 年样本，且与根 HANDOFF 记录矛盾
#   → 采取「**并行验证**」：实盘配置不动，同时记录关闭版的**影子权重**，
#     跑 ≥8 周后用**真实前向表现**（不是回测）比较，再决定是否切换。
TREND_LIVE = 60              # 实盘生效值（None = 关闭 Faber）
TREND_SHADOW = None          # 影子对照值（None = 关闭 Faber）
SHADOW_ENABLED = True        # 是否记录影子权重（关掉可省一点算力）
SHADOW_MIN_WEEKS = 8         # 建议的最短并行观察期（周），见 tools/faber_shadow_report.py


def ensure_dirs():
    for d in (DATA_DIR, INDEX_DIR, STOCK_DIR, RESULT_DIR, DB_PATH.parent):
        d.mkdir(parents=True, exist_ok=True)
    return PROJECT_ROOT