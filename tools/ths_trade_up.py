# -*- coding: utf-8 -*-
"""确保「模拟交易盘」处于可交易状态（只显示窗口，**绝不下单、绝不改持仓**）。

背景（2026-11-16）
------------------
同花顺经典版开机自启后，交易客户端 `xiadan.exe` 的窗口**可能处于隐藏态**
（`IsWindowVisible=False`）。实测 2026-11-16 08:38:50 的自启动日志：

    trade window: 网上股票交易系统5.0 | visible=False iconic=False

而 `trader/ths_uia.py` 的 `_find_trade_hwnd()` 只认**可见**窗口 →
14:50 定时任务连续 4 次 `connect` 失败（每次 120 秒，空转约 6 分钟后放弃），
当日白丢一个交易日。窗口明明在、也登录着，只是"没人把它显示出来"。

本工具的职责就是把这一层补上：**查找（含隐藏）→ 显示/还原 → 确认控件就绪**。
它不启动进程（进程拉起由 `tools/ths_autostart.ps1` 在计划任务环境完成，
本 CLI 会话内启动的进程会被 Job Object 回收），也不做任何账户操作。

状态分类
--------
| state | 含义 | 处置 |
|---|---|---|
| READY | 窗口可见 + 交易控件齐全 | 直接可交易 |
| HIDDEN | 窗口存在但不可见 | `--ensure` 会 `ShowWindow(SW_SHOW)` |
| ICONIC | 窗口已最小化 | `--ensure` 会 `ShowWindow(SW_RESTORE)` |
| NOT_LOGGED_IN | 窗口在、但无交易控件 | 需人工登录模拟盘（不自动登录） |
| NO_WINDOW | 进程在、但无该标题窗口 | 客户端还在启动/卡在启动页 |
| MISSING_PROC | `xiadan.exe` 不在 | 需打开同花顺（或等计划任务） |

用法
----
    E:\\Python32\\python.exe tools\\ths_trade_up.py            # 只诊断，不动窗口
    E:\\Python32\\python.exe tools\\ths_trade_up.py --ensure   # 诊断 + 自愈
    E:\\Python32\\python.exe tools\\ths_trade_up.py --ensure --wait 120 --json

退出码：0 = READY；1 = 未就绪（含进程缺失/未登录）。
"""
import ctypes
import ctypes.wintypes as wt
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import win32con
import win32gui
import win32process

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
STATE = ROOT / "state"
LOG = STATE / "ths_trade_up.log"

# 与 trader/ths_uia.py 保持一致（那边是唯一判据来源，改动必须同步）
TRADE_TITLE = "网上股票交易系统5.0"
TITLE_PREFIX = "网上股票交易系统"

# 交易控件 ID（来自 ths_uia 常量）：代码框 1032 / 数量框 1034 / 总资产 1015 / 可用 1016
_READY_CTRL_IDS = (1032, 1034, 1015, 1016)
# 只认这几个进程名判断「交易客户端在不在」
_TRADE_PROCS = ("xiadan.exe", "xiadan-plus.exe")

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# 探测过程中的异常计数。**不许静默吞**（本项目 selftest 明令禁止 `except: pass`），
# 这里统一按类型计数 + 首次出现打印，既留痕又不至于每个窗口刷一行。
_DIAG = {"enum_err": 0, "proc_err": 0, "ctl_err": 0, "win_err": 0}


def _note(key, exc):
    _DIAG[key] = _DIAG.get(key, 0) + 1
    if _DIAG[key] == 1:
        sys.stderr.write("[trade_up] %s 异常（%r）—— 同类后续不再重复打印\n" % (key, exc))


