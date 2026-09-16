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
from threading import Lock

import win32gui
import win32con
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
            raise RuntimeError("ThsBroker 未连接（窗口句柄失效，请重新 connect）")
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
                        return True
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
    def _read_static(self, cid):
        """按 ID 读 Static 文本；多个实例时优先取非空且不等于 '?' 的。"""
        vals = []
        for h in self._find_id(cid):
            t = (win32gui.GetWindowText(h) or "").strip()
            if t:
                vals.append(t)
        return vals

    @staticmethod
    def _f(s):
        try:
            return float(str(s).replace(",", ""))
        except (TypeError, ValueError):
            return None

    def fetch_balance(self) -> dict:
        self._switch("query")
        time.sleep(0.6)
        self._switch("position")     # 资金股票页，含完整资金字段
        time.sleep(0.8)

        def pick(cid):
            vs = self._read_static(cid)
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

    def _pos_ok(self, pos, bal, prices, old=None) -> bool:
        """校验一次持仓读取是否可信（防止 OCR 坏数据污染账本）。

        判据（任一不满足即判读取失败）：
          1. 有持仓市值却读不到任何明细；
          2. 任一行数量 ≤ 0；
          3. 用**行情价**×数量 与资金栏「股票市值」偏差 > 5%
             （行情价来自数据层，可靠；OCR 的价列不可用于此校验）；
          4. 本地原有持仓在新读结果里消失 —— 用**市值反证**区分「真清仓」与「OCR 漏读」。

        判据 4 的原理（2026-09-14 修正）
        --------------------------------
        原实现直接判「消失 = 漏读 → 拒单」，其注释假设「真清仓会让股票市值归零」，
        **该假设是错的**：真清仓后市值栏只剩**剩余**持仓的市值，并不为零，
        因此卖出后会走到这条并被误判 → 整套流程被卡死一整天（14:54 实际发生）。

        正确判据（把消失的持仓按行情价加回去，与资金栏市值比）：
          - **漏读**：真实持仓 = 读到的 + 漏掉的 → `calc + missing ≈ mv` → 加回去**不超出** mv
          - **真清仓**：漏掉的那些其实已不在券商账上 → 加回去**会显著超出** mv
        故：仅当 `calc + missing <= mv × 1.05` 时才判为漏读并拒单。
        """
        try:
            mv = float(bal.get("market_value") or 0.0)
        except (TypeError, ValueError):
            mv = 0.0
        if not pos:
            return mv <= 1.0 and bal.get("cash") is not None
        for p in pos.values():
            if int(p.get("qty") or 0) <= 0:
                return False
        if mv > 1.0:
            calc = 0.0
            for code, p in pos.items():
                px = prices.get(self._guess_full_code(code))
                if px is None:
                    px = p.get("cost") or 0.0
                calc += p["qty"] * px
            if calc <= 0 or abs(calc - mv) / mv > 0.05:
                return False
            # 判据 4：消失的持仓按行情价加回去，看是否与资金栏市值自洽
            held = {self._guess_full_code(c) for c in pos}
            missing = 0.0
            for code, v in (old or {}).items():
                if v.get("qty", 0) > 0 and code not in held:
                    px = prices.get(code)
                    if px is None:
                        px = v.get("cost") or 0.0
                    missing += v["qty"] * px
            if missing > 0 and calc + missing <= mv * 1.05:
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
            calc = 0.0
            for code, p in pos.items():
                px = prices.get(self._guess_full_code(code))
                if px is None:
                    px = p.get("cost") or 0.0
                calc += p["qty"] * px
            held = {self._guess_full_code(c) for c in pos}
            missing, items = 0.0, []
            for code, v in (old or {}).items():
                if v.get("qty", 0) > 0 and code not in held:
                    px = prices.get(code) or v.get("cost") or 0.0
                    missing += v["qty"] * px
                    items.append("{} qty={} px={:.3f}".format(code, v["qty"], px))
            dev = abs(calc - mv) / mv * 100 if mv else 0.0
            print("[reconcile] ⚠️ 持仓校验未通过，诊断数字：")
            print("   读到 pos   =", pos)
            print("   资金栏 bal =", bal)
            print("   全池行情价 =", {k: prices.get(k) for k in
                                     ("510880.SH", "511010.SH", "518880.SH") if k in prices})
            print("   calc(行情价×数量) = {:,.2f}   mv(资金栏市值) = {:,.2f}   偏差 = {:.2f}%".format(
                calc, mv, dev))
            print("   本地消失的持仓 = " + ("; ".join(items) if items else "(无)"))
            print("   calc+missing = {:,.2f}   vs   mv×1.05 = {:,.2f}".format(
                calc + missing, mv * 1.05))
        except Exception as e:                       # 诊断本身绝不能影响主流程
            print("[reconcile] (诊断打印失败: {})".format(e))

    def fetch_position(self, validator=None, tries=4) -> dict:
        """回读持仓。

        OCR 单次拍摄会漏行/读串列，因此连拍多次，并以 validator 作为「这一帧够不够可信」
        的停止条件；validator 给定时返回首个通过校验的结果，否则返回行数最多的那一帧。
        """
        best = {}
        for _ in range(tries):
            self._switch("query")
            time.sleep(0.5)
            self._switch("position")
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
            self._switch("query")
            time.sleep(0.5)
            self._switch("today_order")
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
            new_pos[full] = {"qty": qty, "cost": round(cost, 4),
                             "peak": max(cost, old_peak)}

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

