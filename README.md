# agentskill — 自动化量化交易模型

一个完整、模块化、开箱即用的自动化量化交易模型（A 股），覆盖从数据获取到信号生成、风控、下单撮合、持久化、定时调度的全链路。

## 一、整体架构

```
                    ┌──────────────────────────────────────────────┐
                    │                  Scheduler（调度层）           │
                    │        每个交易日 15:05 自动触发 run_once     │
                    └───────────────────────┬──────────────────────┘
                                            ▼
   ┌────────────┐   ┌────────────┐   ┌────────────┐   ┌────────────┐
   │ dataprovider│ → │  factors   │ → │ strategies │ → │    risk    │
   │  数据层     │   │  因子层    │   │  策略层    │   │  风控层    │
   └────────────┘   └────────────┘   └────────────┘   └─────┬──────┘
                                             ▲                ▼
                                  ┌──────────┴───────┐  ┌────────────┐
                                  │ trader（执行层）  │←─│  account   │
                                  │ broker + execution│  │  账户/持仓  │
                                  └───────┬──────────┘  └─────┬──────┘
                                          ▼                   ▼
                                  ┌──────────────────────────────────┐
                                  │      storage（SQLite 持久化）      │
                                  │   orders / equity / positions     │
                                  └──────────────────────────────────┘
```

## 二、从规划到执行：每一步

### 第 1 步 — 数据层 `dataprovider/`
- `store.py`：`DataStore` 负责行情下载（akshare）、本地 CSV 缓存、读取；`classify_code` 区分指数/个股，`fetch_index_cons_sample` 从指数成分股抽样生成个股池。
- `panel.py`：`build_panel` 把多标的行情对齐成横截面面板（date × code）。

### 第 2 步 — 因子层 `factors/`
- `registry.py`：内置技术因子（RSI、MACD、乖离率、均线差、动量、量比、涨停、连板）。
- `engine.py`：`compute_factors` 在面板上逐标的计算，返回 `{factor: DataFrame}`。

### 第 3 步 — 策略层 `strategies/`
- `base.py`：`Strategy` 抽象基类，约定 `generate_weights(date, factors, panel) -> {code: weight}`。
- `builtin.py`：动量、均值回归、双均线、多因子、连板龙头五种策略。
- `registry.py`：策略注册表，按名称创建。

### 第 4 步 — 风控层 `risk/`
- `manager.py`：`RiskManager.filter_weights` 对策略目标权重做二次修正——单标的上限、总仓位上限、个股止损/止盈/回撤止盈。

### 第 5 步 — 执行层 `trader/`
- `broker.py`：`Broker` 抽象基类 + `PaperBroker` 模拟撮合（滑点/佣金/印花税）。未来接 `easytrader` 只需实现 `LiveBroker` 子类。
- `execution.py`：`ExecutionEngine.rebalance` 把「目标权重 vs 当前持仓」的差转成买卖订单（A 股 100 股整手），提交 broker 并回写账户。

### 第 6 步 — 账户层 `account/`
- `portfolio.py`：`PortfolioAccount` 管理现金、持仓（数量/成本/峰值），`apply_fill` 按成交结果更新。

### 第 7 步 — 持久化层 `storage/`
- `db.py`：`TradeDB`（SQLite）保存订单、每日净值、持仓，支持模拟盘状态恢复。

### 第 8 步 — 调度层 `scheduler/`
- `runner.py`：`DailyRunner.run_once` 是单次交易日闭环；`Scheduler.run_forever` 在每日 15:05 自动触发。

### 第 9 步 — 回测引擎 `backtest/`
- `account.py`：权重式组合账户（手续费/滑点/印花税）。
- `engine.py`：`BacktestEngine` 逐日推进，支持大盘择时（ma20/abs_mom/rsrs）。

### 第 10 步 — 绩效分析 `analysis/`
- `metrics.py`：年化收益、夏普、索提诺、卡玛、最大回撤、波动率、胜率、超额收益等。

### 第 11 步 — 入口 `main.py`
- 四个子命令：`backtest` / `run` / `daemon` / `status`。

## 三、快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 历史回测（指数池）
python main.py backtest --strategy momentum --codes 000300.SH,000905.SH,399006.SZ --start 20190101 --end 20211231 --topk 3 --rebalance 5