def _log(msg):
    """写日志到 state/ths_trade_up.log；失败不抛（日志坏掉不能拖垮巡检）。"""
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        line = "[{}] {}\n".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
        with open(str(LOG), "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as exc:
        sys.stderr.write("[trade_up] 日志写入失败：%r\n" % (exc,))


def _exe_name(pid):
    h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if not _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value.rsplit("\\", 1)[-1]
    except Exception:
        return ""
    finally:
        _k32.CloseHandle(h)


def trade_proc_pids():
    """当前在跑的交易客户端 pid 列表（不依赖 psutil —— Python32 没装）。"""
    pids = []
    try:
        for pid in win32process.EnumProcesses():
            name = _exe_name(pid).lower()
            if name in _TRADE_PROCS:
                pids.append(pid)
    except Exception as exc:
        _note("proc_err", exc)
    return pids


def _has_trade_controls(hwnd, max_nodes=6000):
    """窗口子树里是否已有交易控件（未登录时窗口框架在、控件不在）。

    ⚠️ 必须从 hwnd 本身入栈再逐层展开 —— 只把 `GetWindow(hwnd, GW_CHILD)`
    当作起点会**只走第一个子窗口那条链，漏掉所有同级兄弟**（2026-11-16 实测：
    这样写会把已登录的窗口误判成「未登录」，因为它要的 1015/1016 在兄弟链上）。
    """
    stack = [(hwnd, 0)]
    n = 0
    found = set()
    while stack and n < max_nodes:
        cur, d = stack.pop()
        n += 1
        if not cur or d > 12:
            continue
        try:
            if cur != hwnd:
                cid = win32gui.GetDlgCtrlID(cur)
                if cid in _READY_CTRL_IDS:
                    found.add(cid)
                    if found == set(_READY_CTRL_IDS):
                        return True
        except Exception as exc:
            _note("ctl_err", exc)
        child = win32gui.GetWindow(cur, win32con.GW_CHILD)
        while child:
            stack.append((child, d + 1))
            child = win32gui.GetWindow(child, win32con.GW_HWNDNEXT)
    return False


def _broker_usable():
    """权威判据：用交易链路自己的连接路径确认账户可用（**只读，不下单**）。

    2026-11-17 实测（两次时间点都有反证）：
      · 15:27:23 `main.py ths-check` → `[OK] 连接成功`，读到总资产/可用/持仓；
      · 同一时刻 `ensure_trade_window()` 报 `NOT_LOGGED_IN`；
      · 对 hwnd=198672 做只读 dump：274 个节点、129 个 Static、70 个 Button、
        3 个 `CVirtualGridCtrl`（账户/持仓表格在），但 `_READY_CTRL_IDS` 只命中
        `1015/1016`，**缺 1032/1034**。

    ⇒ 界面已登录可用，是「四个控件 ID 必须全到齐」这个判据在客户端布局变化后
    **过时**，产生了假阴性；而预检是 fail-closed 的，于是一个可交易的账户被判成
    「未登录」并直接拒绝交易。

    因此控件 ID 只保留为**快速路径**：不命中时用本函数复核，判据以「交易链路
    实际能不能读到账户」为准。返回 (ok, 说明)。
    """
    try:
        from trader.ths_uia import UiaThsBroker
    except ImportError:
        return False, "无法导入 trader.ths_uia.UiaThsBroker"
    try:
        broker = UiaThsBroker()
        broker.connect()
        bal = broker.fetch_balance() or {}
        import math
        raw_cash = bal.get("cash")
        if isinstance(raw_cash, bool):
            return False, "UIA 可用资金字段不是有效金额"
        cash = float(raw_cash)
        if not math.isfinite(cash) or cash < 0:
            return False, "UIA 可用资金字段缺失或无效"
        return True, "UIA 连接成功，可用资金字段已通过有限非负校验"
    except Exception as exc:                       # 复核失败也是结论，必须回报
        return False, "UIA 复核失败: {!r}".format(exc)


def find_trade_windows():
    """枚举**全部**标题匹配的顶层窗口（不看可见性）。

    返回 [{'hwnd','title','visible','iconic','pid','controls'}, ...]
    ⚠️ 回调必须 return True —— 否则 EnumWindows 会在第一个分支处提前终止
    （2026-10-23 `probe_env.py` 就栽在这上面）。
    """
    out = []

    def cb(h, _):
        try:
            t = (win32gui.GetWindowText(h) or "").strip()
            if t == TRADE_TITLE or t.startswith(TITLE_PREFIX):
                _, pid = win32process.GetWindowThreadProcessId(h)
                out.append({
                    "hwnd": h,
                    "title": t,
                    "visible": bool(win32gui.IsWindowVisible(h)),
                    "iconic": bool(win32gui.IsIconic(h)),
                    "pid": int(pid),
                    "controls": None,      # 懒查：只有候选才付这个代价
                })
        except Exception as exc:
            _note("enum_err", exc)
        return True

    win32gui.EnumWindows(cb, None)
    return out


def snapshot():
    """当前状态快照（不修改任何窗口）。"""
    wins = find_trade_windows()
    pids = trade_proc_pids()
    base = {"hwnd": None, "visible": False, "iconic": False,
            "controls": False, "procs": pids, "n_windows": len(wins)}

    # 优先取「已可见」的那个（可能同时存在隐藏的旧窗口）
    wins.sort(key=lambda w: (not w["visible"], w["iconic"]))
    for w in wins:
        if w["controls"] is None:
            w["controls"] = _has_trade_controls(w["hwnd"])
    ready = [w for w in wins if w["visible"] and not w["iconic"] and w["controls"]]
    if ready:
        w = ready[0]
        base.update(state="READY", hwnd=w["hwnd"], visible=True,
                    iconic=w["iconic"], controls=True,
                    detail="窗口可见且交易控件齐全")
    elif wins:
        w = wins[0]
        base.update(hwnd=w["hwnd"], visible=w["visible"],
                    iconic=w["iconic"], controls=w["controls"])
        if not w["controls"]:
            base.update(state="NOT_LOGGED_IN",
                        detail="交易窗口在（hwnd=%d），但没有交易控件 → 模拟盘未登录" % w["hwnd"])
        elif w["iconic"]:
            base.update(state="ICONIC", detail="交易窗口已最小化")
        else:
            base.update(state="HIDDEN", detail="交易窗口存在但不可见（IsWindowVisible=False）")
    elif pids:
        base.update(state="NO_WINDOW",
                    detail="交易客户端进程在（pid=%s），但还没出现交易窗口（启动中或卡在启动页）"
                           % ",".join(str(p) for p in pids))
    else:
        base.update(state="MISSING_PROC", detail="交易客户端 xiadan.exe 不在运行")
    return base


def show_trade_window(hwnd, iconic):
    """把交易窗口显示出来。**不调 SetForegroundWindow** —— 无人值守时抢焦点是副作用。"""
    cmd = win32con.SW_RESTORE if iconic else win32con.SW_SHOW
    try:
        win32gui.ShowWindow(hwnd, cmd)
    except Exception as exc:
        return False, "ShowWindow 失败：%r" % (exc,)
    # 给窗口一点时间完成映射（同花顺窗口有自绘初始化）
    for _ in range(10):
        if win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd):
            return True, "ShowWindow(%s) 成功，窗口已可见" % ("SW_RESTORE" if iconic else "SW_SHOW")
        time.sleep(0.2)
    return False, "ShowWindow 已调用，但窗口仍不可见"


