"""UIA 强制下单实测：对 510880 下小额买单，提交换股提交，并读当日委托确认。

用法（32 位 Python，同花顺需登录模拟会话且窗口可见）：
    E:\\Python32\\python.exe test_uia_order.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 把输出同时写入日志文件（管理员进程无可见控制台，便于事后读取）
_LOG_PATH = str(Path(__file__).resolve().parent / "uia_order_run.log")

from trader.ths_uia import UiaThsBroker
from trader.broker import Order
from dataprovider.store import DataStore


def main():
    b = UiaThsBroker(exe_path=r"D:\同花顺软件\同花顺\xiadan.exe").connect(retries=2)
    print("[OK] UIA 连接成功")

    df = DataStore().read("510880.SH")
    price = round(float(df["close"].iloc[-1]), 2)
    print("下单: 510880 红利ETF  买入 100 股 @ {:.2f}".format(price))

    order = Order("510880.SH", "buy", 100, price, reason="UIA强制下单实测")
    try:
        b.submit(order)
        print("--- submit ---")
        print("status:", order.status, "price:", order.filled_price, "qty:", order.filled_qty)
    except Exception as e:
        print("下单失败:", e)
        return 1

    # 等待委托上poir丰，读当日委托确认
    import time
    time.sleep(1.5)
    try:
        orders = b.fetch_today_orders()
        print("--- 当日委托 ---")
        print(orders)
    except Exception as e:
        print("读当日委托失败:", e)
    try:
        print("--- 账户资金 ---")
        print(b.fetch_balance())
    except Exception as e:
        print("读资金失败:", e)
    return 0


if __name__ == "__main__":
    import io, contextlib
    buf = io.StringIO()
    code = None
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            code = main()
        except Exception as e:
            print("未捕获异常:", e)
            code = 1
    msg = buf.getvalue()
    print(msg, end="")
    try:
        with open(_LOG_PATH, "w", encoding="utf-8") as f:
            f.write(msg)
    except Exception:
        pass
    raise SystemExit(code)