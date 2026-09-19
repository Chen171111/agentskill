# 全项目重验：隐藏 bug 与命令覆盖缺陷

> 2026-09-17 ｜ 起因：送转口径回灌前做的全量复核
> 工具：`tools/audit_doc_commands.py`（本轮新增，可复跑）
> 范围：`docs/` + `joinquant/` 共 **136 条 `$PY` 命令** × `tools/` 共 **75 个脚本**

---

## 零、摘要

| 编号 | 缺陷 | 严重度 | 影响 | 状态 |
|---|---|---|---|---|
| **BUG-1** | 引擎成本是万3、模型宣称万5，调整式照万5扣 | ★★★ | **所有「10 万真实年化」偏低 0.10~0.33pp**（数字偏高） | ⚠️ **待拍板** |
| **BUG-2** | `sweep_poolsize.py` 费率 0.0003 | ★★ | 临界单笔金额 10,000 → 算成 16,667 | ✅ 已修 |
| **BUG-3** | `diag_dividend_into_mf.py` 无 `--adj-mode` | ★★ | 该文深挖部分**无法复现 legacy**（横幅说明是错的） | ✅ 已修 |
| **BUG-4** | `项目运行手册.md:124` `--topk` 应为 `--topn` | ★★ | 照文档跑**直接 argparse 报错** | ✅ 已修 |
| **BUG-5** | 18 条文档命令缺 `--adj-mode` | ★ | **静默**用默认口径，拿不到文档里的旧数字 | ⚠️ 待回灌时一并改 |
| **BUG-6** | 9 条文档命令缺 `--out` | ★ | 跑 correct 会**原地覆盖 legacy 证据** | ⚠️ 待回灌时一并改 |
| **BUG-7** | 涨跌停分档不完整（创业板时间分档/ST/北交所） | ★ | 判据确有错，但本项目触发次数 ≈ 0，**不改结论** | ⚠️ 低优先 |
| **BUG-8** | 候选不足时的资金处理，文档与实现不一致 | ★ | 文档说留现金，实现是等权满仓 | ⚠️ 待定稿时更正 |

**一句话**：**BUG-1 是本次最值钱的发现** —— 它和送转口径是同性质的问题
（"名义口径 ≠ 实际口径"），但影响面**更大**（送转只动股息率线，成本动**所有**线）。

---

## 一、BUG-1 ★★★ 引擎成本口径不自洽（**唯一需要主人拍板的**）

### 1.1 事实链（每一环都可复现）

```python
# ① 引擎默认值 —— tools/backtest_stock.py:135
def run(df, factor_specs, *, start, end, topk=50, hold=5, ..., 
        cost_buy=0.0003, cost_sell=0.0013,      # ← 佣金万3（cost_sell = 万3 + 印花税0.1%）
        slippage=0.0005, ...):
```

```python
# ② 事后调整式 —— tools/backtest_dividend.py:55-58
NOMINAL_FEE = 0.0005                                # 主人实际费率：万5
MODELED_ROUND = NOMINAL_FEE + (NOMINAL_FEE + STAMP) + SLIP * 2   # = 0.0005+0.0015+0.001 = 0.0030
```

```python
# ③ 调整逻辑 —— tools/backtest_dividend.py:271-274
comm       = max(NOMINAL_FEE, MIN_COMMISSION / ticket)
real_round = (comm + SLIP) + (comm + STAMP + SLIP)
extra      = x.年单边换手 * (real_round - MODELED_ROUND) * 100   # ← 假定引擎已按 0.0030 扣过
real       = x["年化%"] - extra
```

**引擎实际 round-trip = (0.0003+0.0005) + (0.0013+0.0005) = 0.0026**，
而调整式假定是 **0.0030** → **少扣 0.0004/round-trip**。

### 1.2 "没有调用方覆盖默认值" 的证据

`cost_buy` 在 `run()` 里是 **keyword-only**（签名里在 `*` 之后），不可能位置传参。全库扫：

```bash
$ grep -rn "cost_buy" tools/*.py | grep -v "^tools/backtest_stock.py"
（无输出）
```

→ **没有任何脚本传过成本参数**，全部走万3默认值。

### 1.3 量化（定稿形态 `indpct` N=20 / hold=60 / 10 万，`results/adjfix_correct_backtest.csv`）

| 区间 | 年化% | 年单边换手 | 现"10万真实" | **少扣pp** | **修正后** |
|---|---|---|---|---|---|
| 全区间 | 12.9460 | 2.640 | 12.6823 | −0.1056 | **12.5764** |
| 样本内 2019~2022 | 13.8751 | 2.689 | 13.6068 | −0.1076 | **13.4987** |
| **样本外 2023~2026** | 7.8331 | 2.740 | **7.5598** | −0.1096 | **7.4496** |