def ensure_trade_window(timeout=60, show=True, quiet=False):
    """确保交易窗口可用。返回 snapshot dict（含 actions 列表）。"""
    actions = []
    t0 = time.time()
    deadline = t0 + max(0, timeout)
    last = None
    while True:
        snap = snapshot()
        last = snap
        state = snap["state"]
        if state == "READY":
            break
        # 这两种状态靠「等」或「点一下」都解决不了，立刻返回，别浪费重试预算
        if state == "MISSING_PROC":
            break
        # 只在「窗口确实没显示出来」时才动手，避免每轮重复 ShowWindow
        if show and snap["hwnd"] and ((not snap["visible"]) or snap["iconic"]):
            ok, msg = show_trade_window(snap["hwnd"], snap["iconic"])
            actions.append(msg)
            if not quiet:
                print("[trade_up] " + msg)
            if state == "NOT_LOGGED_IN":
                # 显示之后再确认一次控件是否出现（登录完成后控件才会构建）
                time.sleep(1.0)
        if time.time() >= deadline:
            break
        time.sleep(3)

    # A restore on the final allowed iteration must be observed before reporting.
    snap = snapshot()
    snap["actions"] = actions

    # 控件 ID 判据不命中时，用交易链路自己的连接路径复核一次（2026-11-17 假阴性修复）。
    # 只做一次、只在必要时做 —— 一次 UIA 连接约 10~30 秒，不能在轮询循环里反复做。
    if snap["state"] == "NOT_LOGGED_IN":
        ok, why = _broker_usable()
        if ok:
            # Account reads can outlast a hide/minimize/close. Recheck the
            # actual window after the read before promoting it to READY.
            snap = snapshot()
            snap["actions"] = actions
            if snap.get("hwnd") and snap.get("visible") and not snap.get("iconic"):
                snap["state"] = "READY"
                snap["detail"] = ("控件 ID 探针未命中（当前客户端布局不含 _READY_CTRL_IDS 全集），"
                                  "但 UIA 复核通过 → 判定可用：{}".format(why))
                snap["fallback"] = "uia"
                _log("fallback=uia state=READY hwnd={} detail={}".format(snap.get("hwnd"), why))
        else:
            snap["detail"] = "固定控件探针未齐全；UIA账户复核未通过：" + why

    snap["ok"] = (snap["state"] == "READY")
    snap["elapsed"] = round(time.time() - t0, 1)
    return snap