# 历史回测（个股动量池 + 大盘择时）
python main.py backtest --strategy momentum --pool 个股动量 --start 20190101 --end 20251231 --timing ma20

# 历史回测（回撤熔断 + 波动率目标仓位，显著降低回撤）
python main.py backtest --strategy momentum --codes 000300.SH,000905.SH,399006.SZ --dd-circuit --vol-target 0.15

# 模拟盘单次运行（个股池，真实下单撮合）
python main.py run --strategy momentum --pool 个股动量 --topk 3 --rebalance 5

# 每日定时自动运行（常驻进程，交易日 14:50 尾盘触发）
python main.py daemon --strategy momentum --pool 个股动量

# 查看持仓与订单
python main.py status

# 测试同花顺连接并读取模拟盘资金/持仓
python main.py ths-check --ths-exe "D:\同花顺软件\同花顺\xiadan.exe"

# Web Dashboard（交互式回测 + 持仓查看）
python server.py
# 打开 http://127.0.0.1:8000/
```

## 四、回撤控制（学习自 GitHub 热门项目 / 聚宽 / 幻方量化）

回撤控制是量化交易的"生命线"。参考业界主流做法，agentskill 内置了三层风控：

| 层级 | 机制 | 默认 | 来源 | 效果 |
|---|---|---|---|---|
| 大盘择时 | MA20 / 绝对动量 / RSRS / **BIAS温度计** | 关 | 聚宽社区、AQR、聚宽"坦克300" | 熊市降仓防御 |
| **回撤熔断** | 组合回撤≥15%启动，分档降仓（15%→65%、20%→40%、25%清仓）+ 滞回 | **开** | GitHub `vzeman/trading-autoresearch` | 实测最大回撤 -45%→-25%，代价收益比~1.0 |
| **波动率目标仓位** | 组合实际波动超目标时等比降仓 | **开(15%)** | AI-Capital、幻方/九坤风控体系 | 稳定组合波动 |
| **风险平价加权** | TopK 标的内按波动率倒数分配权重，替代等权 | 关 | BigQuant 行业轮动优化版 | 年化+5.6pp、回撤-3.8pp |

**默认方案**：`回撤熔断` + `波动率目标15%` 已默认开启（`DEFAULT_DD_CIRCUIT=True`、`DEFAULT_VOL_TARGET=0.15`）。回测时不传任何风控参数即可享受甜点组合保护；如需更高收益可用 `--no-dd-circuit --vol-target 0` 关闭全部风控。

**学习自 BigQuant / JoinQuant 的低回撤组合（2024 实测最优）**：

```bash
# 最优甜点：RSRS 择时 + 风险平价加权（个股动量池 2019-2025）
python main.py backtest --strategy momentum --pool 个股动量 --no-dd-circuit --vol-target 0 --timing rsrs --risk-parity
```

回测结果：**年化 24.47%、最大回撤 -17.44%、夏普 1.26、索提诺 1.78、卡玛 1.40**，显著优于无风控基线（25.28%/-25.17%/0.92）。

- **BIAS 温度计择时**（聚宽"坦克300"）：大盘 20 日乖离率多档仓位——`bias≤-8%`空仓、`-8%~-5%`三成仓、`-5%~0`半仓、`>0`满仓，比 MA20 更敏感。
- **风险平价加权**（BigQuant）：高波动标的给更小仓位，从源头压波动，实测收益和回撤同时改善。

**熔断逻辑（带滞回的极端保险）**：
- 进入熔断：组合回撤 ≥15% 才启动，按深度分档（15%→65%仓、20%→40%仓、25%→清仓）；
- 退出熔断：回撤修复到 ≤10% 才恢复满仓（滞回带 5%，避免反复震荡）；
- 设计理念：浅阈值（如 5%）对 A股波动太敏感，会反复"打脸"，且常常砍在 V 型反弹底部，导致**收益损失远大于回撤改善**。深阈值 + 滞回后，代价收益比从 1.76 降到约 1.0。

**核心思想借鉴**：
- 期货/券商的"海龟法则"现代版：波动率头寸管理（volatility position sizing），波动越大仓位越小。
- 私募中性策略（幻方/九坤/明汯）回撤<5% 的关键：满仓分散 + 严格止损线 + 风格中性。
- 聚宽社区经典：沪深300 均线趋势过滤（均线向上满仓、向下半仓）+ "1月/4月不操作"季节效应。

**注意**：回撤熔断/波动率目标用的是**组合净值历史**（`_nav_history`），基于"先验回撤"决定当前仓位，会牺牲部分收益换取更平滑的净值曲线，需根据风险偏好权衡。

## 五、推荐标的池（`config.py`）

| 池名 | 说明 |
|---|---|
| 宽基成长 | 沪深300/中证500/中证1000/科创50/创业板等指数 |
| 行业轮动 | 白酒/医疗/军工/银行/新能源等行业指数 |
| default | 温和宽基组合 |
| 个股动量 | 沪深300 成分股抽样 20 只（动态生成） |
| ETF宽基 | 沪深300/中证500/上证50/创业板/创业板50/中证1000 ETF |
| ETF行业 | 券商/酒/消费/银行/军工/医药/医疗/新能源 ETF |
| ETF防御 | 黄金/红利/国债 ETF（低相关避险资产） |
| ETF稳健 | 宽基+行业+防御 混合，稳健组合 |

> **ETF 数据说明**：ETF 代码走新浪接口（`fund_etf_hist_sina`）。回测验证：在随机 ETF 组合中**强制加入黄金（518880）+红利（510880）ETF**，可把回撤均值从 -45.79% 降到 -28.65%、年化>0 占比从 52% 提到 98%，系统性降回撤效果显著。

## 六、接入实盘 / 同花顺模拟炒股

> 完整配置步骤见 [docs/同花顺经典版接入指南.md](docs/同花顺经典版接入指南.md)，含同花顺客户端设置、依赖安装、常见问题排查。

### 方式一：同花顺经典版（含模拟炒股，无需真实券商账户）

`trader/broker.py` 中的 `ThsBroker` 基于开源库 `easytrader`，通过操控同花顺经典版委托程序 `xiadan.exe` 下单。本机已装经典版：`D:\同花顺软件\同花顺\xiadan.exe`。

**安装依赖**（沙盒内会被拦截，请在系统 PowerShell 手动执行）：
```bash
pip install easytrader
```

**运行（单次模拟盘下单到同花顺）**：
```bash
python main.py run --strategy momentum --pool 个股动量 --ths --ths-exe "D:\同花顺软件\同花顺\xiadan.exe"
```

**每日自动运行（常驻，接入同花顺）**：
```bash
python main.py daemon --strategy momentum --pool 个股动量 --ths --ths-exe "D:\同花顺软件\同花顺\xiadan.exe"
```

> 关键前提：同花顺必须用**经典版**（远航版/极速版不兼容）；客户端要登录到「模拟炒股」交易窗口、不能最小化、超时时间设 0、默认买卖价格清空。

### 方式二：自定义券商（实盘）

`Broker` 是抽象基类，接入任意券商只需实现 `submit(order)` 方法：

```python
from trader.broker import Broker

