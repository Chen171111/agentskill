# -*- coding: utf-8 -*-
"""只读诊断 2：资金栏字段溯源 + 当日成交/委托明细。

目的（2026-09-21）
----------------
持仓表汇总市值 95,944.000，但资金栏 _BAL_MKTVAL(1014) 读出 121,595.20，
差 25,651.20 —— 差额来源不明，必须先定性，否则「修正持仓解析」之后
`_pos_ok` 判据 3（行情价×数量 vs 资金栏市值）依旧会失败。

本脚本**只读**：不提交委托、不写本地账本。
输出 state/diag_balance_<时间戳>.txt，包含：
  [A] 交易窗口内所有「非空 Static」控件：hwnd / id / 所属页面 / 文本
      → 直接看 1012~1017 的真实语义与是否有跨页同名控件
  [B] 当前页 Static 文本（资金股票页的顶栏）
  [C] 当日成交页 OCR 原文 + 解析
  [D] 当日委托页 OCR 原文 + 解析

用法:
    E:\\Python32\\python.exe tools/diag_ths_balance.py
"""
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "state")
SHOT_DIR = os.path.join(OUT_DIR, "diag_shots")

BAL_IDS = {1012: "资金余额", 1013: "冻结金额", 1014: "股票市值",
           1015: "总资产", 1016: "可用金额", 1017: "可取金额"}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(OUT_DIR, "diag_balance_{}.txt".format(stamp))
    fh = open(out, "w", encoding="utf-8")

    def L(*a):
        s = " ".join(str(x) for x in a)
        print(s)
        fh.write(s + "\n")

    import win32gui
    from trader.ths_uia import UiaThsBroker, parse_trades

    L("资金栏/成交明细诊断  {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    broker = UiaThsBroker()
    try:
        broker.connect()
    except Exception as e:
        L("✗ 连接失败：{}".format(e))
        fh.close()
        print("诊断输出: {}".format(out))
        return 2
    L("✓ 已连接 hwnd={}\n".format(broker._hwnd))

    # ---------- [A] 全部非空 Static ----------
    L("===== [A] 窗口内非空 Static 控件（id / 页面 / 文本）=====")
    rows = []

    def rec(h, d=0):
        if d > 10:
            return
        try:
            cls = win32gui.GetClassName(h) or ""
        except Exception:
            cls = ""
        if cls == "Static":
            t = (win32gui.GetWindowText(h) or "").strip()
            if t:
                cid = win32gui.GetDlgCtrlID(h)
                page = broker._page_label_of(h)
                vis = bool(__import__("win32gui").IsWindowVisible(h))
                rows.append((cid, page, vis, t, h))
        try:
            ch = win32gui.GetWindow(h, 5)
        except Exception:
            return
        while ch:
            rec(ch, d + 1)
            try:
                ch = win32gui.GetWindow(ch, 2)   # GW_HWNDNEXT
            except Exception:
                break

    rec(broker._win())
    L("共 {} 个".format(len(rows)))
    L("--- 资金栏候选 ID ---")
    for cid, page, vis, t, h in rows:
        if cid in BAL_IDS:
            L("  id={} ({}?) 页面={!r} vis={} 文本={!r}".format(
                cid, BAL_IDS[cid], page, vis, t))
    L("--- 其它含数字文本的 Static（前 80 条）---")
    seen = 0
    for cid, page, vis, t, h in rows:
        if cid in BAL_IDS:
            continue
        if any(ch.isdigit() for ch in t):
            L("  id={} 页面={!r} vis={} 文本={!r}".format(cid, page, vis, t))
            seen += 1
            if seen >= 80:
                break

    # ---------- [B] 资金栏实读 ----------
    L("\n===== [B] fetch_balance() 实读 =====")
    try:
        bal = broker.fetch_balance()
        L("  {}".format(bal))
    except Exception as e:
        bal = {}
        L("  ✗ {}".format(e))

    # ---------- [C][D] 成交 / 委托 OCR ----------
    def dump_page(key, title):
        L("\n===== {} OCR =====".format(title))
        broker._switch("query")
        time.sleep(0.5)
        broker._switch(key)
        time.sleep(1.2)
        L("  当前页标签 = {!r}".format(broker._page_label()))
        rc = broker._grid_rect()
        L("  grid_rect = {}".format(rc))
        txt = broker._read_grid_text()
        for i, ln in enumerate((txt or "").splitlines()):
            if ln.strip():
                L("    [{:02d}] {!r}".format(i, ln))
        return txt

    try:
        t_trade = dump_page("today_trade", "[C] 当日成交")
        L("  parse_trades → {}".format(parse_trades(t_trade)))
    except Exception as e:
        L("  ✗ 当日成交读取失败: {}".format(e))

    try:
        t_order = dump_page("today_order", "[D] 当日委托")
        L("  parse_trades → {}".format(parse_trades(t_order)))
    except Exception as e:
        L("  ✗ 当日委托读取失败: {}".format(e))

    fh.close()
    print("\n诊断输出: {}".format(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
