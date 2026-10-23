"""运行环境快照（只读，无副作用）。

用途：在每次 daily_run 开始时把「当时桌面上到底有什么」记进日志，
避免事后靠猜（2026-10-19 的「未找到交易窗口 + 没看到 cmd 窗口」就属于无法回溯的情况）。

记录内容
  1. 当前进程所在会话 ID、输入桌面名（Default=正常解锁桌面，Winlogon=锁屏中）
  2. 所有可见顶层窗口的 标题 / 进程名 / PID
  3. 同花顺相关进程（hexin / HxMain / xiadan / hexinhelper）及其会话
  4. 交易窗口是否命中（与 trader/ths_uia._find_trade_hwnd 同判据：可见 + 标题前缀）

输出：纯文本，每行以 [env] 前缀，方便 grep。
退出码恒 0 —— 它只是取证工具，不得影响交易链路。
"""
import ctypes
import os
import sys

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32

TRADE_PREFIX = "网上股票交易系统"   # 与 trader/ths_uia.py 保持一致

TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


def _session_of(pid):
    sid = ctypes.c_ulong(0)
    try:
        ok = k32.ProcessIdToSessionId(ctypes.c_ulong(pid), ctypes.byref(sid))
        return sid.value if ok else -1
    except Exception:
        return -1


def _input_desktop_name():
    """返回当前输入桌面名：Default 正常；Winlogon 表示锁屏/登录界面。"""
    try:
        h = u32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if not h:
            return "UNKNOWN(err=%d)" % k32.GetLastError()
        buf = ctypes.create_unicode_buffer(256)
        need = ctypes.c_ulong(0)
        ok = u32.GetUserObjectInformationW(h, 2, buf, ctypes.sizeof(buf), ctypes.byref(need))
        u32.CloseDesktop(h)
        return buf.value if ok else "UNKNOWN"
    except Exception as exc:
        return "UNKNOWN(%s)" % exc


def _processes():
    """返回 {pid: exe_name}（含全部进程，调用方按需过滤）。"""
    res = {}
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1 or snap == 0xFFFFFFFF:
        return res
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    ok = k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        res[int(entry.th32ProcessID)] = entry.szExeFile
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return res


def main():
    procs = _processes()
    print("[env] pid=%d session=%d desktop=%s" % (
        os.getpid(), _session_of(os.getpid()), _input_desktop_name()))

    ths_keys = ("hexin", "hxmain", "xiadan", "ths.exe")
    ths = [(pid, name) for pid, name in procs.items()
           if any(name.lower().startswith(k) for k in ths_keys)]
    if ths:
        for pid, name in sorted(ths, key=lambda x: x[1].lower()):
            print("[env] ths_proc %s pid=%d session=%d" % (name, pid, _session_of(pid)))
    else:
        print("[env] ths_proc NONE")

    rows = []
    skipped = []          # 枚举失败必须留痕（selftest「禁止静默吞异常」纪律）

    def cb(hwnd, _):
        # ⚠️ 每个分支都必须 return True（2026-10-23 修复）
        # 回调签名是 BOOL CALLBACK，返回 None/False 会让 EnumWindows **立即终止**。
        # 原实现用裸 `return` 跳过不可见窗口 → 转成 False → 枚举在第 1 个不可见
        # 窗口处就停了，于是 `visible_windows` 恒为 1（往往只有 QA OSD），
        # 交易窗口明明开着也报 `trade_window=MISS`。
        # 后果：10-23 19:10 补跑成功的同时，日志仍写着 MISS —— 这份「取证」
        # 反而会把人引向「客户端没开」的错误结论（10-19 的诊断就被它污染过）。
        try:
            if not u32.IsWindowVisible(hwnd):
                return True
            n = u32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 2)
            u32.GetWindowTextW(hwnd, buf, n + 2)
            pid = ctypes.c_ulong(0)
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            rows.append((buf.value.strip(), procs.get(int(pid.value), "?"), int(pid.value)))
        except Exception as exc:            # 回调里不能抛（会让 EnumWindows 提前中断）
            skipped.append(repr(exc))
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    u32.EnumWindows(WNDENUMPROC(cb), 0)

    hit = [r for r in rows if r[0].startswith(TRADE_PREFIX)]
    print("[env] visible_windows=%d" % len(rows))
    if skipped:
        print("[env] 窗口枚举跳过 %d 个（首个错误：%s）" % (len(skipped), skipped[0]))
    for title, exe, pid in rows:
        print("[env]   win pid=%-6d exe=%-18s title=%s" % (pid, exe, title[:70]))
    if hit:
        print("[env] trade_window=HIT %d (%s)" % (len(hit), hit[0][2]))
    else:
        print("[env] trade_window=MISS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:            # 取证工具永不拖垮交易链路
        print("[env] probe_error=%r" % (exc,))
        sys.exit(0)