_BAN_WORDS = ("资产", "可用", "当日", "总资产", "股票市值", "资金", "证券代码",
              "证券名称", "操作", "成交", "委托", "撤", "市场", "代码", "入", "出",
              "合同编号", "委托时间", "交易市场")


def _to_float(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _clean_code(raw):
    code = str(raw).zfill(6)
    return code if code.startswith(("5", "6", "0", "1", "3")) else None


def parse_confirm_text(text) -> dict:
    """解析「委托确认」弹窗正文 → {code, name, price, qty, amount}。

    弹窗样本（富文本剥标签后）：
        证券代码：510880(红利ETF华泰柏瑞)
        买入价格：3.427 (卖一)
        买入数量：100
        预估金额：347.700
    """
    import re
    out = {}
    m = re.search(r"证券代码[：:]\s*(\d{6})(?:\s*[（(]([^）)]*)[）)])?", text or "")
    if m:
        out["code"] = m.group(1)
        if m.group(2):
            out["name"] = m.group(2).strip()
    m = re.search(r"(?:买入|卖出)价格[：:]\s*([\d.]+)", text or "")
    if m:
        out["price"] = _to_float(m.group(1))
    m = re.search(r"(?:买入|卖出)数量[：:]\s*([\d,]+)", text or "")
    if m:
        out["qty"] = int(m.group(1).replace(",", ""))
    m = re.search(r"预估金额[：:]\s*([\d,.]+)", text or "")
    if m:
        out["amount"] = _to_float(m.group(1))
    return out


def parse_position(text) -> dict:
    """OCR 持仓表 → {code: {qty, cost, price}}。

    持仓列序：证券代码 证券名称 股票余额 可用余额 冻结数量 成本价 市价 盈亏 ...
    取「股票余额」为 qty、「成本价」为 cost。
    """
    import re
    out = {}
    for ln in text.splitlines():
        m = re.search(r"\b\d{6}\b", ln)
        if not m or not re.search(r"[\u4e00-\u9fff]", ln):
            continue
        if any(b in ln for b in _BAN_WORDS):
            continue
        code = _clean_code(m.group(0))
        if not code:
            continue
        nums = re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", ln.split(code)[-1])
        # 持仓行至少要有「股票余额/可用/冻结/成本价」4 个数；数量必须为正。
        # OCR 抖动会产生 1~2 个数字的碎片行（实测出现过 qty=1/cost=0 的假持仓），
        # 这类行一旦被当成持仓，会让引擎严重误判仓位。
        if len(nums) < 4:
            continue
        qty = int(float(nums[0].replace(",", "")))
        if qty <= 0:
            continue
        out[code] = {
            "qty": qty,                                                # 股票余额
            "available": int(float(nums[1].replace(",", ""))),
            "frozen": int(float(nums[2].replace(",", ""))),
            "cost": _to_float(nums[3]),                                # 成本价
            "price": _to_float(nums[4]) if len(nums) > 4 else None,    # 市价
        }
    return out


def parse_trades(text) -> list:
    """OCR 当日委托/成交表 → [{code, name, side, status, qty, filled_qty, price, avg_price, deal_id}]。

    列序：委托时间 证券代码 证券名称 操作 状态 委托数量 成交数量 委托价格 成交均价 撤消数量 合同编号 交易市场
    """
    import re
    rows = []
    for ln in text.splitlines():
        m = re.search(r"\b\d{6}\b", ln)
        if not m or not re.search(r"[\u4e00-\u9fff]", ln):
            continue
        code = _clean_code(m.group(0))
        if not code:
            continue
        tail = ln.split(code, 1)[-1]
        nums = re.findall(r"\d[\d,]*(?:\.\d+)?", tail)
        nums_f = [_to_float(x) for x in nums]

        def g(i):
            return nums_f[i] if i < len(nums_f) else None

        if "全部成交" in ln or "已成" in ln:
            status = "已成"
        elif "部分成交" in ln:
            status = "部分成交"
        elif "已撤" in ln:
            status = "已撤"
        else:
            status = "已报"
        rows.append({
            "code": code,
            "name": (re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "",
                            re.split(r"买入|卖出", tail)[0])[:12] or None),
            "side": "买入" if "买入" in ln else ("卖出" if "卖出" in ln else None),
            "status": status,
            "qty": int(g(0)) if g(0) is not None else None,            # 委托数量
            "filled_qty": int(g(1)) if g(1) is not None else 0,        # 成交数量
            "price": g(2),                                             # 委托价格
            "avg_price": g(3),                                         # 成交均价
            "deal_id": str(int(g(5))) if g(5) is not None else None,   # 合同编号
        })
    return rows