**通用公式**：`少扣(pp/年) = 0.0004 × 年单边换手 × 100`

| 线 | 换手 | 少扣 |
|---|---|---|
| 定稿形态（季度） | 2.64x | **0.11pp** |
| 月度 `hold=20` | ≈6.0x | 0.24pp |
| 多因子 8 因子线 | 8.21x | **0.33pp** |

> 验算（全区间那一行）：`2.640 × (0.004 − 0.0030) × 100 = 0.2640`，
> 而 `12.9460 − 12.6823 = 0.2637` ✓ → 恒等式成立，证明调整式的基数就是 `0.0030`。

### 1.4 为什么这不是"另一个 bug 而是同一类病"

`docs/HANDOFF_项目总结与下一步.md` §二 第 1 条：

> ETF 佣金率用 **万5** ｜ `config.TRADING_COST.commission_rate` 万3 → **万5**（已修）

**ETF 线改了，个股回测引擎没改。** 这正是 `tools/README.md` 铁律 14
「同一判据在多处实现 = 迟早分叉；**改对了 ≠ 改完了**」的第 N 个实例。

### 1.5 修复方案（**不擅自翻默认**，等一句话）

| 方案 | 做法 | 代价 |
|---|---|---|
| **A（推荐）** | `run()` 默认改 `cost_buy=0.0005, cost_sell=0.0015`，与 `MODELED_ROUND` 自洽 | 所有"10 万真实"数字降 0.1~0.33pp → **全部文档要回灌** |
| B | 保持引擎默认，把 `MODELED_ROUND` 改成 0.0026 | 等于承认"名义万5、实际万3"，且文档里的万5 说明要改 |
| C | 不动，只在每份文档标注"数字偏高 0.1~0.33pp" | 留着已知的自相矛盾 |

**我的建议：A，但和送转 correct 的回灌合并成一次做**（否则要回灌两轮）。

---

## 二、BUG-2 ★★ `sweep_poolsize.py` 费率漏改

```python
# tools/sweep_poolsize.py:40-42
MIN_COMMISSION = 5.0
NOMINAL_FEE = 0.0003                    # ← 全项目其它 4 处都是 0.0005
BREAK_EVEN_TICKET = MIN_COMMISSION / NOMINAL_FEE
```

`BREAK_EVEN_TICKET`（临界单笔金额，低于它就吃最低佣金）= 5/0.0003 = **16,667 元**；
正确值 = 5/0.0005 = **10,000 元** → **偏大 2/3**。

**已修**（改回 0.0005 并写明来由）。该脚本结论是「单调下坡，无拐点」，方向不受影响。

---

## 三、BUG-3 ★★ 深挖脚本口径不可复现

### 定位

`tools/diag_dividend_into_mf.py`
- 它 `from tools.sweep_dividend_into_mf import prepare`，而 `prepare` 内部是：
  ```python
  adj_mode=getattr(args, "adj_mode", "correct")
  ```
- 该脚本的 argparse **没有 `--adj-mode`** → `args` 无该属性 → **静默落到 `correct`**

### 后果

`docs/个股线_股息率并入多因子.md` 的横幅写着「复现本文旧数字：脚本加 `--adj-mode legacy`」，
但该文的**深挖部分**（逐年 / 逐调仓窗口 / 6 个邻域 / w=1→2）全部来自这个脚本
→ **那句话对该文是假的**，旧数字无法复现。

### 复现

```bash
$PY tools/diag_dividend_into_mf.py --adj-mode legacy   # audit-ignore: 修复前会报错（BUG-3 复现用例）
# 修复前：error: unrecognized arguments: --adj-mode
```

**已修**：加了 `--adj-mode`（纯透传，默认仍是 `correct`，不改变任何既有行为）。

---

## 四、BUG-4 ★★ 运行手册的命令照跑就报错

`docs/项目运行手册.md:124`（阶段 3②）：

```bash
$PY -u tools/test_industry_neutral.py \   # audit-ignore: 故意保留的错误命令（BUG-4 复现用例）
    --bars data/stockbars/bars_total_tax10.parquet \
    --topk 20 --hold 60 --hyst-entry 8 --hyst-exit 4 \
    --out-prefix results/adj_correct_runbook_check --adj-mode correct
```

**`--topk` 是 `backtest_stock.py` 的参数；本脚本定义的是 `--topn`。** 实测：

```
test_industry_neutral.py: error: unrecognized arguments: --topk 20
```

**已修**为 `--topn 20`，并补上 `--adj-mode correct`。

> 这是 `tools/audit_doc_commands.py` 的 A 类问题（**唯一 1 条**）——
> 说明项目文档质量整体是好的，这类错很少。

---

## 五、BUG-5 / BUG-6 ★ 命令覆盖不全（18 + 9 条）

