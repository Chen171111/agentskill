# 策略与交易链路缺陷修复计划

> **For agentic workers:** 按任务测试先行，逐项实现并勾选。用户已明确授权“修复所有缺陷包括策略以及代码”，计划直接执行。

**Goal:** 关闭两份审查报告的 F1—F10、E01—E08，并验证策略、回测、下单及调度的一致性。

**Architecture:** 信号与可成交原价分离；信号资格与择时共享；逐笔委托意图先落库、回执与成交按唯一编号追踪；真实交易入口统一时段和数据守卫。保留原工作树、账本历史及编码换行，不执行真实委托。

**Tech Stack:** Python 3.10/3.14、pandas、SQLite、Win32、PowerShell 5.1/7、unittest。

**Spec:** `docs/review_20261117/findings.md`、`environment_findings.md`。

## Global Constraints

- 每个缺陷先看失败的期望行为测试，再修实现；两种 Python 运行回归。
- 不改写实际持仓、资金、委托历史；数据库升级先用复制/临时账本验收。
- 沿用当前未提交更改；修改文件保留原 CRLF/LF/BOM；不提交、不推送仓库。
- 任务部署先验证无害退出码样本，再更新具体动作；不触发补跑。

## Review Focus

1. 空仓、冻结股份、资金不足与未知委托不能误作可买卖。
2. 提交中断后已报单可恢复，含同代码同数量的多单，不能重复报单。
3. 非调仓日止损、峰值和失败计数必须与回测一致。
4. 延长未来数据不得改变历史信号、股数、现金和费用。
5. 临近休市、日历缺失、刷新失败、窗口缺失必须拒绝真实交易。

## Task 1 · 券商读取、确认、回读（worker broker_fix）

Files: trader/ths_uia.py、ths_parse.py；tools/ths_trade_up.py、test_broker_guards.py、test_ths_parse.py。

- [x] F1/F2/F3、E02/E03/E04/E08 正确行为测试先失败。
- [x] 当前页资金、合法空仓、可信峰值与可用/冻结数量；确认关键字段缺失拒绝。
- [x] broker_order_id 唯一匹配；未知回执与部分/撤单/拒单终态保真。
- [x] 两种解释器下通过回归。

## Task 2 · 策略、数据、回测（worker strategy_fix）

Files: dataprovider/{store,adjust,panel}.py、factors/registry.py、strategies/{base,builtin,eligibility}.py、risk/timing.py、pipeline.py、backtest/{engine,account}.py、tools/test_strategy_integrity.py。

- [x] F4/F6/F8 先失败：共享正动量资格、择时和历史前缀一致性。
- [x] 只对声明的动量策略应用资格过滤；不按展示舍入决定交易。
- [x] 因果信号与 raw_* 执行价格分离，折算生效时调整份额/成本/峰值。
- [x] 明示推断折算比例的近似边界；精确输入路径和前缀因果性严格验收。
- [x] 两种解释器回归及权威回测重跑。

## Task 3 · 运行环境与调度（worker runtime_fix）

Files: daily_run.bat；tools/{daily_job,trading_session,run_scheduled,ths_autostart,ths_keepvisible}；tools/schedule/*；dataprovider/calendar.py；桌面 JSON 测试。

- [x] E01/E05/E06/E07/F10 先失败：退出码、首次/重试时段、健康检查和编码。
- [x] 隐藏运行器保留实际退出码；日历本地覆盖不足拒绝交易。
- [x] 手动补跑包含 64 位行情准备；自检不操作真实界面或告警通道。
- [x] 无害副本验收后由 root 部署计划任务动作，并复读磁盘定义。

## Task 4 · 逐笔执行与持久化（root）

Files: trader/{broker,execution,costs}.py、storage/{db,lock}.py、account/portfolio.py、scheduler/runner.py、main.py、tools/test_execution_reliability.py。

- [x] F5/F7/F9 先失败：拒单/对账失败不得推进计数，意图提交前落库，买入只用真实可用现金。
- [x] 订单唯一号、状态和预算持久化；卖出受 available 限制；最低佣金进预算。
- [x] 跨进程互斥与同一信号日去重；未决订单先核对，不盲重发。
- [x] 接入 raw_close、共享资格/择时/时段；CLI 失败以非零码传递。

## Task 5 · 集成与交付（root）

- [x] 新增测试加入正式自检，替换会误报的源码字符串断言为行为断言。
- [x] 原完整自检、所有新增回归、两种解释器和桌面适配测试通过。
- [x] 重跑默认 ETF 与多周期对照；更新 README 权威数字，旧结果保留为历史并标明作废。
- [x] 全清单关闭表、源码/编码/换行检查、任务落盘定义、HANDOFF 与追加台账。
- [x] 实际下单/成交没有执行；不得把离线通过写成真实成交验收。
