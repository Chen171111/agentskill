"""UIA 版同花顺券商：用 pywinauto UIA 后端 + OCR 直接操作同花顺交易窗口。

替代 easytrader 的 win32 实现（easytrader 无法适配新版同花顺的自绘 UI，
下单只弹价格浮框、不真正提交；本实现用 UIA 定位输入框/按钮并真实提交）。

依赖（32 位 Python）：pywinauto, pytesseract + tesseract(chi_sim), Pillow。

接口对齐 trader/broker.Broker：connect / submit / fetch_position /
fetch_balance / sync_fill / reconcile。
"""
import time
import ctypes
import logging
from threading import Lock

logging.disable(logging.CRITICAL)

import win32clipboard
import pytesseract
from PIL import ImageGrab
from pywinauto import Desktop
from pywinauto import keyboard as _kbd

from .broker import Broker, Order

pytesseract.pytesseract.tesseract_cmd = r"E:\Tesseract-OCR\tesseract.exe"

_TITLE = "同花顺"  # 主窗口标题前缀:如 "同花顺(9.60.61) - 自选股"
_MENUS = {
    "buy": "买入[F1]", "sell": "卖出[F2]", "withdraw": "撤单[F3]",
    "query": "查询[F4]",
    "position": "资金股票", "today_order": "当日委托", "today_trade": "当日成交",
}


def _set_clipboard(text):
    """把文本写入系统剪贴板。"""
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(str(text))
    finally:
        win32clipboard.CloseClipboard()


def _vk_of(ch):
    """把单个字符映射为虚拟键码。数字/字母的 VK 等于 ASCII;其余走小数/常用键。"""
    if '0' <= ch <= '9' or 'a' <= ch.lower() <= 'z':
        return ord(ch.lower())
    return {'.': 0xBE, '-': 0xBD, ' ': 0x20, '\t': 0x09}.get(ch)