### 5.1 缺 `--adj-mode`（18 条）→ 静默用默认口径

| 文档 | 行 | 脚本 |
|---|---|---|
| `个股线_四进三出阈值检验.md` | 292 / 296 / 300 | `sweep_hyst.py` |
| `个股线_机制归因_hyst与行业中性化.md` | 212 | `diag_industry_hyst_overlap.py` |
| `个股线_红利税.md` | 183 / 193 | `test_industry_neutral.py` / `backtest_dividend.py` |
| `个股线_股息率实盘方案.md` | 386 / 391 / 393 / 395 | `test_industry_neutral.py` |
| `个股线_股息率并入多因子.md` | 240 / 249 | `sweep_dividend_into_mf.py` / `diag_dividend_into_mf.py` |
| `个股线_股息率策略.md` | 348 / 355 / 363 / 364 | `test_dividend_factor.py` / `backtest_dividend.py` |
| `个股线_行业中性化.md` | 315 | `test_industry_neutral.py` |
| `新会话开场提示词.md` | 198 | `paper_track_dividend.py` |

**危害**：照文档跑 → 静默用 `correct` → 文档里的 `legacy` 数字**拿不到，也不报警**。
（方向上是"安全"的：拿到的是正确口径；但**可复现性没了**，而本项目最在意的就是这条。）

### 5.2 缺 `--out`（9 条）→ 会覆盖 legacy 证据

```bash
$PY tools/sweep_hyst.py --topn 20 --hold 60 --max-dy 10 --min-div3 2 --adj-mode correct   # audit-ignore: 示范：这样跑会覆盖 legacy 产物
#   ↑ 没有 --out → 默认写 results/hyst_sweep.csv，而那是 legacy 产物 → 原地覆盖
```

同样危险的默认输出名：`results/hyst_sweep.csv`、`results/dividend_into_mf.csv`、
`results/dividend_backtest.csv`、`results/dividend_factor_ic.csv`、
`results/diag_ind_hyst_overlap.csv`、`results/dividend_into_mf_diag.csv`。

**危害**：跑一次 correct，**legacy 原件永久消失** → 之后无法做 A/B 对照，
也无法验证"加开关没动旧结论"（那条正是送转立项最硬的证据之一）。

**纪律**（已写进 `tools/README.md`）：**跑 correct 一律显式 `--out results/adj_correct_*.csv`。**

### 5.3 待重跑清单的**实际覆盖**

`docs/个股线_送转调整口径缺陷.md` §四 给的 4 条命令，**盖不住它要回灌的 5 份文档**：

| 要回灌的文档 | 实际需要的命令 | 清单是否覆盖 |
|---|---|---|
| `个股线_行业中性化.md` | `test_industry_neutral --bars bars_total` | ✅ |
| `个股线_机制归因_hyst与行业中性化.md` | `diag_industry_hyst_overlap` | ✅ |
| `个股线_股息率并入多因子.md`（判定部分） | `sweep_dividend_into_mf` | ✅ |
| ↳ **（深挖部分）** | `diag_dividend_into_mf`（≈40 分钟） | ❌ **缺** |
| `个股线_四进三出阈值检验.md` | `sweep_hyst`（无税）**+ `--ctrl-only`（纯门槛对照）+ `--bars bars_total_tax10`（税后）** | ⚠️ **只覆盖 1/3** |
| `个股线_红利税.md` | `test_industry_neutral --bars bars_total_tax10` + `backtest_dividend --bars tax10` | ❌ **两条全缺** |
| `个股线_股息率策略.md`（横幅也在，但不在那 5 份名单里） | `test_dividend_factor` | ✅ **已跑完**（`results/adj_correct_ic.csv`） |

> ⚠️ `个股线_四进三出阈值检验.md` 的核心数字（8.30% → 11.08%）是**税后**的，
> 清单给的却是**无税**那条命令 —— 只跑清单，回灌不了这份文档。

---

## 六、BUG-7 ★ 涨跌停分档不完整（**真实，但当前影响可忽略**）

`tools/backtest_stock.py:99-102`：

```python
lim = np.where(df.code.str[2:5].str.startswith(("68", "30")), 0.20, 0.10)
df["limit"] = lim
df["buy_blocked"]  = df.open >= (df.prev_close * (1 + lim)).round(2)
df["sell_blocked"] = df.open <= (df.prev_close * (1 - lim)).round(2)
```

三处不完整：

| # | 问题 | 现状 | 应有 |
|---|---|---|---|
| 1 | **创业板涨跌幅有制度切换** | 全区间按 **20%** | **2020-08-24 起**才 20%，之前是 10% |
| 2 | **ST 涨跌幅 5%** | 未处理 | 转 ST 后按 5% |
| 3 | **北交所 30%** | 未处理 | 按 30%（当前池内无 BJ，属潜在） |

