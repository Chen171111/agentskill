# -*- coding: utf-8 -*-
"""只读诊断：同花顺「资金股票」页的持仓表到底被读成了什么。

为什么需要（2026-09-21 事故）
----------------------------
14:50 自动交易被拒单：OCR 只读到 1 只持仓（510880 1600 份，市值 5,342），
但资金栏「股票市值」= 121,595.20，偏差 95.61%。当日上午用户手工买入了若干
**个股**（不在策略池内），怀疑 OCR 漏读。

本脚本**只读**：不提交任何委托、不写本地账本（不碰 state/trading.db）。
它把下列原始证据落盘到 state/diag_position_<时间戳>.txt：
  1. 所有 CVirtualGridCtrl 的句柄 / 可见性 / 矩形 / 层级，看是否截错表格；
  2. `_read_grid_text()` 的 **OCR 原文**（最关键——能直接看出是漏读还是解析丢行）；
  3. `parse_position()` 的解析结果；
  4. 资金栏各字段。

用法（必须 32 位 Python）:
    E:\\Python32\\python.exe tools/diag_ths_position.py
    E:\\Python32\\python.exe tools/diag_ths_position.py --shots 3   # 连拍次数
"""
import argparse
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "state")
SHOT_DIR = os.path.join(OUT_DIR, "diag_shots")


def _log(fh, *a):
    line = " ".join(str(x) for x in a)
    print(line)
    fh.write(line + "\n")


def dump_grids(broker, fh):
    """枚举所有 CVirtualGridCtrl：可见性 + 矩形 + 自身文本，判断表格选取是否正确。"""
    import win32gui
    from trader import ths_uia as T

    _log(fh, "\n===== [A] 全部 CVirtualGridCtrl 实例 =====")
    hs = broker._find_class("CVirtualGridCtrl")
    _log(fh, "共找到 {} 个".format(len(hs)))
    for h in hs:
        try:
            vis = bool(T.u32.IsWindowVisible(h))
            rc = win32gui.GetWindowRect(h)
            w, ht = rc[2] - rc[0], rc[3] - rc[1]
            par = win32gui.GetParent(h)
            ptitle = (win32gui.GetWindowText(par) or "")[:40]
            txt = (win32gui.GetWindowText(h) or "")[:60]
            _log(fh, "  hwnd={} vis={} rect={} size={}x{} parent='{}' text='{}'".format(
                h, vis, rc, w, ht, ptitle, txt))
        except Exception as e:
            _log(fh, "  hwnd={} 读取失败: {}".format(h, e))

    rc = broker._grid_rect()
    _log(fh, "→ _grid_rect() 选中: {}".format(rc))


def dump_page_state(broker, fh):
    """当前页标题 / 可见页标签，用来确认「资金股票」页确实是当前显示页。"""
    from trader import ths_uia as T
    try:
        _log(fh, "\n===== [B] 页面状态 =====")
        _log(fh, "  当前页标签 = {!r}".format(broker._page_label()))
        hwnd = broker._active_page()
        _log(fh, "  _active_page() hwnd = {}".format(hwnd))
        if hwnd:
            _log(fh, "  标题控件文本 = {!r}".format(
                (T.win32gui.GetWindowText(hwnd) or "")[:80]))
    except Exception as e:
        _log(fh, "  页面状态读取失败: {}".format(e))


def dump_shot(broker, fh, idx, save_png=True):
    """单次拍摄：OCR 原文 + 解析结果。"""
    import pytesseract
    from PIL import ImageGrab
    from trader.ths_uia import parse_position

    _log(fh, "\n===== [C{}] 第 {} 次拍摄 =====".format(idx, idx))
    broker._switch("query")
    time.sleep(0.5)
    broker._switch("position")
    time.sleep(1.0)

    rc = broker._grid_rect()
    _log(fh, "  grid_rect = {}".format(rc))
    if not rc:
        _log(fh, "  ✗ 未取到表格矩形")
        return "", {}

    broker._raise(keep=True)
    try:
        time.sleep(0.3)
        img = ImageGrab.grab(bbox=rc)
    finally:
        broker._unraise()

    if save_png:
        try:
            os.makedirs(SHOT_DIR, exist_ok=True)
            p = os.path.join(SHOT_DIR, "pos_{}_{}.png".format(
                datetime.now().strftime("%Y%m%d_%H%M%S"), idx))
            img.save(p)
            _log(fh, "  截图已存: {}".format(p))
        except Exception as e:
            _log(fh, "  截图保存失败: {}".format(e))

    img4 = img.resize((img.width * 4, img.height * 4))
    txt = ""
    try:
        txt = pytesseract.image_to_string(img4, lang="chi_sim+eng", config="--psm 6")
    except Exception as e:
        _log(fh, "  OCR 失败: {}".format(e))

    _log(fh, "  --- OCR 原文（逐行，带行号）---")
    for i, ln in enumerate((txt or "").splitlines()):
        if ln.strip():
            _log(fh, "    [{:02d}] {!r}".format(i, ln))
    pos = parse_position(txt)
    _log(fh, "  --- parse_position 结果 ---")
    _log(fh, "    {}".format(pos))
    return txt, pos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", type=int, default=3, help="连拍次数")
    ap.add_argument("--no-png", action="store_true", help="不保存截图")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(OUT_DIR, "diag_position_{}.txt".format(stamp))

    fh = open(out, "w", encoding="utf-8")
    _log(fh, "同花顺持仓读取诊断  {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    _log(fh, "输出文件: {}".format(out))

    from trader.ths_uia import UiaThsBroker
    broker = UiaThsBroker()
    try:
        broker.connect()
    except Exception as e:
        _log(fh, "✗ 连接失败：{}".format(e))
        _log(fh, "  （同花顺交易窗口未打开/未登录。请先手工打开并登录模拟盘，再重跑本脚本）")
        fh.close()
        print("\n诊断输出: {}".format(out))
        return 2

    _log(fh, "✓ 已连接交易窗口 hwnd={}".format(broker._hwnd))
    dump_page_state(broker, fh)
    dump_grids(broker, fh)

    _log(fh, "\n===== [D] 资金栏 =====")
    try:
        bal = broker.fetch_balance()
        _log(fh, "  {}".format(bal))
    except Exception as e:
        bal = {}
        _log(fh, "  ✗ 读取失败: {}".format(e))

    results = []
    for i in range(1, max(1, args.shots) + 1):
        try:
            _, pos = dump_shot(broker, fh, i, save_png=not args.no_png)
            results.append(pos)
        except Exception as e:
            _log(fh, "  第 {} 次拍摄异常: {}".format(i, e))
        time.sleep(0.4)

    _log(fh, "\n===== [E] 结论数字 =====")
    _log(fh, "  资金栏股票市值 = {}".format(bal.get("market_value")))
    for i, pos in enumerate(results, 1):
        _log(fh, "  第 {} 次读到 {} 只: {}".format(i, len(pos), list(pos.keys())))
    best = max(results, key=len) if results else {}
    _log(fh, "  最多一次读到 {} 只: {}".format(len(best), best))
    fh.write("\n")
    fh.close()
    print("\n诊断输出: {}".format(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
