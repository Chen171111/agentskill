# 交接文档 HANDOFF

> 交接时间：2026-09-11　项目：agentskill（量化自动交易）
> 本文件供下一个 agent 快速接手，聚焦 **UIA 版同花顺强制下单测试** 的最新进展与未完成事项。

## 一、本次会话目标
在交易时段内完成同花顺模拟盘**强制下单测试**，验证 UIA 版 Broker 能真实提交一笔买入委托。

## 二、已完成的改动（本分支 master，未推送）
| 文件 | 改动 |
|---|---|
| `trader/ths_uia.py` | ① 键盘注入从 `SendInput`(pywinauto send_keys，被同花顺拦截，报 `SendInput() inserted only 0 out of 2`)改为 `keybd_event` 逐字符；② `_fill_edit` 改为 **剪贴板粘贴**(`_set_clipboard` + Ctrl+V)，规避逐字符丢字；③ 新增 `_vk_of`、`_set_clipboard`、`_clear_edit`；④ `_accept_dialogs` 重构为**不做窗口标题过滤**、直接前缀匹配按钮文本(`是/确定/确认/OK/Yes`)，适配「是(Y)」按钮；⑤ `connect()` 窗口匹配改为标题前缀 `同花顺`(不再硬编码 `网上股票交易系统5.0`)。新增 `import win32clipboard`、`import ctypes`。 |
| `test_uia_order.py` | 强制下单测试脚本：连接→读 510880 昨收→submit 买入100股→读当日委托/资金。输出重定向写 `uia_order_run.log`（管理员进程无控制台，便于事后读取）。 |

## 三、当前结论（关键！）
1. **UIA 可连接同花顺、可读资金/持仓/委托**，资金 OCR 读取正常（约 203116.96）。
2. 同花顺主窗口真实标题是 **`同花顺(9.60.61) - 自选股`**（MFC 类 `Afx:...`），**不是** `网上股票交易系统5.0` —— 这是 `connect()` 匹配修复的原因。
3. 买入面板是**独立的浮动弹窗**，点击「买」/F1 后出现（`_switch("buy")` 会触发），坐标随窗口布局变化，居中偏左。
4. **强行下单未真正成功**：`submit` 无异常（status=filled），但**当日委托查不到 510880、资金未扣款**，说明委托未真实上盘。
5. **根因卡点**：同花顺自绘输入框对 `SendInput`、`keybd_event` 均**不实时响应**——代码注入到 UIA 枚举的 Edit 控件（自绘影子控件，`BoundingRectangle` 坐标不可信、`set_value`/`WM_SETTEXT` 均无效），真正的代码框一直空白。剪贴板粘贴方案**尚未最终验证**（截图确认代码框仍空）。

## 四、下一步接力（下一个 agent 必须做的）
1. **确认交易时段与同花顺窗口可见**（客户端不能最小化/隐藏）。
2. 以**管理员权限**运行（同花顺提升权限运行，Python 需同权限才能键盘注入）。参考：
   `Start-Process -FilePath "E:\Python32\python.exe" -ArgumentList "test_uia_order.py" -WorkingDirectory "<agentskill>" -Verb RunAs -Wait`
   （用 `pythonw.exe` 无窗运行时 UIA 桌面枚举会**找不到窗口**，故用 `python.exe`，但会有黑色控制台遮挡——可用单独截图确认。）
3. **首选验证路径**：截图整窗定位买入面板代码框**真实像素坐标**，用绝对坐标点击聚焦后注入，截图 OCR 验证代码框是否出现 `510880` 且带出「红利ETF」名称。
   - 已有工具参考：`diag_locate.py`（已删，按需重建）思路＝connect→_switch("buy")→整窗截图 ImageGrab+OCR。
4. 若剪贴板粘贴（`_fill_edit` 当前实现）能填进代码框，则跑通 `test_uia_order.py`，确认当日委托出现 510880。
5. 注意 `fetch_balance` 的 OCR 对资金数字存在截断误读（曾出现 `96.0`），判定以「当日委托」记录为准。

## 五、环境与依赖
- Python：**32 位** `E:\Python32\python.exe`（操作同花顺必需）。
- 依赖：`pywinauto`、`win32clipboard`(pywin32)、`Pillow`、`pytesseract`；OCR：`E:\Tesseract-OCR\tesseract.exe` + `chi_sim`。
- 同花顺：经典版模拟炒股，客户端 `D:\同花顺软件\同花顺\xiadan.exe`，需已登录模拟会话。easytrader 已弃用。

## 六、其他须知
- git 仓库根在 `agentskill/`（不是上层 `量化/`），分支 `master`。
- 本分支本地改动未推送：`trader/ths_uia.py`(M)、`test_uia_order.py`(untracked)。诊断脚本 `diag_*` 本次已全部删除，如需重建按记录思路来。
- 本会话大量 `diag_*` 文件已清理，避免污染仓库。

## 七、风险提示
- 同花顺自绘 UI 是自动化最大障碍；若最终确认程序化注入完全无效且用户不接受「手动配合」，需如实告知用户：**实盘下单应改人工执行**，自动化仅做风控与信号生成。