class UiaThsBroker(Broker):
    """UIA 操控同花顺（模拟盘）下单/查询。"""

    def __init__(self, exe_path=None):
        self._win = None
        self._lock = Lock()
        self._exe_path = exe_path
        self._connected = False

    # ---------- 连接 ----------
    def connect(self, exe_path=None, retries=2):
        self._exe_path = exe_path or self._exe_path
        last_err = None
        for _ in range(retries + 1):
            try:
                for w in Desktop(backend="uia").windows():
                    wt = (w.window_text() or "")
                    if wt.startswith(_TITLE) and "自选" not in wt[:4]:
                        self._win = w
                        w.set_focus()
                        self._connected = True
                        return self
                last_err = RuntimeError("未找到交易窗口（请启动同花顺并登录交易/模拟）")
            except Exception as e:
                last_err = e
            time.sleep(1)
        raise RuntimeError("同花顺连接失败：{}".format(last_err))

    # ---------- 基础 UI 操作 ----------
    def _win_safe(self):
        if not self._win:
            raise RuntimeError("ThsBroker 未连接")
        return self._win

    def _click_menu(self, text):
        dl = self._win_safe()
        for it in dl.descendants(control_type="TreeItem"):
            try:
                if (it.window_text() or "").startswith(text):
                    it.click_input()
                    time.sleep(0.4)
                    return True
            except Exception:
                continue
        return False

    def _edits(self):
        """当前页的 Edit 控件（按纵向位置排序）。"""
        dl = self._win_safe()
        out = []
        for c in dl.descendants(control_type="Edit"):
            try:
                if c.rectangle().width() > 20:
                    out.append(c)
            except Exception:
                pass
        out.sort(key=lambda c: c.rectangle().top)
        return out

    def _button(self, text):
        dl = self._win_safe()
        for c in dl.descendants(control_type="Button"):
            try:
                if (c.window_text() or "").strip() == text and c.is_enabled():
                    return c
            except Exception:
                continue
        return None

    def _type_keys(self, keys):
        """模拟按键。^a=全选, '+v'=Ctrl+V, 其余逐字符 keybd_event。"""
        try:
            if keys == "^a":  # Ctrl+A 全选
                ctypes.windll.user32.keybd_event(0x11, 0, 0, 0)  # Ctrl down
                ctypes.windll.user32.keybd_event(0x41, 0, 0, 0)  # A down
                ctypes.windll.user32.keybd_event(0x41, 0, 2, 0)  # A up
                ctypes.windll.user32.keybd_event(0x11, 0, 2, 0)  # Ctrl up
            elif keys in ("^v", "+v", "ctrl+v"):
                ctypes.windll.user32.keybd_event(0x11, 0, 0, 0)  # Ctrl down
                ctypes.windll.user32.keybd_event(0x56, 0, 0, 0)  # V down
                ctypes.windll.user32.keybd_event(0x56, 0, 2, 0)  # V up
                ctypes.windll.user32.keybd_event(0x11, 0, 2, 0)  # Ctrl up
            else:
                for ch in str(keys):
                    vk = _vk_of(ch)
                    if vk is not None:
                        ctypes.windll.user32.keybd_event(vk, ord(ch), 0, 0)
                        ctypes.windll.user32.keybd_event(vk, ord(ch), 2, 0)
            time.sleep(0.15)
        except Exception as e:
            raise RuntimeError("键盘输入失败: {}".format(e))

    def _clear_edit(self, ctrl):
        """聚焦编辑框并 Ctrl+A 全选。"""
        try:
            ctrl.set_focus()
            time.sleep(0.25)
        except Exception:
            pass
        self._type_keys("^a")

    def _fill_edit(self, ctrl, text):
        """向 Edit 填值:先 Ctrl+A 清空,再剪贴板粘贴整串(避免逐字符丢字)。"""
        # 确保交易窗口在前台(键盘输入需要焦点)
        try:
            self._win_safe().set_focus()
            time.sleep(0.2)
        except Exception:
            pass
        try:
            ctrl.set_focus()
            time.sleep(0.2)
        except Exception:
            pass
        self._clear_edit(ctrl)
        _set_clipboard(str(text))
        self._type_keys("^v")

    def _read_text(self):
        """截图 + OCR 当前交易窗口内容。"""
        dl = self._win_safe()
        try:
            dl.set_focus()
            time.sleep(0.5)
            r = dl.rectangle()
        except Exception:
            return ""
        time.sleep(0.5)
        try:
            img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom))
            return pytesseract.image_to_string(img, lang="chi_sim+eng")
        except Exception:
            return ""

    def _switch(self, key):
        top = {"buy": "买入[F1]", "sell": "卖出[F2]", "withdraw": "撤单[F3]",
               "query": "查询[F4]"}.get(key)
        if top:
            self._click_menu(top)
        sub = _MENUS.get(key)
        if sub and sub not in ("买入[F1]", "卖出[F2]", "撤单[F3]", "查询[F4]"):
            self._click_menu(sub)
        time.sleep(0.5)

    # ---------- 订单提交 ----------
    def _submit_page(self, order, page_top):
        self._switch(page_top)
        time.sleep(0.4)
        edits = self._edits()
        if len(edits) < 3:
            raise RuntimeError("未找到下单输入框（{} 个）".format(len(edits)))
        code = order.code.split(".")[0]
        self._fill_edit(edits[0], code)
        self._fill_edit(edits[1], str(round(order.price, 3)))
        self._fill_edit(edits[2], str(int(order.qty)))
        time.sleep(0.3)
        btn = self._button("买入" if page_top == "buy" else "卖出")
        if not btn:
            raise RuntimeError("未找到提交按钮")
        btn.click_input()
        time.sleep(0.6)
        # 处理可能的确认/提示对话框
        self._accept_dialogs()
        order.status = "filled"
        order.filled_price = order.price
        order.filled_qty = int(order.qty)
        order.fee = 0.0
        return order

    def _accept_dialogs(self):
        """点掉确认/提示对话框的按钮:不做窗口标题过滤(自绘弹窗标题不可预测),
        直接扫所有顶层窗口里以"是/确定/确认/OK"开头的按钮并点击。"""
        for _ in range(6):
            clicked = False
            for w in Desktop(backend="uia").windows():
                for c in w.descendants(control_type="Button"):
                    btxt = (c.window_text() or "").strip()
                    if not btxt:
                        continue
                    if btxt.startswith("是") or btxt.startswith("确定") \
                            or btxt.startswith("确认") or btxt.startswith("OK") \
                            or btxt.startswith("Yes"):
                        try:
                            c.click_input()
                            time.sleep(0.4)
                            clicked = True
                        except Exception:
                            pass
                        break
            if not clicked:
                break

    def submit(self, order: Order) -> Order:
        with self._lock:
            if order.side in ("buy", "买入"):
                self._submit_page(order, "buy")
            else:
                self._submit_page(order, "sell")
            return order

    # ---------- 查询 ----------
    def fetch_balance(self) -> dict:
        self._switch("query")
        txt = self._read_text()
        out = {
            "cash": _num_after(txt, ("可用金额", "可用余额", "可用:")),
            "market_value": 0.0,
            "total": _num_after(txt, ("资产:", "总资产:", "资产")),
        }
        if out.get("total") is None and out.get("cash") is not None:
            out["total"] = out["cash"]
        return out

    def fetch_position(self) -> dict:
        self._switch("query")
        self._click_menu("资金股票")
        time.sleep(0.6)
        txt = self._read_text()
        return parse_position(txt)

    def fetch_today_orders(self) -> list:
        self._switch("query")
        self._click_menu("当日委托")
        time.sleep(0.6)
        txt = self._read_text()
        return parse_trades(txt)

    def sync_fill(self, order: Order):
        """回读当日委托/成交，用真实价/量修正订单。"""
        try:
            rows = self.fetch_today_orders()
            code = order.code.split(".")[0]
            for r in rows:
                if (r.get("code") or "").zfill(6) == code:
                    if r.get("price"):
                        order.filled_price = r["price"]
                    if r.get("qty"):
                        order.filled_qty = r["qty"]
                    return
        except Exception:
            pass

    def reconcile(self, account) -> bool:
        pos = self.fetch_position()
        bal = self.fetch_balance()
        if not pos and not bal:
            return False
        old = getattr(account, "positions", {}) or {}
        new_pos = {}
        for code, p in pos.items():
            full = code
            old_peak = old.get(full, {}).get("peak", 0.0) if old.get(full) else 0.0
            new_pos[full] = {"qty": p["qty"], "cost": p["cost"],
                             "peak": max(p.get("price") or p["cost"] or 0.0, old_peak)}
        account.positions = new_pos
        if "cash" in bal and bal["cash"] is not None:
            account.cash = bal["cash"]
        return True