**实测（创业板窗口）**：

| 项 | 值 |
|---|---|
| 创业板 2019-01-02 ~ 2020-08-21 行数 | 309,451 |
| **被现行 20% 判据漏判的一字涨停行数** | **1,233**（占该窗口 0.398%） |
| 对照：**全区间**现行判据总共捕获的行数 | **585** |

→ **漏判量是现行判据全部捕获量的 2 倍** —— 判据在那个窗口基本是失效的。

**但影响可忽略**，理由要一起说清（否则会误判严重性）：
项目调仓频率低（定稿 32 期 × 20 只 ≈ 640 次买入），落在该窗口的创业板买入按比例
只有个位数量级 → **预期实际触发 ≈ 0 笔**。**不改变任何结论。**

**另有两处次生问题**：

1. `universe_all.csv` **已有 `board` 字段**（主板 3496 / 创业板 1452 / 科创板 622），
   引擎却用**代码前缀**重新发明判据 —— 又一处"判据重复实现"。
2. `.round(2)` 打在 `prev_close` 上，而 `prev_close` 来自 **`bars_total`（总收益指数价）**，
   不是真实价。涨停价按真实价四舍五入到分才有意义 → 判据应在**真实价**上做。

**修复建议**：用 `board` 字段 + 时间分档（创业板 `date >= 20200824`）+ ST 5% + 北交所 30%，
并加一条"分档覆盖度"断言。**优先级低**（不改结论），但应在下次动引擎时一起做。

---

## 七、BUG-8 ★ 文档与实现不一致

| 文档 | 写的 | 实现 |
|---|---|---|
| `个股线_股息率实盘方案.md` §7.2 | 候选池 < 20 → **有多少买多少，剩余资金留现金** | `backtest_stock.py:287-289`（cond_col 模式）`wv = ones(len(sel)); wv /= wv.sum()` → **等权满仓** |

`joinquant/README.md` §6.2 已经发现并写明"文档那句话应更正"，但
**`个股线_股息率实盘方案.md` 一直没改** → 定稿文档与实盘执行规则不一致。

**建议**：定稿文档 §7.2 改为"等权买满（与回测一致）"，或把引擎改成"不足 N 只时留现金"并重跑。
**两者会给出不同结果**，属于要主人拍板的口径选择。

---

## 八、已修 / 待办

### 已修（本轮）

| 文件 | 改动 |
|---|---|
| `tools/audit_doc_commands.py` | **新增**（本审计工具，可复跑） |
| `tools/diag_dividend_into_mf.py` | 补 `--adj-mode`（纯透传） |
| `tools/sweep_poolsize.py` | `NOMINAL_FEE` 0.0003 → **0.0005** |
| `docs/项目运行手册.md` | `--topk` → **`--topn`**，补 `--adj-mode correct` |

### 待办（按优先级）

1. **BUG-1 拍板**：成本口径是否现在修（我推荐 A，与送转回灌合并做一次）
2. 送转 correct 的**完整**回灌重跑（含 §5.3 表里被漏掉的 4 条命令）
3. BUG-5 / BUG-6：回灌时同步给 18 + 9 条命令补 `--adj-mode` / `--out`
4. BUG-7：下次动引擎时补涨跌停分档（不改结论）
5. BUG-8：定稿文档 §7.2 与实现对齐（需主人选口径）

---

## 九、复现命令

```bash
PY="C:/Users/XiaoQi/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
cd "E:/MyWorkAndProject/量化/agentskill"

# ── 命令覆盖审计（4 类问题，只读）──
$PY tools/audit_doc_commands.py
$PY tools/audit_doc_commands.py --json results/audit_doc_commands.json

# ── BUG-1：证明"没有调用方传成本参数" ──
grep -rn "cost_buy" tools/*.py | grep -v "^tools/backtest_stock.py"    # 应为空
sed -n '135p' tools/backtest_stock.py                                  # 看默认值 0.0003
sed -n '55,58p;271,274p' tools/backtest_dividend.py                    # 看调整式
# 量化：少扣 = 0.0004 × 年单边换手 × 100

# ── BUG-3：修复前会报错 ──
$PY tools/diag_dividend_into_mf.py --adj-mode legacy   # audit-ignore: 修复前会报错（BUG-3 复现用例）

# ── BUG-4：修复前会报错 ──
$PY tools/test_industry_neutral.py --topk 20 --bars data/stockbars/bars_total_tax10.parquet   # ← 故意保留的错误命令（BUG-4 的复现用例）   # audit-ignore: 故意保留的错误命令（BUG-4 复现用例）
```

---

## 免责声明

研究与教学用途，不构成任何投资建议。所有回测数字均为历史模拟结果，**不预示未来收益**。
