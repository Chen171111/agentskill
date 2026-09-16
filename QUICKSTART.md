# 快速开始

目标：**从 clone 到跑出第一个回测结果，大约 1 分钟。**

下面每一步都实测过（2026-09-17，Windows / Python 3.13 与 3.14）。
实测：**在完全没有数据的情况下，一条命令约 10 秒出结果**（含自动下载）。

---

## 第 0 步：环境要求

| 项 | 要求 |
|---|---|
| 操作系统 | **Windows / macOS / Linux 均可**（回测部分跨平台；实盘部分仅 Windows，见文末） |
| Python | **3.10 或更高**（本项目实测于 3.13 / 3.14；更低版本未验证） |
| 网络 | **需要联网** —— 行情数据在首次运行时自动下载 |

> `requirements.txt` 里没有锁版本上限，理论上更新的版本也能用。
> 如果遇到奇怪的语法错误，优先升级到 Python 3.12+。

## 第 1 步：拿到代码

```bash
git clone https://github.com/Chen171111/agentskill.git
cd agentskill
```

> 仓库里**不含行情数据**（`data/` 在 `.gitignore` 里），这是故意的——
> 数据由你在第 3 步自动拉取，保证是你自己的最新数据。

## 第 2 步：装依赖

强烈建议用虚拟环境：

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

`requirements.txt` 内容：

```
akshare>=1.14.0     # 行情数据源
pandas>=1.5.0
numpy>=1.23.0
fastapi>=0.100.0    # Web Dashboard（可选）
uvicorn>=0.23.0
```

> **`akshare` 是必需品**，不是可选项。没有它，第 3 步会在下载数据时失败。
> 想用 Web Dashboard 就装 fastapi/uvicorn，只跑回测可以不装。

## 第 3 步：跑第一个回测

```bash
python main.py backtest --strategy etf_rotation --pool ETF全球 --topk 5 --rebalance 5 --start 20190101 --end 20260911
```

**首次运行会自动下载** 11 只 ETF 的行情到 `data/`，你会先看到这样的日志：

```
[ensure] 需下载 510300.SH, 510500.SH, 510050.SH, ... 请稍候
[DataStore] 510500.SH 份额折算自动复权 1 处
[DataStore] 159928.SZ 份额折算自动复权 1 处
[DataStore] 513100.SH 份额折算自动复权 1 处
[DataStore] 513500.SH 份额折算自动复权 1 处
```

> 那 4 行 `份额折算自动复权` **不是错误**，是正常输出——ETF 份额折算会让价格跳变，
> 引擎在读取时自动做了前复权，让收益序列连续。
> 详见 [docs/ETF线_复权阈值缺陷.md](docs/ETF线_复权阈值缺陷.md)。

## 你会看到什么

```
正在回测 etf_rotation / 510300.SH,510500.SH,... / 20190101 ~ 20260911

===== 绩效指标 =====
累计收益      :    28.39%
年化收益      :     3.33%
最大回撤      :    -9.89%
夏普比率      :     0.47
索提诺比率     :     0.53
卡玛比率      :     0.34
年化波动      :     7.59%
胜率        :    52.52%
基准累计收益    :    51.77%
基准年化收益    :     5.61%
基准最大回撤    :   -45.10%
超额收益      :   -23.38%

区间: 20190102 ~ 20260911
```

**怎么读这些数字：**

- **基准默认是池内第一只标的**（`ETF全球` → `510300.SH` 沪深300ETF），可用 `--benchmark` 换。
- ⚠️ **超额收益是 −23.38%，即这条线跑输基准。** 这不是 bug，是真实结论。
  它被保留是因为**回撤小一个量级**（−9.89% vs −45.10%）——它是低回撤配置，不是高收益策略。
- 想知道收益是从哪来的，跑归因：见 [docs/ETF线_收益归因.md](docs/ETF线_收益归因.md)。

## 数据下载说明（第一次跑最可能出问题的地方）

| 问题 | 说明 |
|---|---|
| **下载到哪** | ETF / 指数行情 → `data/stocks/`、`data/indexes/`。整个 `data/` 不入 git |
| **要多久** | 实测 11 只 ETF 约 **10 秒**（新浪接口）。个股池会慢一些 |
| **会重复下载吗** | 不会。文件存在就跳过。想强制刷新就**删掉 `data/` 里对应的 CSV**，或用 `python tools/refresh_data.py`（CLI 没有暴露强制刷新开关） |
| **接口挂了怎么办** | akshare 依赖第三方公开接口，会波动。重跑一次通常就好 |
| **只想跑离线** | 只要 `data/` 里有对应 CSV 就行，可以整个目录拷来拷去 |

## 排错

### `ModuleNotFoundError: No module named 'akshare'`

依赖没装到**你正在用的那个 Python** 上。常见原因：装了多个 Python / 忘了激活虚拟环境。

```bash
python -c "import akshare; print(akshare.__version__)"   # 先确认
```

### `FileNotFoundError: data/stocks/510300.SH.csv`

数据没下下来。往上翻日志，找这一行：

```
[ensure] 需下载 510300.SH 但缺少 akshare(...)，请用 64 位 E:\Python 先准备数据
```

看到它就说明是 **akshare 不可用**（而不是网络问题）。按上一条解决。

### Windows 控制台中文乱码

```bash
set PYTHONIOENCODING=utf-8
```

### 个股池报中证指数接口错误

`--pool 个股动量` 会调 `index_stock_cons_csindex` 拉中证官网的成分股，**这个接口网络波动时容易失败**。
稍后重试即可。ETF 池不受影响。

## 下一步

| 想做什么 | 怎么做 |
|---|---|
| 换个池 / 换策略 | `--pool ETF稳健`、`--strategy momentum`（策略清单见 [README](README.md#五策略与标的池)） |
| 看有哪些标的池 | `python -c "import config; print(list(config.RECOMMENDED_POOLS))"` |
| 开风控 | `--dd-circuit`（默认开）、`--vol-target 0.15`、`--risk-parity`、`--timing rsrs` |
| 看 Web 界面 | `pip install fastapi uvicorn` → `python server.py` → 打开 <http://127.0.0.1:8000/> |
| 看完整参数 | `python main.py backtest --help` |
| 看研究结论 | [docs/README.md](docs/README.md) |

## 关于实盘 / 模拟盘

`main.py` 还有 `run` / `simulate` / `daemon` / `status` / `reset` / `ths-check` 六个子命令，
走的是**同花顺经典版客户端自动化**那条路：

> ⚠️ **这部分绑死了项目作者的本机环境**（Windows + 同花顺经典版 + 个人模拟账户 + 硬编码路径），
> **别人拿不走**。想接自己的模拟盘，见 [docs/同花顺经典版接入指南.md](docs/同花顺经典版接入指南.md)；
> 想接自己的券商，实现 `trader/broker.py` 的 `Broker.submit(order)` 即可。

---

**免责声明**：本项目仅供研究与学习，不构成投资建议。回测是历史模拟结果，不预示未来收益。
详见 [README 的免责声明](README.md#免责声明)。
