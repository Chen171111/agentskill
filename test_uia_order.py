"""同花顺强制下单实测（UIA/win32 消息级）。

用法（32 位 Python，同花顺已登录模拟会话，交易窗口『网上股票交易系统5.0』已打开）：
    E:\\Python32\\python.exe test_uia_order.py            # 真实提交一笔小额买单
    E:\\Python32\\python.exe test_uia_order.py --dry      # 只填表+截图，不提交

输出同时写入 uia_order_run.log（便于管理员/无控制台环境事后查看）。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_HERE = Path(__file__).resolve().parent
_LOG_PATH = str(_HERE / "uia_order_run.log")

from trader.ths_uia import UiaThsBroker, _ID_CODE, _ID_NAME, _ID_PRICE, _ID_QTY, _ID_MAXQTY
from trader.broker import Order

CODE = "510880.SH"
QTY = 100
DRY = "--dry" in sys.argv


def _price():
    """取标的参考价：优先本地数据，失败则用同花顺自动带出的最新价（填表后由客户端决定）。"""
    try:
        from dataprovider.store import DataStore
        df = DataStore().read(CODE)
        return round(float(df["close"].iloc[-1]), 2)
    except Exception as e:
        print("[warn] 本地行情读取失败({})，价格交由客户端自动带出".format(e))
        return None


def _snap(broker, tag):
    """截交易窗口，留档验证。"""
    try:
        import ctypes
        import win32gui
        from PIL import ImageGrab
        u32 = ctypes.windll.user32
        h = broker._hwnd
        rc = win32gui.GetWindowRect(h)
        u32.SetWindowPos(h, -1, 0, 0, 0, 0, 0x0001 | 0x0002)
        time.sleep(0.3)
        u32.SetWindowPos(h, -2, 0, 0, 0, 0, 0x0001 | 0x0002)
        time.sleep(0.4)
        p = _HERE / "uia_order_{}.png".format(tag)
        ImageGrab.grab(bbox=rc).save(str(p))
        print("截图:", p)
    except Exception as e:
        print("[warn] 截图失败:", e)


def main():
    b = UiaThsBroker(exe_path=r"D:\同花顺软件\同花顺\xiadan.exe").connect(retries=2)
    print("[OK] 已连接交易窗口『网上股票交易系统5.0』 hwnd=0x{:X}".format(b._hwnd))
    print("     当前页面:", b._page_label())

    price = _price()
    print("下单: {}  买入 {} 股 @ {}".format(CODE, QTY, price if price else "客户端自动价"))

    # ---- 资金（按控件 ID 直读，不用 OCR）----
    bal = b.fetch_balance()
    print("--- 提交前资金 ---")
    print(bal)

    order = Order(CODE, "buy", QTY, price or 0.0, reason="强制下单实测")

    if DRY:
        # 只填表，不提交
        b._switch("buy")
        time.sleep(0.4)
        info = b._fill_order_form(order, page="买入股票")
        print("--- dry-run 填表结果 ---")
        print("当前页面:", b._page_label())
        print("证券名称:", info["name"])
        print("代码框:", repr(win32gui_text(b, _ID_CODE, "买入股票")))
        print("数量框:", repr(win32gui_text(b, _ID_QTY, "买入股票")))
        _snap(b, "dry")
        print("未提交（--dry）。确认无误后去掉 --dry 重跑。")
        return 0

    try:
        b.submit(order)
        print("--- submit ---")
        print("status:", order.status, "price:", order.filled_price)
    except Exception as e:
        print("下单失败:", e)
        _snap(b, "fail")
        return 1

    _snap(b, "submitted")

    time.sleep(1.5)
    print("--- 提交后资金（可用金额应减少约 金额+费用）---")
    print(b.fetch_balance())

    print("--- 当日委托（OCR，以客户端界面为准）---")
    try:
        rows = b.fetch_today_orders()
        print(rows)
        hit = [r for r in rows if r.get("code") == CODE.split(".")[0]]
        print("命中本单:", hit if hit else "未读到（可人工查看客户端『当日委托』）")
    except Exception as e:
        print("读当日委托失败:", e)
    return 0


def win32gui_text(b, cid, page=None):
    import win32gui
    try:
        return win32gui.GetWindowText(b._ctrl(cid, page=page))
    except Exception as e:
        return "<err {}>".format(e)


if __name__ == "__main__":
    import io
    import contextlib
    buf = io.StringIO()
    code = None
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            code = main()
        except Exception as e:
            import traceback
            traceback.print_exc()
            code = 1
    msg = buf.getvalue()
    print(msg, end="")
    try:
        Path(_LOG_PATH).write_text(msg, encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(code)
