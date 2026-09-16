"""交易窗口控件树导出工具（排障用）。

用法（32 位 Python）：
    E:\\Python32\\python.exe tools/ths_dump_tree.py           # 自动连接交易窗口
    E:\\Python32\\python.exe tools/ths_dump_tree.py 0x20BFC   # 指定窗口句柄

输出：全部子控件(递归直接子窗口)到 ths_tree_<hwnd>.txt，并打印：
  · 买入/卖出页的关键控件（证券代码/价格/数量/提交按钮）
  · 交易窗口 owned 的顶层弹窗（委托确认框在这里，不在 GW_CHILD 子树里！）

背景见 docs/ths_trade_window.md。
"""
import sys
import ctypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import win32gui

u32 = ctypes.windll.user32
_TITLE = "网上股票交易系统"


def walk(hwnd, depth=0, maxdepth=12, lines=None, visited=None):
    if lines is None:
        lines = []
    if visited is None:
        visited = set()
    if depth > maxdepth or hwnd in visited:
        return lines
    visited.add(hwnd)
    child = u32.GetWindow(hwnd, 5)          # GW_CHILD
    while child:
        try:
            lines.append("{}{:>5} hwnd=0x{:X} id={:<12} vis={} cls={:<24} geo={} txt={!r}".format(
                "  " * depth, depth, child, u32.GetDlgCtrlID(child),
                u32.IsWindowVisible(child), win32gui.GetClassName(child),
                win32gui.GetWindowRect(child), win32gui.GetWindowText(child)))
        except Exception as e:
            lines.append("{}{} <err {}>".format("  " * depth, depth, e))
        walk(child, depth + 1, maxdepth, lines, visited)
        try:
            child = u32.GetWindow(child, 2)  # GW_HWNDNEXT
        except Exception:
            break
    return lines


def find_trade():
    found = []

    def cb(h, _):
        try:
            if win32gui.IsWindowVisible(h):
                t = (win32gui.GetWindowText(h) or "").strip()
                if t.startswith(_TITLE):
                    found.append(h)
        except Exception:
            pass
    win32gui.EnumWindows(cb, None)
    return found


def owned_dialogs(hwnd):
    out = []
    h = u32.GetTopWindow(0)
    n = 0
    while h and n < 600:
        try:
            if u32.GetWindow(h, 4) == hwnd and u32.IsWindowVisible(h):
                out.append(h)
        except Exception:
            pass
        h = u32.GetWindow(h, 2)
        n += 1
    return out


def main():
    if len(sys.argv) > 1 and sys.argv[1].lower().startswith("0x"):
        hwnd = int(sys.argv[1], 16)
    else:
        cands = find_trade()
        if not cands:
            print("未找到交易窗口（标题前缀 {!r}）。请先打开并登录同花顺交易/模拟客户端。".format(_TITLE))
            return 1
        hwnd = cands[0]

    print("交易窗口 hwnd=0x{:X} title={!r} geo={}".format(
        hwnd, win32gui.GetWindowText(hwnd), win32gui.GetWindowRect(hwnd)))

    lines = walk(hwnd)
    out = Path(__file__).resolve().parent.parent / "ths_tree_0x{:X}.txt".format(hwnd)
    out.write_text("\n".join(lines), encoding="utf-8")
    print("控件树 {} 行 -> {}".format(len(lines), out))

    KEYS = ("证券代码", "证券名称", "买入价格", "卖出价格", "买入数量", "卖出数量", "重填")
    print("=== 买卖页关键控件 ===")
    for ln in lines:
        if any(k in ln for k in KEYS) or ("cls=Edit" in ln) or ("id=1006" in ln):
            print(" ", ln.strip())

    print("=== 交易窗口 owned 顶层弹窗（委托确认/提示在这里）===")
    for h in owned_dialogs(hwnd):
        print("  0x{:X} cls={} geo={}".format(h, win32gui.GetClassName(h), win32gui.GetWindowRect(h)))
        for ln in walk(h, 0, 4):
            if "cls=Button" in ln or "cls=Static" in ln:
                print("     ", ln.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