class MyBroker(Broker):
    def submit(self, order):
        # 调用你的券商 API，这里示例
        order.status = "filled"
        order.filled_price = order.price
        order.filled_qty = order.qty
        return order
```

然后在 `DailyRunner(..., broker=MyBroker())` 中传入即可。

## 七、项目结构

```
agentskill/
├── config.py            # 全局配置
├── pipeline.py          # 回测流水线
├── main.py              # CLI 入口
├── requirements.txt
├── dataprovider/        # 数据层
├── factors/             # 因子层
├── strategies/          # 策略层
├── risk/                # 风控层
├── trader/              # 执行层（broker + execution）
├── account/             # 账户层
├── storage/             # 持久化层（SQLite）
├── scheduler/           # 调度层
├── backtest/            # 回测引擎
├── analysis/            # 绩效分析
├── data/                # 行情缓存（indexes/ stocks/）
├── results/             # 结果输出
└── state/               # 交易状态库 trading.db
```

## 八、注意事项

- 回测与模拟盘均计入手续费（万3）、滑点（0.05%）、卖出印花税（0.1%）。
- 指数不可直接交易，模拟盘请使用 `个股动量` 池或指定个股代码。
- `index_stock_cons_csindex` 依赖 akshare 的中证官网接口，网络波动时可能失败，可稍后重试。
- 本模型为研究与教学用途，不构成投资建议。