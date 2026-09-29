"""同花顺「网上股票交易系统5.0」券商实现（win32 消息级，不依赖鼠标/焦点/OCR 下单）。

【为什么重写】
旧实现把交易窗口认成了行情窗口「同花顺(9.60.61) - 自选股」，那是个纯自绘行情界面，
里面的 Edit 是 UIA 影子控件，写入/点击都不生效 —— 这才是「强制下单不成交」的真正原因。
同花顺经典版实际有**两个**独立窗口：
    · 「同花顺(x.x.x) - 自选股」  行情（自绘，不可自动化）
    · 「网上股票交易系统5.0」    交易（标准 MFC，控件句柄齐全，可完全消息级自动化）
本模块只操作后者。

【落地方式】
1. 用 win32 后端按标题定位交易窗口，按**控件 ID** 取子控件句柄（买卖页共用同一套 ID）。
2. 填值用 WM_CHAR 逐字符投递给 Edit（关键：同花顺的输入框**忽略 WM_SETTEXT**，
   无法用 SetWindowText 清空；清空必须投递退格 WM_CHAR(8)）。
3. 下单点「买入/卖出」按钮用 BM_CLICK，不需要窗口在顶层、不需要焦点。
4. 「委托确认」弹窗的按钮是标准 Button（文本 `是(&Y)` / `否(&N)`），同样 BM_CLICK；
   提交前先解析弹窗里的 Static 文本，核对代码/价格/数量，防止脏数据上盘。
5. 资金直接读顶栏 Static 控件文本（按 ID），不用 OCR。

依赖（32 位 Python）：pywinauto（仅用于左侧菜单树定位）, pywin32, Pillow, pytesseract。
"""
import time
import ctypes
import logging
import math
from threading import Lock

import win32gui
import win32con
import win32process
from pywinauto import Desktop

logging.disable(logging.CRITICAL)

from .broker import Broker, Order

u32 = ctypes.windll.user32
try:
    u32.SetProcessDPIAware()
except Exception:
    pass

# ---------- 常量 ----------
_TRADE_TITLE = "网上股票交易系统5.0"   # 交易窗口标题（不是行情窗口！）
_TITLE_PREFIX = "网上股票交易系统"      # 版本变动时的前缀兜底

# 左侧菜单文字
_MENUS = {
    "buy": "买入[F1]", "sell": "卖出[F2]", "withdraw": "撤单[F3]", "query": "查询[F4]",
    "position": "资金股票", "today_order": "当日委托", "today_trade": "当日成交",
}
# F1~F4 加速键
_FKEYS = {"buy": win32con.VK_F1, "sell": win32con.VK_F2,
          "withdraw": win32con.VK_F3, "query": win32con.VK_F4}

# 买卖页共用的控件 ID（实测：买入页与卖出页 ID 完全一致）
_ID_TITLE = 1478    # 页面标题 Static：'买入股票'/'卖出股票'/'查询资金股票'...
_ID_CODE = 1032     # 证券代码 Edit
_ID_NAME = 1036     # 证券名称 Static（输入代码后自动带出）
_ID_PRICE = 1033    # 价格 Edit
_ID_MAXQTY = 1018   # 可买/可用股数 Static
_ID_QTY = 1034      # 数量 Edit
_ID_SUBMIT = 1006   # 买入/卖出 Button
_ID_REFILL = 1007   # 重填 Button（复位整张表单）

# 顶栏资金 Static ID
_BAL_TOTAL = 1015   # 总资产
_BAL_CASH = 1016    # 可用金额
_BAL_BALANCE = 1012  # 资金余额
_BAL_FROZEN = 1013  # 冻结金额
_BAL_WITHDRAW = 1017  # 可取金额
_BAL_MKTVAL = 1014  # 股票市值

_CONFIRM_YES = ("是", "确定", "确认", "OK", "Yes", "是(&Y)")
_CONFIRM_NO = ("否", "取消", "Cancel", "否(&N)")

# 弹窗关闭按钮的关键词（公告/提示/到期提醒类；区别于下单流程的 是/否 确认框）
_POPUP_CLOSE_WORDS = ("确定", "知道了", "知道了(&K)", "关闭", "关闭(&C)", "不再提醒",
                      "不再提示", "OK", "Cancel", "取消")