ADVICE = {
    "READY": "可正常交易，无需处理",
    "HIDDEN": "窗口存在但被隐藏 → 执行 tools/ths_trade_up.py --ensure 即可显示",
    "ICONIC": "窗口已最小化 → 执行 tools/ths_trade_up.py --ensure 即可还原",
    "NOT_LOGGED_IN": "模拟盘未登录 → 打开同花顺登录模拟炒股账号（本工具不代登录）",
    "NO_WINDOW": "客户端进程在但没有交易窗口 → 等它启动完；仍不行就重启同花顺",
    "MISSING_PROC": "交易客户端没在跑 → 打开同花顺（或由计划任务 THS-AutoStart 拉起）",
}


def _fmt(snap):
    lines = [
        "state   : %s" % snap["state"],
        "detail  : %s" % snap["detail"],
        "hwnd    : %s  visible=%s iconic=%s controls=%s" % (
            snap["hwnd"], snap["visible"], snap["iconic"], snap["controls"]),
        "procs   : %s" % (",".join(str(p) for p in snap["procs"]) or "(none)"),
        "window数: %s（全部同标题窗口，含隐藏）" % snap["n_windows"],
        "建议    : %s" % ADVICE.get(snap["state"], "查看 state/ths_trade_up.log"),
    ]
    if snap.get("actions"):
        lines.append("已执行  : " + " | ".join(snap["actions"]))
    return "\n".join(lines)


def main(argv):
    show = "--ensure" in argv
    as_json = "--json" in argv
    wait = 0
    if "--wait" in argv:
        try:
            wait = int(argv[argv.index("--wait") + 1])
        except Exception:
            wait = 0

    snap = ensure_trade_window(timeout=wait, show=show, quiet=as_json)
    diag = {k: v for k, v in _DIAG.items() if v}
    _log("state=%s ok=%s hwnd=%s actions=%s wait=%s diag=%s" % (
        snap["state"], snap["ok"], snap["hwnd"], snap.get("actions"), wait, diag))
    snap["diag"] = diag

    if as_json:
        payload = {k: v for k, v in snap.items() if k != "actions"}
        payload["actions"] = snap.get("actions", [])
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("=" * 60)
        print("模拟交易盘就绪检查  %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        print("=" * 60)
        print(_fmt(snap))
        if snap.get("diag"):
            print("探测异常: %s" % snap["diag"])
        print("=" * 60)
        print("[%s] %s" % ("就绪" if snap["ok"] else "未就绪",
                          "可进入交易链路" if snap["ok"] else ADVICE.get(snap["state"], "")))
    return 0 if snap["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