# ---------- OCR 文本解析（同花顺查询表格） ----------

# 顶栏金额行 / 表头等噪声词，出现即视为非持仓、非委托行
_BAN_WORDS = ("资产", "可用", "当日", "总资产", "股票市值", "资金", "证券代码",
              "证券名称", "操作", "成交", "委托", "撤", "市场", "代码", "入", "出")


def _num_after(text, labels):
    """取关键词之后第一个数字。"""
    import re
    for label in labels:
        idx = text.find(label)
        if idx >= 0:
            seg = text[idx: idx + 80]
            m = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", seg)
            if m:
                return _to_float(m.group(0))
    return None


def _to_float(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _clean_code(raw):
    code = str(raw).zfill(6)
    return code if code.startswith(("5", "6", "0", "1", "3")) else None


def parse_position(text) -> dict:
    """OCR 持仓文本 → {code: {qty, cost, price}}。

    仅识别"含 6 位证券代码 + 证券简称(汉字)且不含顶栏金额词"的行，避免把
    "资产 203116.96"等金额行误判为持仓。
    """
    import re
    out = {}
    for ln in text.splitlines():
        if not re.search(r"\b\d{6}\b", ln):
            continue
        if not re.search(r"[\u4e00-\u9fff]", ln):
            continue  # 无中文名，排除纯数字行
        if any(b in ln for b in _BAN_WORDS):
            continue  # 顶栏/表头行
        code = _clean_code(re.search(r"\b\d{6}\b", ln).group(0))
        if not code:
            continue
        # 该行可能跨普通文本，取证券代码后的若干数字（市价/持仓/可用/成本/市值）
        seg = ln.split(code)[-1]
        nums = re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", seg)
        if nums:
            out[code] = {
                "qty": int(float(nums[0].replace(",", ""))),
                "cost": _to_float(nums[1]) if len(nums) > 1 else 0.0,
                "price": _to_float(nums[0]),
            }
    return out


def parse_trades(text) -> list:
    """OCR 当日委托/成交 → [{code, price, qty, status}]。"""
    import re
    rows = []
    for ln in text.splitlines():
        if not re.search(r"\b\d{6}\b", ln):
            continue
        if any(b in ln for b in _BAN_WORDS):
            continue
        if not re.search(r"[\u4e00-\u9fff]", ln):
            # 委托行一般含证券名；纯数字行多为金额噪声
            continue
        code = _clean_code(re.search(r"\b\d{6}\b", ln).group(0))
        if not code:
            continue
        nums = re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", ln.split(code)[-1])
        rows.append({
            "code": code,
            "price": _to_float(nums[0]) if nums else None,
            "qty": int(float(nums[1].replace(",", ""))) if len(nums) > 1 else None,
            "status": "已成" if ("已成" in ln or "成交" in ln) else "已报",
        })
    return rows