class UiaThsBroker(Broker):
    """同花顺交易客户端下单/查询（消息级自动化）。"""

    def __init__(self, exe_path=None, use_market_price=True):
        self._hwnd = None
        self._lock = Lock()
        self._exe_path = exe_path
        self._connected = False
        self._busy_windows = []   # 已判定为「非委托确认」的弹窗，避免重复匹配
        # 下单定价方式：
        #   True  = 不写价格框，交给客户端「价格跟随」自动带入**当前最新价**（默认，推荐）
        #   False = 用 order.price 显式限价
        # 为什么默认用市价：同花顺模拟盘的撮合要求「委托价 ≤ 当日最高价」，
        # 若用上一交易日收盘价当限价，遇到今日大跌，委托价会高于当日最高价 →
        # 挂在区间外一整天不成交（2026-09-11 实测：518880 限价 9.083 > 当日最高 8.963，0 成交）。
        self.use_market_price = bool(use_market_price)

    # ================= 连接 =================
    @staticmethod
    def _find_trade_hwnd():
        found = []

        def cb(h, _):
            try:
                if not win32gui.IsWindowVisible(h):
                    return
                t = (win32gui.GetWindowText(h) or "").strip()
                if t == _TRADE_TITLE or t.startswith(_TITLE_PREFIX):
                    found.append(h)
            except Exception:
                pass
        win32gui.EnumWindows(cb, None)
        return found[0] if found else None

    def connect(self, exe_path=None, retries=2):
        self._exe_path = exe_path or self._exe_path
        last_err = None
        for _ in range(retries + 1):
            h = self._find_trade_hwnd()
            if h:
                self._hwnd = h
                self._connected = True
                return self
            last_err = RuntimeError(
                "未找到交易窗口『{}』。请确认已打开同花顺交易/模拟客户端并登录"
                "（行情窗口『同花顺 - 自选股』不可用）".format(_TRADE_TITLE))
            time.sleep(1)
        raise RuntimeError("同花顺连接失败：{}".format(last_err))

    # ================= 控件检索 =================
    def _win(self):
        if not self._hwnd or not win32gui.IsWindow(self._hwnd):
            raise RuntimeError("UiaThsBroker 未连接（窗口句柄失效，请重新 connect）")
        return self._hwnd

    def _find_id(self, cid, root=None):
        """在 root 子树内按控件 ID 找所有窗口。"""
        root = root or self._win()
        found = []

        def rec(h, d=0):
            if d > 12:
                return
            c = u32.GetWindow(h, 5)          # GW_CHILD
            while c:
                try:
                    if u32.GetDlgCtrlID(c) == cid:
                        found.append(c)
                except Exception:
                    pass
                rec(c, d + 1)
                try:
                    c = u32.GetWindow(c, 2)   # GW_HWNDNEXT
                except Exception:
                    break
        rec(root)
        return found

    def _find_class(self, cls_name, root=None):
        root = root or self._win()
        found = []

        def rec(h, d=0):
            if d > 12:
                return
            c = u32.GetWindow(h, 5)
            while c:
                try:
                    if win32gui.GetClassName(c) == cls_name:
                        found.append(c)
                except Exception:
                    pass
                rec(c, d + 1)
                try:
                    c = u32.GetWindow(c, 2)
                except Exception:
                    break
        rec(root)
        return found

    def _active_page(self):
        """当前**显示中**的页面对话框（#32770）。

        买入页与卖出页会**同时存在**（各有自己的 id=1032/1033/1034 控件实例），
        只有正在显示的那个页面 `IsWindowVisible` 为真 —— 必须靠这个区分，否则会操作错页。
        """
        best = None
        for h in self._find_class("#32770"):
            rc = win32gui.GetWindowRect(h)
            w, ht = rc[2] - rc[0], rc[3] - rc[1]
            if w < 200 or ht < 200:
                continue
            if u32.IsWindowVisible(h):
                return h
            if best is None:
                best = h
        return best

    def _label_of(self, page_hwnd):
        if not page_hwnd:
            return ""
        for t in self._find_id(_ID_TITLE, root=page_hwnd):
            txt = (win32gui.GetWindowText(t) or "").strip()
            if txt:
                return txt
        return ""

    def _page_label(self):
        """当前页标题：'买入股票'/'卖出股票'/'查询资金股票'…"""
        return self._label_of(self._active_page())

    def _page_label_of(self, h):
        """某个控件所属页面的标题（沿父链向上找页面容器）。"""
        p = h
        for _ in range(8):
            p = win32gui.GetParent(p)
            if not p or p == self._win():
                break
            lab = self._label_of(p)
            if lab:
                return lab
        return ""

    def _ctrl(self, cid, page=None, visible_only=False):
        """按控件 ID 取句柄。

        page 给定时，只取「所属页面标题包含 page」的那个实例 —— 买卖页 ID 相同，
        不加这个约束会取到另一页的控件。
        """
        hs = self._find_id(cid)
        if page:
            for h in hs:
                if page in self._page_label_of(h):
                    return h
        if visible_only:
            for h in hs:
                if u32.IsWindowVisible(h):
                    return h
        if not hs:
            raise RuntimeError("未找到控件 id={}（当前页：{}）".format(cid, self._page_label()))
        return hs[0]

    # ================= 键盘/鼠标原语 =================
    @staticmethod
    def _post_char(h, ch, delay=0.06):
        u32.PostMessageW(h, win32con.WM_CHAR, ord(ch), 1)
        time.sleep(delay)

    @staticmethod
    def _post_backspace(h, n, delay=0.02):
        for _ in range(n):
            u32.PostMessageW(h, win32con.WM_CHAR, 8, 1)
            time.sleep(delay)

    @staticmethod
    def _bm_click(h):
        u32.PostMessageW(h, win32con.BM_CLICK, 0, 0)

    def _set_field(self, cid, text, maxlen=12, page=None):
        """写入输入框：先退格清空（WM_SETTEXT 对同花顺输入框无效），再逐字符输入。"""
        h = self._ctrl(cid, page=page)
        self._post_backspace(h, maxlen + 6)
        time.sleep(0.15)
        for ch in str(text):
            self._post_char(h, ch)
        time.sleep(0.25)
        return h

    def _click_real(self, x, y):
        u32.SetCursorPos(int(x), int(y))
        time.sleep(0.2)
        u32.mouse_event(2, 0, 0, 0, 0)   # LEFTDOWN
        time.sleep(0.06)
        u32.mouse_event(4, 0, 0, 0, 0)   # LEFTUP
        time.sleep(0.5)

    def _raise(self, keep=False):
        """把交易窗口提到最前（切换左侧菜单需要真实鼠标点击时会用到）。

        keep=True 时**保持置顶不取消**，需配合 `_unraise()` 恢复。
        为什么需要：截图用的是 `ImageGrab`（抓**屏幕像素**），窗口一旦被别的窗口盖住，
        抓到的就是**别人的画面**——实测表现为「持仓读成空 dict」，
        进而让 `_pos_ok` 判 100% 偏差而拒单（2026-09-16 15:11 实际发生）。
        原实现在置顶后**立刻** NOTOPMOST，中间没有窗口"被保证在前"的窗口期，
        截图前若被抢到前面就会截错。
        """
        try:
            u32.SetWindowPos(self._win(), -1, 0, 0, 0, 0, 0x0001 | 0x0002)  # TOPMOST
            time.sleep(0.25)
            if not keep:
                u32.SetWindowPos(self._win(), -2, 0, 0, 0, 0, 0x0001 | 0x0002)  # NOTOPMOST
                time.sleep(0.25)
        except Exception:
            pass

    def _unraise(self):
        """取消 `_raise(keep=True)` 造成的置顶，恢复普通层级的正常行为。"""
        try:
            u32.SetWindowPos(self._win(), -2, 0, 0, 0, 0, 0x0001 | 0x0002)  # NOTOPMOST
        except Exception:
            pass

    # ================= 页面切换 =================
    def _switch(self, key, timeout=6.0):
        """切换到指定页面。返回是否成功。"""
        label = _MENUS.get(key)
        if not label:
            return False

        # 已是目标页则直接返回
        cur = self._page_label()
        if label[:2] in cur and key in ("buy", "sell"):
            return True

        # ① 顶层菜单优先用 F1~F4 加速键（PostMessage 到主窗口，无需焦点）
        vk = _FKEYS.get(key)
        if vk is not None:
            scan = u32.MapVirtualKeyW(vk, 0)
            u32.PostMessageW(self._win(), win32con.WM_KEYDOWN, vk, 1 | (scan << 16))
            u32.PostMessageW(self._win(), win32con.WM_KEYUP, vk,
                             1 | (scan << 16) | (1 << 30) | (1 << 31))
            t0 = time.time()
            while time.time() - t0 < 1.5:
                time.sleep(0.2)
                if label[:2] in self._page_label():
                    return True

        # ② 回退：UIA 定位左侧树节点并真实点击
        # ⚠️ 点击前先清弹窗（2026-09-24 教训）：真实鼠标点击会被盖在上面的
        # 公告/提示弹窗截走 → 页面根本没切过去；且旧实现点击后**无条件
        # return True**，切页失败被吞掉 → 后续 OCR 读到的是别的页面的表格，
        # 列结构不同 → 整帧错位 → 对账失败拒单。
        self._dismiss_popups()
        self._raise()
        try:
            win = None
            for w in Desktop(backend="uia").windows():
                if (w.window_text() or "").strip() == _TRADE_TITLE or \
                        (w.window_text() or "").strip().startswith(_TITLE_PREFIX):
                    win = w
                    break
            if win is None:
                return False
            t0 = time.time()
            while time.time() - t0 < timeout:
                for it in win.descendants(control_type="TreeItem"):
                    try:
                        t = (it.window_text() or "").strip()
                    except Exception:
                        continue
                    if t.startswith(label):
                        r = it.rectangle()
                        cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
                        try:
                            it.select()
                        except Exception:
                            pass
                        self._click_real(cx, cy)
                        time.sleep(0.6)
                        # 点击后校验：真的切过去了才算成功（不再无条件 return True）
                        if label[:2] in self._page_label():
                            return True
                        break               # 没切过去 → 重找/重试
                time.sleep(0.4)
        except Exception:
            pass
        return False

    # ================= 下单 =================
    def _fill_order_form(self, order, page=None):
        """填代码/价格/数量，返回 {"name": 证券名称}。page 用于区分买卖页同名控件。"""
        code = order.code.split(".")[0]

        # 每次下单前先复位表单，避免上次输入残留（同花顺输入框只能是「追加」语义）
        try:
            self._bm_click(self._ctrl(_ID_REFILL, page=page))
            time.sleep(0.8)
        except Exception:
            pass

        # 证券代码（输入后客户端会自动带出证券名称、参考价、可买数量）
        self._set_field(_ID_CODE, code, page=page)
        time.sleep(1.2)
        name = ""
        try:
            name = (win32gui.GetWindowText(self._ctrl(_ID_NAME, page=page)) or "").strip()
        except Exception:
            pass
        if not name:
            raise RuntimeError("证券代码 {} 未被客户端识别（证券名称未带出）".format(code))

        # 价格：默认不动价格框，让客户端按「价格跟随」带入当前最新价（见 __init__ 说明）
        if not self.use_market_price and order.price:
            self._set_field(_ID_PRICE, str(round(float(order.price), 3)), page=page)
            time.sleep(0.3)

        # 数量
        self._set_field(_ID_QTY, str(int(order.qty)), page=page)
        time.sleep(0.4)
        return {"name": name}

    def _read_confirm_text(self, dlg):
        """读取「委托确认」弹窗里的提示文本（用于提交前核对）。

        弹窗内容由若干 Static 承载，正文那个是富文本(带 <font> 标签)，这里剥掉标签。
        """
        import re
        parts = []
        for h in self._find_class("Static", root=dlg):
            t = win32gui.GetWindowText(h) or ""
            if not t.strip():
                continue
            t = re.sub(r"<[^>]+>", "", t)
            if any(k in t for k in ("证券代码", "买入数量", "卖出数量", "买入价格", "卖出价格")):
                parts.append(t)
        return "\n".join(parts)

    def _owned_top_windows(self):
        """交易窗口拥有的顶层弹窗（委托确认/提示等都挂在这里，不在 GW_CHILD 子树里）。"""
        out = []
        h = u32.GetTopWindow(0)
        n = 0
        while h and n < 600:
            try:
                if u32.GetWindow(h, 4) == self._win() and u32.IsWindowVisible(h):
                    out.append(h)
            except Exception:
                pass
            h = u32.GetWindow(h, 2)
            n += 1
        return out

    def _find_confirm_dialog(self, timeout=8.0):
        """找「委托确认」弹窗：交易窗口的 owned 顶层 #32770，且同时含 是/否 按钮。"""
        exclude = set(self._busy_windows)
        t0 = time.time()
        while time.time() - t0 < timeout:
            for h in self._owned_top_windows():
                if h in exclude:
                    continue
                if win32gui.GetClassName(h) != "#32770":
                    continue
                btns = [(b, win32gui.GetWindowText(b) or "")
                        for b in self._find_class("Button", root=h)]
                joined = " ".join(t for _, t in btns)
                if "是" in joined and "否" in joined:
                    return h, btns
            time.sleep(0.3)
        return None, []

    def _confirm_dialog(self, dlg, expect_code=None, expect_qty=None, order=None):
        """核对弹窗内容并点「是(Y)」。返回 (ok, 弹窗文本)。

        真实委托价以弹窗为准（市价模式下我方不写价格框，价格由客户端带入），
        核对通过后回填到 order，保证台账记录的是真实报单价。
        """
        txt = self._read_confirm_text(dlg)
        info = parse_confirm_text(txt)

        if expect_code and expect_code not in txt:
            # 弹窗内容与预期不符 → 点「否」放弃
            self._click_dialog_button(dlg, _CONFIRM_NO)
            return False, txt
        if expect_qty is not None and info.get("qty") is not None \
                and int(info["qty"]) != int(expect_qty):
            self._click_dialog_button(dlg, _CONFIRM_NO)
            return False, txt
        if not self._click_dialog_button(dlg, _CONFIRM_YES):
            return False, txt
        if order is not None and info.get("price"):
            order.price = float(info["price"])
        return True, txt

    def _click_dialog_button(self, dlg, prefixes):
        """在弹窗里按按钮文本前缀 BM_CLICK。"""
        for b in self._find_class("Button", root=dlg):
            t = (win32gui.GetWindowText(b) or "").strip()
            if not t:
                continue
            for p in prefixes:
                if t.startswith(p) or p in t:
                    self._bm_click(b)
                    return True
        return False

    def _dismiss_info(self, timeout=3.0):
        """点掉「委托已成功提交」之类的提示框（交易窗口拥有的顶层弹窗）。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            for h in self._owned_top_windows():
                if win32gui.GetClassName(h) != "#32770":
                    continue
                btns = [(b, win32gui.GetWindowText(b) or "")
                        for b in self._find_class("Button", root=h)]
                joined = " ".join(t for _, t in btns)
                if "是" in joined and "否" in joined:
                    continue          # 那是委托确认框，不在这里处理
                if self._click_dialog_button(h, ("确定", "好的", "OK")):
                    time.sleep(0.4)
                    return True
            time.sleep(0.2)
        return False

    # ================= 弹窗清理（读表前） =================
    def _same_pid_top_windows(self):
        """与交易窗口**同进程**的可见顶层窗口（同花顺公告/提醒弹窗常挂在这里，
        不一定是交易窗口 owned 的 —— 2026-09-24 的遮挡弹窗即属此类，
        原来的 `_owned_top_windows` 枚举不到）。"""
        out = []
        try:
            _, pid = win32process.GetWindowThreadProcessId(self._win())
        except Exception:
            return out
        h = u32.GetTopWindow(0)
        n = 0
        while h and n < 1200:
            try:
                _, hp = win32process.GetWindowThreadProcessId(h)
                if hp == pid and u32.IsWindowVisible(h):
                    out.append(h)
            except Exception:
                pass
            h = u32.GetWindow(h, 2)
            n += 1
        return out

    def _dismiss_popups(self, tries=2):
        """读表前清理同花顺的公告/提示/到期提醒等弹窗。

        为什么需要（2026-09-24 教训）：这些弹窗会**持续**盖住持仓表格 →
        `fetch_position` 连拍 4 帧全部乱码/丢行（510880 整行丢失、513500 串列、
        513100 读到负价）→ 对账失败、拒单一整天。原 `_dismiss_info` 只在
        **下单后**调用、且只认「确定/好的/OK」按钮，公告类弹窗（知道了/关闭）
        和独立顶层弹窗都处理不了。

        安全边界（绝不误伤）：
          · 不碰交易窗口本身 / 大尺寸主窗口（>900×600）；
          · 不碰含「是+否」按钮的对话框 —— 那是委托确认框，只能由下单流程处理；
          · 非 #32770 类的窗口只在标题命中 公告/提示/提醒/通知/风险 时才动；
          · 只处理与交易窗口同进程的窗口。
        """
        closed = []
        for _ in range(tries):
            hit = False
            targets = []
            try:
                targets.extend(self._owned_top_windows())
            except Exception:
                pass
            targets.extend(self._same_pid_top_windows())
            for h in targets:
                if h == self._win():
                    continue
                try:
                    cls = win32gui.GetClassName(h)
                    title = (win32gui.GetWindowText(h) or "").strip()
                except Exception:
                    continue
                if cls == "#32770":
                    pass                      # 标准对话框，进入按钮判断
                elif any(k in title for k in ("公告", "提示", "提醒", "通知", "风险")):
                    pass                      # 自定义类的公告窗，按标题兜底
                else:
                    continue                  # 其余一律不碰
                # 委托确认框（是/否）绝不能在这里点 —— 防止误提交/误取消委托
                btns = [(b, win32gui.GetWindowText(b) or "")
                        for b in self._find_class("Button", root=h)]
                joined = " ".join(t for _, t in btns)
                if "是" in joined and "否" in joined:
                    continue
                # 大窗口（主窗体）跳过，只关小弹窗
                try:
                    rc = win32gui.GetWindowRect(h)
                    if (rc[2] - rc[0]) > 900 and (rc[3] - rc[1]) > 600:
                        continue
                except Exception:
                    continue
                if self._click_dialog_button(h, _POPUP_CLOSE_WORDS):
                    closed.append(title or cls)
                    hit = True
                    time.sleep(0.4)
                    continue
                # 没有可点按钮 → WM_CLOSE（纯展示型公告窗通常响应）
                try:
                    u32.PostMessageW(h, win32con.WM_CLOSE, 0, 0)
                    closed.append(title or cls)
                    hit = True
                    time.sleep(0.4)
                except Exception:
                    pass
            if not hit:
                break
        if closed:
            print("[ths] 读表前清理了 {} 个弹窗：{}".format(len(closed), closed))
        return closed

    def _submit_page(self, order, key):
        page = "买入股票" if key == "buy" else "卖出股票"
        self._switch(key)
        time.sleep(0.4)
        self._fill_order_form(order, page=page)

        btn = self._ctrl(_ID_SUBMIT, page=page)
        btxt = (win32gui.GetWindowText(btn) or "").strip()
        if ("买入" if key == "buy" else "卖出") not in btxt:
            raise RuntimeError("提交按钮文本异常：{!r}（当前页 {}）".format(btxt, self._page_label()))

        self._bm_click(btn)
        time.sleep(1.2)

        code = order.code.split(".")[0]
        dlg, _ = self._find_confirm_dialog()
        if dlg is None:
            raise RuntimeError("未出现「委托确认」弹窗，委托可能未提交成功")

        ok, txt = self._confirm_dialog(dlg, expect_code=code, expect_qty=int(order.qty),
                                       order=order)
        if not ok:
            raise RuntimeError("委托确认失败或被取消，弹窗内容：\n{}".format(txt))
        time.sleep(1.0)
        self._dismiss_info()

        order.status = "submitted"
        order.filled_price = float(order.price or 0.0)
        order.filled_qty = 0          # 真实成交量以当日委托回读为准
        order.fee = 0.0
        return order

    def submit(self, order: Order) -> Order:
        with self._lock:
            if order.side in ("buy", "买入"):
                self._submit_page(order, "buy")
            else:
                self._submit_page(order, "sell")
            return order

    # ================= 撤单 =================
    # 撤单页同样是多实例（买入页/卖出页/撤单页各有一套同 ID 按钮），
    # 必须锁定「当前活动页面」子树内的那一份，否则点了隐藏实例不会生效。
    _CANCEL_IDS = {"all": 30001, "buy": 30002, "sell": 30003, "single": 1099}

    def cancel_all(self, scope="all", timeout=6.0):
        """撤销可撤委托。scope: all=全撤 / buy=撤买 / sell=撤卖。

        返回 (是否点到按钮, 确认弹窗文本)。
        """
        self._switch("withdraw")
        time.sleep(1.2)
        cid = self._CANCEL_IDS.get(scope)
        if cid is None:
            raise ValueError("scope 只能是 all/buy/sell/single，收到 {!r}".format(scope))
        page = self._active_page()
        cands = self._find_id(cid, root=page) if page else []
        if not cands:
            cands = [h for h in self._find_id(cid) if u32.IsWindowVisible(h)]
        if not cands:
            raise RuntimeError("撤单页未找到撤单按钮 id={}（当前页 {}）".format(cid, self._page_label()))
        self._bm_click(cands[0])
        time.sleep(1.2)

        dlg, _ = self._find_confirm_dialog(timeout=timeout)
        txt = ""
        if dlg is not None:
            txt = self._read_confirm_text(dlg)
            self._click_dialog_button(dlg, _CONFIRM_YES)
            time.sleep(0.8)
        self._dismiss_info()
        return True, txt

    # ================= 查询 =================
    def _read_static(self, cid, prefer_page=None):
        """按 ID 读 Static 文本，**所属页面优先**排序后返回。

        ⚠️ 必须按「所属页面」过滤（2026-09-21 修复，与 `_grid_rect` 同一类坑）
        ---------------------------------------------------------------------
        同名控件 ID 在**多个页面上同时存在**（买入 / 卖出 / 资金股票的顶栏资金栏
        各有一套 1012~1017），且隐藏实例的 `GetWindowText` **照样返回旧值**。
        原实现收集所有非空实例后取 `vals[-1]`（枚举顺序的最后一个）→ 读到隐藏页的
        陈旧数字：
            总资产   219,157.24（隐藏页）  vs  190,912.44（真实当前页）
            股票市值 121,595.20（隐藏页）  vs   93,350.40（真实当前页）
        而真实当前页的值可由持仓表自证：95944.00 / 190912.44 = 50.26%，与表格
        「仓位占比」汇总 50.26% 完全吻合。读错值直接导致下单前对账偏差 95.61% 拒单
        （2026-09-21 14:50 实际发生）。

        返回顺序：当前显示页的实例 → 其它可见实例 → 其余（保持旧兜底行为）。
        """
        cur = prefer_page if prefer_page is not None else self._page_label()
        on_page, visible, others = [], [], []
        for h in self._find_id(cid):
            t = (win32gui.GetWindowText(h) or "").strip()
            if not t:
                continue
            if cur and cur in self._page_label_of(h):
                on_page.append(t)
            elif u32.IsWindowVisible(h):
                visible.append(t)
            else:
                others.append(t)
        return on_page + visible + others

    @staticmethod
    def _f(s):
        try:
            return float(str(s).replace(",", ""))
        except (TypeError, ValueError):
            return None

    def fetch_balance(self) -> dict:
        # 先清弹窗：切页走「真实点击」分支时，弹窗会截走点击导致切页失败
        # （2026-09-24 教训，详见 _dismiss_popups）
        self._dismiss_popups()
        self._switch("query")
        time.sleep(0.6)
        self._switch("position")     # 资金股票页，含完整资金字段
        time.sleep(0.8)
        # 记录当前显示页，供 _read_static 过滤掉「隐藏页上的同名陈旧控件」
        page = self._page_label() or _MENUS["position"]

        def pick(cid):
            vs = self._read_static(cid, prefer_page=page)
            return self._f(vs[-1]) if vs else None

        return {
            "total": pick(_BAL_TOTAL),
            "cash": pick(_BAL_CASH),
            "balance": pick(_BAL_BALANCE),
            "frozen": pick(_BAL_FROZEN),
            "withdraw": pick(_BAL_WITHDRAW),
            "market_value": pick(_BAL_MKTVAL),
        }

    def _grid_rect(self):
        """当前页**可见**的最大 CVirtualGridCtrl 矩形（表格）。

        ⚠️ 必须过滤可见性（2026-09-14 发现）
        ------------------------------------
        买入/卖出/撤单/持仓/当日委托页**同时存在**，每个页面都有自己的
        `CVirtualGridCtrl`；而且**隐藏窗口的 `GetWindowRect` 照样返回有效矩形**。
        因此只按「面积最大」挑，会**截到别的页面的表格**——
        列结构和列数都不同，OCR 出来的数字全错位，进而触发对账拒单。

        正确做法：先用 `IsWindowVisible` 过滤，只在**当前显示**的页面上选。
        """
        best = None
        hidden_skipped = 0
        for h in self._find_class("CVirtualGridCtrl"):
            if not u32.IsWindowVisible(h):
                hidden_skipped += 1
                continue
            rc = win32gui.GetWindowRect(h)
            w, ht = rc[2] - rc[0], rc[3] - rc[1]
            if w < 300 or ht < 80:
                continue
            area = w * ht
            if best is None or area > best[0]:
                best = (area, rc)
        if best is None and hidden_skipped:
            # 兜底：若一个可见的都没有（例如页面切换中），退回原逻辑，避免直接失败
            for h in self._find_class("CVirtualGridCtrl"):
                rc = win32gui.GetWindowRect(h)
                w, ht = rc[2] - rc[0], rc[3] - rc[1]
                if w < 300 or ht < 80:
                    continue
                area = w * ht
                if best is None or area > best[0]:
                    best = (area, rc)
        return best[1] if best else None

    def _read_grid_text(self):
        """OCR 当前表格区域。位置精确到控件矩形，比整窗 OCR 准得多。

        ⚠️ OCR 参数是关键（2026-09-14 实测踩坑）
        ------------------------------------------
        同花顺表格是 `CVirtualGridCtrl` **自绘**的，列间距大。
        **默认 PSM(3) 会把每一列切成独立段落**，输出形如：

            '三: 510880 ”红利ETF华             1600'
            '三: 511010 ”国债ETF国                           0'
            '可用款额'
            '1600'
            '市价'
            '3.401'

        而 `parse_position` 假设「一行含全部列」→ 解析出的行只有 1~2 个数字，
        被 `<4` 的守卫丢掉；运气差时会读出**错位的数量**，触发对账拒单
        （2026-09-14 14:54 实际发生：511010 读成 400 股，实际 300）。

        **改用 `--psm 6`（假设统一文本块）后按行输出完整行**，
        再放大 **4 倍**让小数点被正确识别（2 倍时 `3.424` 会丢点变成 `3424`，
        3 倍时虽多数正确、但实测仍偶发丢点，4 倍下整行结构最完整且耗时相近）。

        实测耗时（903×494 表）：2x 0.74s / 3x 0.99s / 4x 1.09s；
        （903×360 表）：2x 1.00s / 4x 1.05s → 4x 的稳定性更值得这点代价。

        注意：ImageGrab 抓的是**屏幕像素**，同花顺被别的窗口盖住时会 OCR 到别人的画面。
        因此截图前必须先把交易窗口提到最前，否则会「读到空表格」而误判为空仓。
        """
        import pytesseract
        from PIL import ImageGrab
        rc = self._grid_rect()
        if not rc:
            return ""
        self._raise(keep=True)
        try:
            time.sleep(0.3)
            img = ImageGrab.grab(bbox=rc)
        finally:
            self._unraise()
        img = img.resize((img.width * 4, img.height * 4))
        try:
            return pytesseract.image_to_string(
                img, lang="chi_sim+eng", config="--psm 6")
        except Exception:
            return ""

    @staticmethod
    def _mk_price(code6, p, prices) -> float:
        """给一行 OCR 持仓定价：本地行情价 → OCR 市价 → OCR 成本价。

        ⚠️ 顺序不能颠倒（2026-09-21 修复）
        ---------------------------------
        原实现只有「行情价 → 成本价」。策略池只有 11 只 ETF，账户里**人工买入的
        个股永远取不到行情价**（601091 不在池内）→ 回退成**成本价** → 等于拿
        「成本」去核对「资金栏市值」，只当日涨跌就必然偏离：
            2400 股 × 成本 41.673 = 100,015  vs  资金栏市值口径 ~93,350（偏差 7.1%）
            → 超过 5% 闸门 → 拒单打不出来，即使持仓行已能正确解析。
        正确顺序：池外标的应用 OCR 读到的「市价」列 —— 2026-09-21 实测该列读得准
        （2400 × 37.720 = 90,528.000，与表格「市值」列 90528.000 逐位一致）。
        """
        px = prices.get(UiaThsBroker._guess_full_code(code6))
        if px:
            return float(px)
        return float(p.get("price") or p.get("cost") or 0.0)

    def _pos_calc(self, pos, prices) -> float:
        """Σ(行情价×数量) —— 判据 3 与 `_dump_pos_failure` 共用的**单一来源**。

        ⚠️ 铁律 14（同一判据多处实现＝迟早分叉）：两边都调这里，不得各写循环。
        """
        return sum(p["qty"] * self._mk_price(code, p, prices)
                   for code, p in pos.items())

    def _missing_positions(self, pos, old, prices):
        """本地有、本帧没读到的持仓 → (市值合计, [(code, qty, px), ...])。

        ⚠️ `old` 是全代码 dict（DB 口径），`pos` 是 6 位代码 dict（OCR 口径），
        比较前必须补后缀，否则永远判「全部消失」。
        """
        held = {self._guess_full_code(c) for c in pos}
        total, items = 0.0, []
        for code, v in (old or {}).items():
            if v.get("qty", 0) > 0 and code not in held:
                px = (prices.get(code) or v.get("mark")
                      or v.get("cost") or 0.0)
                total += v["qty"] * px
                items.append((code, v["qty"], px))
        return total, items

    def _pos_ok(self, pos, bal, prices, old=None) -> bool:
        """校验一次持仓读取是否可信（防止 OCR 坏数据污染账本）。

        判据（任一不满足即判读取失败）：
          1. 有持仓市值却读不到任何明细；
          2. 任一行数量 ≤ 0；
          2.5 帧级行内自洽（2026-09-24 新增）：任一行 price<0（盈亏列被读成市价）
              或 available+frozen != qty（串列）→ 整帧判坏。当天实际坏帧：
              513500 读成 qty=500/available=21200/frozen=21200，4 连拍全坏拒单；
          3. 用**行情价**×数量 与资金栏「股票市值」偏差 > 5%
             （行情价来自数据层，可靠；OCR 的价列**仅**在取不到行情价时兜底
              —— 见 `_mk_price`：池外标的必须用 OCR 市价，不能用成本价）；
          4. 本地原有持仓在新读结果里消失 —— 用**市值反证**区分「真清仓」与「OCR 漏读」；
             读到的持仓已能解释市值（<2%）时，本地多余持仓按**幽灵**处理、允许覆盖
             （2026-09-28 修订，见下）。

        判据 4 的原理（2026-09-14 建立，2026-09-28 修订）
        --------------------------------------------------
        原实现直接判「消失 = 漏读 → 拒单」，其注释假设「真清仓会让股票市值归零」，
        **该假设是错的**：真清仓后市值栏只剩**剩余**持仓的市值，并不为零，
        因此卖出后会走到这条并被误判 → 整套流程被卡死一整天（14:54 实际发生）。

        2026-09-14 修正为「市值反证」（把消失的持仓按行情价加回去，与资金栏市值比）：
          - **漏读**：真实持仓 = 读到的 + 漏掉的 → `calc + missing ≈ mv` → 加回去**不超出** mv
          - **真清仓**：漏掉的那些其实已不在券商账上 → 加回去**会显著超出** mv
        故：仅当 `calc + missing <= mv × 1.05` 时才判为漏读并拒单。

        2026-09-28 再修订：固定 5% 容差是**结构性缺陷**
        ----------------------------------------------
        幽灵持仓（本地有、券商无 —— 如 09-22 卖出未落账）占比 < 5% 时，
        `calc + missing` 永远落在 `mv×1.05` 内 → 永远判「漏读」→ **永久死锁**，
        且重试救不了（同一帧是确定性结果，4 连拍只会得到 4 次相同的拒单）。
        实况（09-23~09-28 交易线锁死 4 个交易日）：510880 市值 5,371 占 mv 的
        4.7% < 5% → `120,072 <= 120,225.84` 恒成立 → 每天必失败。

        新规则（先问「读到的持仓」能否解释市值，再追究差额）：
          ① `calc ≈ mv`（偏差 < 2%）→ 券商侧已读全 → 本地多出的必是幽灵
             → **允许覆盖**（用券商真值盖掉本地账本正是 reconcile 的职责；
             此处拒单只会重蹈死锁，而让幽灵继续躺在账本里没有任何好处）；
          ② 差额 ≥ 2%（确有东西没读到）→ 回到市值反证：
             `calc + missing <= mv × 1.05` 判漏读并拒单。
        2% 阈值依据：实测 calc vs mv 噪声底 0.06%~0.53%（行情源一致时），
        2% ≈ 4 倍余量，且判据 3 的 5% 闸门仍在前置把关。**已知代价**：漏读
        < 2% 的小仓位会按幽灵放过（账本少记一天、下次读取自愈，幅度被 2%
        天然封顶）—— 用这个有界且自愈的代价，换消除结构性死锁。
        数值回归：`tools/test_pos_ok.py`（真数字 7 场景，需 32 位 Python）。
        """
        try:
            mv = float(bal.get("market_value") or 0.0)
        except (TypeError, ValueError):
            mv = 0.0
        if not pos:
            return mv <= 1.0 and bal.get("cash") is not None
        # 判据 2.5：帧级行内自洽（负价/串列帧直接判坏，见 pos_frame_sane 注释）
        if not pos_frame_sane(pos):
            return False
        for p in pos.values():
            if int(p.get("qty") or 0) <= 0:
                return False
        if mv > 1.0:
            calc = self._pos_calc(pos, prices)
            # NaN/inf 闸门（2026-09-29）：nan 的比较运算**全部返回 False**——
            # 不挡它，下面判据 3（5% 闸门）与判据 4（幽灵分支 + 市值反证）会
            # 全部静默放行，坏帧混过校验、nan 成本价写进账本（实测复现过该穿透）。
            if not math.isfinite(calc):
                return False
            if calc <= 0 or abs(calc - mv) / mv > 0.05:
                return False
            # 判据 4：本地持仓消失 → 「幽灵放行」与「市值反证」两级（原理见 docstring）
            missing, ghosts = self._missing_positions(pos, old, prices)
            if missing > 0:
                if abs(calc - mv) / mv < 0.02:
                    # 读到的持仓已能解释资金栏市值（偏差 <2%）→ 券商侧读全，
                    # 本地多出的必是幽灵 → 允许覆盖（reconcile 会用券商真值
                    # 重写 positions，幽灵自然出账；拒单只会重蹈死锁）。
                    print("[reconcile] ⚠️ 本地持仓在券商侧不存在（calc 与 mv 自洽 "
                          "{:.2f}% < 2%），按幽灵处理、允许覆盖：{}".format(
                              abs(calc - mv) / mv * 100,
                              "; ".join("{} {}股@{:.3f}".format(c, q, px)
                                        for c, q, px in ghosts)))
                elif calc + missing <= mv * 1.05:
                    return False
        return True

    def _dump_pos_failure(self, pos, bal, prices, old) -> None:
        """对账校验失败时打印诊断数字。

        为什么需要（2026-09-14 教训）：`reconcile` 原来失败时**只返回 False**，
        runner 抛出的错误里没有任何数字，事后完全无法定位是「读到了什么」出的问题
        —— 只能靠重开同花顺反复复现。把关键数字打进日志，下次一眼就能看出
        是 OCR 读错数量、还是资金栏/行情价不对。
        """
        try:
            mv = float(bal.get("market_value") or 0.0)
            calc = self._pos_calc(pos, prices)
            detail = []
            for code, p in pos.items():
                px = self._mk_price(code, p, prices)
                detail.append("{} qty={} px={:.3f}{}".format(
                    code, p["qty"], px,
                    "" if prices.get(self._guess_full_code(code)) else "(池外/无行情价)"))
            missing, items = self._missing_positions(pos, old, prices)
            dev = abs(calc - mv) / mv * 100 if mv else 0.0
            print("[reconcile] ⚠️ 持仓校验未通过，诊断数字：")
            print("   读到 pos   =", pos)
            print("   资金栏 bal =", bal)
            # 打印**全部**行情价池（原先硬编码 3 只，池子改版后已失效）
            print("   行情价池   =", {k: round(float(v), 4)
                                      for k, v in sorted(prices.items())})
            print("   计价明细   = " + ("; ".join(detail) if detail else "(无持仓)"))
            print("   calc(行情价×数量) = {:,.2f}   mv(资金栏市值) = {:,.2f}   偏差 = {:.2f}%".format(
                calc, mv, dev))
            print("   本地消失的持仓 = " + ("; ".join(
                "{} qty={} px={:.3f}".format(c, q, px) for c, q, px in items) if items else "(无)"))
            print("   calc+missing = {:,.2f}   vs   mv×1.05 = {:,.2f}".format(
                calc + missing, mv * 1.05))
            if missing > 0 and dev < 2.0:
                print("   → calc 与 mv 自洽 {:.2f}% < 2%：本地多余持仓按幽灵处理"
                      "（允许覆盖，不因此拒单）".format(dev))
        except Exception as e:                       # 诊断本身绝不能影响主流程
            print("[reconcile] (诊断打印失败: {})".format(e))

    def fetch_position(self, validator=None, tries=4) -> dict:
        """回读持仓。

        OCR 单次拍摄会漏行/读串列，因此连拍多次，并以 validator 作为「这一帧够不够可信」
        的停止条件；validator 给定时返回首个通过校验的结果，否则返回行数最多的那一帧。

        2026-09-24 教训后的三重加固（当天 4 连拍全部乱码/丢行 → 对账失败拒单）：
        ① 每帧先清弹窗 —— 公告/提示弹窗持续盖住表格，不清则帧帧皆坏；
        ② 切页后校验页标题（必须是「资金股票」页）—— 切页失败会 OCR 到别的
           页面的表格，列结构不同 → 整帧错位；
        ③ 页标题对了才拍，坏帧（串列/负价/未过校验）自动进入下一帧重拍。
        """
        best = {}
        for i in range(tries):
            self._dismiss_popups()
            self._switch("query")
            time.sleep(0.3)
            if not self._switch("position"):
                print("[ths] 持仓第 {}/{} 帧：切页失败，跳过重试".format(i + 1, tries))
                time.sleep(0.5)
                continue
            page = self._page_label() or ""
            if "资金股票" not in page:
                print("[ths] 持仓第 {}/{} 帧：页面停在 {!r}（非资金股票页），跳过重试".format(
                    i + 1, tries, page))
                time.sleep(0.5)
                continue
            time.sleep(1.0)
            cur = parse_position(self._read_grid_text())
            if len(cur) > len(best):
                best = cur
            if validator is not None:
                try:
                    if cur and validator(cur):
                        return cur
                except Exception:
                    pass
            elif len(cur) >= 3:
                return cur
            time.sleep(0.4)
        return best

    def fetch_today_orders(self, tries=3) -> list:
        """回读当日委托（连拍取行数最多的那一帧）。"""
        best = []
        for _ in range(tries):
            self._dismiss_popups()
            self._switch("query")
            time.sleep(0.3)
            if not self._switch("today_order"):
                time.sleep(0.5)
                continue
            time.sleep(1.0)
            cur = parse_trades(self._read_grid_text())
            if len(cur) > len(best):
                best = cur
            if len(best) >= 3:
                break
            time.sleep(0.4)
        return best

    def sync_fill(self, order: Order):
        """回读当日委托，用真实成交价/成交量修正订单。"""
        try:
            rows = self.fetch_today_orders()
            code = order.code.split(".")[0]
            for r in rows:
                if (r.get("code") or "").zfill(6) != code:
                    continue
                if r.get("avg_price"):
                    order.filled_price = r["avg_price"]      # 成交均价
                elif r.get("price"):
                    order.filled_price = r["price"]          # 委托价兜底
                if r.get("filled_qty"):
                    order.filled_qty = r["filled_qty"]
                order.status = "filled" if r.get("status") == "已成" else "submitted"
                return
        except Exception:
            pass

    def reconcile(self, account, prices=None) -> bool:
        """用同花顺真实持仓/资金校正本地账户，消除双账本。

        prices：行情数据里的现价 {全代码: 价}。**强烈建议传入**——持仓表的 OCR 会偶发
        读串列（实测出现过「成本价 8950」其实是 8.950、「市价」读成盈亏值），
        仅凭 OCR 自身无法发现。有了行情价就可以：
          ① 校验每行成本价是否离谱（偏离行情价 >50% 即判 OCR 读错，退回行情价）
          ② 用行情价×数量 与资金栏「股票市值」交叉核对，作为整体可靠性闸门

        注意：fetch_position() 返回 6 位纯代码，而全系统（prices / target_weights / DB）
        一律用带后缀的全代码（如 510880.SH）。这里必须补全后缀，否则持仓无法被定价
        —— 表现为 market_value 恒为 0，且引擎误以为空仓而重复买入。
        """
        prices = prices or {}
        bal = self.fetch_balance()
        old = getattr(account, "positions", {}) or {}

        # 资金栏是交叉核对的「锚」：读不到市值/现金时（资金控件也读坏），
        # _pos_ok 的判据 3/4 全部静默跳过、只剩弱判据 —— 坏帧可能混过校验。
        # 直接判对账失败，让人工介入（2026-09-24 加固：宁可拒单，不要脏账本）。
        try:
            _mv = float(bal.get("market_value") or 0.0)
        except (TypeError, ValueError):
            _mv = 0.0
        if bal.get("cash") is None or _mv <= 0:
            print("[reconcile] ⚠️ 资金栏读取异常（bal={}），无法交叉核对 → 判对账失败".format(bal))
            return False

        # 连拍持仓，以「市值自洽」作为停止条件：读到可信的那一帧为止
        pos = self.fetch_position(validator=lambda p: self._pos_ok(p, bal, prices, old))
        if not self._pos_ok(pos, bal, prices, old):
            self._dump_pos_failure(pos, bal, prices, old)
            return False

        new_pos = {}
        for code, p in pos.items():
            full = self._guess_full_code(code)
            qty = int(p["qty"])
            cost = float(p.get("cost") or 0.0)
            ref = prices.get(full) or p.get("price")
            # 成本价校验：偏离行情价 >50% 视为 OCR 读错（实测 8.950 被读成 8950），退回行情价
            if ref and cost > 0:
                if not (0.5 * ref <= cost <= 2.0 * ref):
                    cost = float(ref)
            elif not cost and ref:
                cost = float(ref)
            old_peak = old.get(full, {}).get("peak", 0.0) if old.get(full) else 0.0
            # mark：该标的的「最后已知价」，池外标的靠它才能被正确定价
            # ⚠️ 为什么必须存（2026-09-21 修复）
            # --------------------------------
            # `PortfolioAccount.market_value()` 用 `prices.get(code, 0.0)` 计价，
            # 而 prices 只有策略池（11 只 ETF）。账号里人工买入的个股（601091）
            # 因此**市值为 0** → 总资产少算 9 万（真实 190,912 vs 算出 102,904，
            # 虚低 46%）→ ①净值序列被污染、②仓位按虚低的 equity 计算、
            # ③回撤熔断会把「净值腰斩」误判成巨亏而清仓。存下 mark 即可让
            # market_value 用 OCR 市价兜底，口径立刻自洽。
            mark = self._mk_price(code, p, prices)
            new_pos[full] = {"qty": qty, "cost": round(cost, 4),
                             "peak": max(cost, old_peak),
                             "mark": round(mark, 4)}

        # 校验通过，落账
        account.positions = new_pos
        if bal.get("cash") is not None:
            account.cash = bal["cash"]
        elif bal.get("total") is not None and bal.get("market_value") is not None:
            account.cash = bal["total"] - bal["market_value"]
        # 冻结资金（已报未成交委托占用）仍属账户资产，须计入净值，
        # 否则「下单瞬间资产骤降」会被回撤熔断误判成巨亏而清仓。
        try:
            account.frozen = float(bal.get("frozen") or 0.0)
        except (TypeError, ValueError):
            account.frozen = 0.0
        return True

    @staticmethod
    def _guess_full_code(code6) -> str:
        """6 位纯代码 -> 带市场后缀的全代码（5xx/6xx/9xx 沪市；1xx/0xx/3xx 深市）。"""
        c = str(code6).zfill(6)
        if c.startswith(("6", "9", "5")):
            return c + ".SH"
        if c.startswith(("0", "3", "1")):
            return c + ".SZ"
        return c


# ================= OCR 文本解析（表格兜底） =================
#
# 实现已抽到 `trader/ths_parse.py`（纯函数、**无 win32 依赖**）。
# 为什么抽出去（2026-09-21）：本模块顶层 `import win32gui / pywinauto`，
# 只有 32 位 Python 能加载 → 持仓/成交**行解析**这条最易静默出错的逻辑
# 进不了 64 位跑的 `tools/selftest.py`，历次坑只能人工连客户端复现
# （2026-09-21 的「C沈鼓」丢行即为此类）。现在 selftest 可用真实 OCR 原文回归。
# 这里 re-export 保持向后兼容：`from trader.ths_uia import parse_position` 照旧可用。
from .ths_parse import (  # noqa: E402
    pos_frame_sane, parse_confirm_text, parse_position, parse_trades)
