"""每日自动交易入口（**用 32 位 Python 运行**）。

为什么不让 daily_run.bat 直接调 main.py：
    bat 没法记录结构化结果。失败时只留一大段日志，而无人值守时**没有人会去看**，
    任务就"悄悄不跑了"。本脚本在 main.py 外面包一层，负责：
      · 把运行结果写成结构化状态 state/last_run.json
      · 失败时写显眼的 state/ALERT.txt（含原因 + 日志尾部），成功时自动清除
      · 失败时**发告警邮件**（config/mail_alert.json；未配置则静默跳过）——
        消息框只在人在电脑前有用，邮件是无人值守时唯一能到达的通道
      · 用非零退出码把失败向上传递（Task Scheduler 的 LastTaskResult 会体现）

**计划运行 vs 手动运行**（2026-09-19 新增 `--manual`）
----------------------------------------------------
`state/last_run.json` 的语义是「**每日自动交易**（计划任务）最近一次运行的健康状态」，
`tools/check_alerts.py` 靠它判断自动交易是否正常。

但**任何一次额外的运行（手动重跑 / 调试 / 补跑）也会写同一个文件** —— 如果它失败了，
就会把当天计划任务的**成功记录覆盖掉**，让次日体检误报「自动交易失败」。
（2026-09-18 21:02 的误报事故就是这条机制：当天 14:50 计划任务其实成功。）

→ 因此手动运行请显式加 `--manual`：
      · 状态写 `state/last_run_manual.json`，**不覆盖** `state/last_run.json`
      · 失败写 `state/ALERT_manual.txt`，**不污染** `state/ALERT.txt`
        （`check_alerts.py` 只看 ALERT.txt，所以手动失败不会触发自动交易告警）
      · 成功仍会清除 `ALERT.txt` —— 手动**补跑**成功意味着问题已解决，告警该消

用法：
    E:\\Python32\\python.exe tools/daily_job.py             # 计划运行（或手动补跑整条链路）
    E:\\Python32\\python.exe tools/daily_job.py --manual    # 调试 / 额外重跑

**连接类失败的限窗重试**（2026-09-20 新增，D5）
----------------------------------------------
历史失败里最常见的一类就是「同花顺客户端没开 / 没登录 / 被最小化」
（09-14 / 09-15 / 09-16 连续三天栽在这上面）。这类失败**是可自愈的**
—— 人把窗口点出来就行 —— 但无人值守时没人点，于是白丢一个交易日。

于是：这类失败会**在收盘前的窄窗口内自动重试**（`_RETRY_MAX` 次、`_RETRY_GAP_SEC` 间隔，
14:50 起 → 14:52 / 14:54 / 14:56）。

- **只重试「连接同花顺」这一类**：别的失败（选股报错 / 数据陈旧 / 对账失败）
  重试也不会变好 → 立刻放弃，避免用重试掩盖真问题。
- **收盘后绝不重试**（窗口上限 `_RETRY_WINDOW_END` = 14:56）：
  15:00 之后既无法按当日价成交，又平白增加「重复下单」的风险。
- 尝试次数写进 `last_run.json` 的 `attempts` 字段，事后可查。
"""
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.trading_session import (SHANGHAI, require_trading_session,
                                   current_trading_session_deadline)
STATE = ROOT / "state"
LOG = STATE / "daily_run.log"
# 计划运行的状态文件（check_alerts 判定「每日自动交易是否健康」的唯一依据）
STATUS = STATE / "last_run.json"
ALERT = STATE / "ALERT.txt"
# 手动运行（--manual）的独立状态文件与告警 —— **不得覆盖上面两个**，
# 否则一次额外运行的失败会污染当天计划任务的健康记录（见模块 docstring）。
STATUS_MANUAL = STATE / "last_run_manual.json"
ALERT_MANUAL = STATE / "ALERT_manual.txt"

# 常见失败的排查提示
_HINTS = {
    # 2026-11-16 新增：前置预检自己的失败信号（最具体，必须排在前面）
    "模拟交易盘未就绪": "交易窗口不可用（隐藏/未登录/进程没开）→ 跑 "
                        "E:\\Python32\\python.exe tools\\ths_trade_up.py --ensure 看状态并自愈",
    "网上股票交易系统": "同花顺交易窗口没找到 → 确认客户端已打开且已登录模拟盘（不可最小化）",
    "对账失败": "持仓/资金读取未通过校验 → 检查同花顺是否停在异常弹窗上，重跑一次",
    "行情数据陈旧": "行情没刷新 → 手动执行 E:\\Python\\python.exe tools\\refresh_data.py",
    "未出现「委托确认」弹窗": "委托未提交成功 → 检查同花顺是否弹出其它对话框挡住",
}

# ⚠️ 只认这两个参数。**任何别的参数一律拒绝执行** —— 见 main() 开头的守卫。
_ALLOWED_ARGS = {"--manual", "--simulate-failure", "--data-ready"}

# ---- 连接类失败的「限窗重试」（2026-09-20 新增，D5）----
# 历史失败里最常见的一类就是「同花顺客户端没开 / 没登录 / 被最小化」
# （09-14 / 09-15 / 09-16 连续三天栽在这上面）。这类失败是**可自愈**的
# —— 人把窗口点出来就行 —— 但无人值守时没人点，于是白丢一个交易日。
#
# 因此：在**收盘前**的窄窗口内自动重试。**收盘后绝不重试** ——
# 15:00 之后重试既无法按当日价成交，又平白增加「重复下单」的风险。
_RETRY_MAX = 3
_RETRY_GAP_SEC = 120            # 每 2 分钟一次：14:50 起 → 14:52 / 14:54 / 14:56
_RETRY_WINDOW_END = (14, 56)    # 当天该时刻之后不再重试
# 只有这些字样才算「连接类失败」——别的失败（选股报错/数据陈旧/对账失败）
# 重试也不会变好，直接放弃，避免掩盖真问题。
_RETRY_KEYWORDS = ("同花顺连接失败", "网上股票交易系统", "交易窗口")

# ---- 前置预检（2026-11-16 新增）----
# 同花顺**开机自启**后，交易窗口 `网上股票交易系统5.0` 可能是**隐藏**的
# （`IsWindowVisible=False`）。2026-11-16 08:38:50 的自启动日志实测就是这样，
# 结果 14:50 的定时任务从 connect 开始连挂 4 次（120 秒一次，空转约 6 分钟）才放弃，
# 当日白丢一个交易日 —— 窗口在、也登录着，只是没人点出来。
# 所以进交易链路前先「把窗口弄到可用」；弄不好就**立刻**失败告警，
# 不把那 6 分钟浪费在必然失败的重试上（那段时间本来是留给"弹窗挡一下"这类偶发的）。
_PRE_WAIT_SEC = 45


def _log_offset():
    try:
        return LOG.stat().st_size if LOG.exists() else 0
    except Exception:
        return 0


def _read_since(offset, n=40):
    """只取「本次运行」产生的日志（否则会把上一次成功运行的输出误当成本次结果）。"""
    try:
        raw = LOG.read_bytes()
        seg = raw[offset:] if 0 <= offset <= len(raw) else raw
        text = seg.decode("utf-8", "ignore")
        lines = [l for l in text.splitlines() if l.strip()]
        return "\n".join(lines[-n:]) if lines else ""
    except Exception:
        return "(日志不可读)"


def _hint_for(tail: str) -> str:
    if not tail.strip():
        return "本次运行没有产生任何日志 —— 很可能是 Python/脚本启动阶段就失败了"
    for k, v in _HINTS.items():
        if k in tail:
            return v
    return "查看 state/daily_run.log 尾部定位原因"


def _in_retry_window(now=None) -> bool:
    """现在是否还在「收盘前」的重试窗口内。

    对**手动运行**同样适用 —— 收盘后重试一样无意义（没法按当日价成交）。
    """
    try:
        require_trading_session(now or _now())
        return True
    except RuntimeError:
        return False


def _now():
    return datetime.now(SHANGHAI)


def _check_cached_health():
    """Revalidate the complete configured pool even after a batch refresh."""
    import config
    from dataprovider.calendar import is_trading_day
    from dataprovider.store import DataStore
    from tools.refresh_data import health_check
    # The health checker predates local-only calendars. Validate local
    # coverage first so a missing calendar is a blocking health failure.
    is_trading_day(_now(), local_only=True)
    codes = list(config.RECOMMENDED_POOLS["ETF全球"])
    problems, notes = health_check(DataStore(), codes, "ETF全球")
    for note in notes:
        print("[daily_job] " + note)
    return problems


def _prepare_data(data_ready=False):
    """Run 64-bit refresh unless the batch did it; always check cache health."""
    try:
        if not data_ready:
            python64 = os.environ.get("PYTHON64") or r"E:\Python\python.exe"
            result = subprocess.run(
                [python64, "-X", "utf8", "-u", str(ROOT / "tools" / "refresh_data.py")],
                cwd=str(ROOT), check=False, timeout=600)
            if result.returncode:
                return result.returncode, "行情刷新或体检失败，拒绝交易"
        problems = _check_cached_health()
        if problems:
            return 3, "行情数据体检失败: " + "; ".join(problems)
    except Exception as exc:
        return 3, "行情前置检查不可用，拒绝交易: {!r}".format(exc)
    return 0, ""


def _preflight():
    """Restore the client and verify readiness; never log in or place orders."""
    try:
        import ths_trade_up
    except ImportError:
        from tools import ths_trade_up
    snap = ths_trade_up.ensure_trade_window(timeout=_PRE_WAIT_SEC, show=True, quiet=True)
    if snap.get("state") == "MISSING_PROC":
        # The scheduled job owns the interactive desktop. Launching only the
        # trade client here recovers a missed login-trigger task; authentication
        # remains manual and readiness is verified again after startup.
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "tools" / "ths_autostart.ps1"),
             "-TradeOnly", "-EnsureWaitSec", str(_PRE_WAIT_SEC)],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=_PRE_WAIT_SEC + 30)
        if result.returncode:
            return "客户端安全启动失败 rc={}: {}".format(result.returncode, result.stderr[-1000:])
        snap = ths_trade_up.ensure_trade_window(timeout=0, show=True, quiet=True)
    print("[daily_job] 前置预检：{} —— {}".format(snap["state"], snap["detail"]))
    for action in snap.get("actions") or []:
        print("[daily_job] 预检已执行：" + action)
    if not snap.get("ok"):
        return "{} — {}（建议：{}）".format(
            snap["detail"], snap["state"], ths_trade_up.ADVICE.get(snap["state"], ""))
    return ""


def _is_conn_failure(tail: str) -> bool:
    """失败是否属于「连接同花顺」这一类 —— **只有这类值得重试**。"""
    return any(k in tail for k in _RETRY_KEYWORDS)


def _run_once(argv, selftest: bool):
    """执行一次 `main.py simulate --ths`。返回 (rc, captured_lines, err)。"""
    rc, err, captured = 1, "", []
    if selftest:
        print("[daily_job] SELF-TEST 模拟失败（未执行真实交易）")
        return rc, captured, "self-test: 模拟失败，用于验证告警链路"
    try:
        # 自己捕获子进程输出：既转发到 stdout（bat 会重定向进日志），
        # 又留在内存里做结果摘要 —— 这样无论谁调用本脚本，摘要都可靠。
        proc = subprocess.Popen(
            argv, cwd=str(ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="ignore", bufsize=1)
        for line in proc.stdout:
            print(line, end="")
            captured.append(line.rstrip("\n"))
        rc = proc.wait()
    except Exception:
        err = traceback.format_exc(limit=3)
    return rc, captured, err


def main():
    # Parse before touching production paths; simulation is fully isolated.
    unknown = [a for a in sys.argv[1:] if a not in _ALLOWED_ARGS]
    if unknown:
        print("[daily_job] 未知参数 {}，拒绝执行".format(unknown))
        return 2
    if "--simulate-failure" in sys.argv:
        print("[daily_job] SELF-TEST 模拟失败（无交易、预检、状态、邮件或界面副作用）")
        return 1

    STATE.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-X", "utf8", str(ROOT / "main.py"), "simulate", "--ths"]
    started = _now()
    manual = "--manual" in sys.argv
    status_path = STATUS_MANUAL if manual else STATUS
    alert_path = ALERT_MANUAL if manual else ALERT
    rc, err, captured = 1, "", []
    n_attempt = 0
    offset = _log_offset()
    deadline = None
    pre_fail = ""
    try:
        require_trading_session(started)
        deadline = current_trading_session_deadline(started)
        rc, pre_fail = _prepare_data("--data-ready" in sys.argv)
        if not pre_fail and rc == 0:
            require_trading_session(_now())
            pre_fail = _preflight()
            if pre_fail:
                rc = 1
    except Exception as exc:
        rc = 1
        pre_fail = "前置安全检查失败，拒绝交易: {!r}".format(exc)

    if pre_fail or rc:
        captured = [pre_fail or "行情前置检查失败"]
    else:
        for k in range(_RETRY_MAX + 1):
            # Every attempt checks both the absolute original deadline and
            # the calendar/window, including after a clipped retry sleep.
            current = _now()
            try:
                if current >= deadline:
                    raise RuntimeError("本交易时段已结束，不再重试")
                require_trading_session(current)
            except RuntimeError as exc:
                rc, err = 1, str(exc)
                if not captured:
                    captured = [err]
                break
            n_attempt += 1
            if k:
                print("[daily_job] 重试 {}/{}".format(k, _RETRY_MAX))
            offset = _log_offset()
            rc, captured, err = _run_once(argv, False)
            if rc == 0 or k == _RETRY_MAX:
                break
            attempt_tail = ("\n".join([line for line in captured if line.strip()][-40:])
                            or _read_since(offset))
            if not _is_conn_failure(attempt_tail):
                break
            remaining = (deadline - _now()).total_seconds()
            if remaining <= 0:
                break
            wait = min(float(_RETRY_GAP_SEC), remaining)
            print("[daily_job] 连接类失败，{} 秒后复查交易窗口".format(wait))
            time.sleep(wait)

    tail = "\n".join([l for l in captured if l.strip()][-40:]) or _read_since(offset)
    ok = (rc == 0)

    status = {
        "ts": started.strftime("%Y-%m-%d %H:%M:%S"),
        "date": started.strftime("%Y%m%d"),
        "mode": "manual" if manual else "scheduled",
        "ok": ok,
        "exit_code": rc,
        "attempts": n_attempt,          # 实际尝试次数（>1 说明发生过重试）
        "cmd": "main.py simulate --ths",
        "rebalanced": ("非调仓日" not in tail) if ok else None,
        "summary": next((l.strip() for l in reversed(tail.splitlines())
                         if "总资产" in l), ""),
        "error": err,
    }
    try:
        status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception:
        pass

    if ok:
        # 成功就清掉旧告警，避免"狼来了"。
        # 手动**补跑**成功也清 ALERT.txt —— 那说明问题已解决，告警该消。
        try:
            for p in (ALERT, ALERT_MANUAL):
                if p.exists():
                    p.unlink()
        except Exception:
            pass
        print("[daily_job] OK  {}  [{}]".format(
            status["summary"], status["mode"]))
        return 0

    body = [
        "=" * 60,
        "【自动交易失败告警】" if not manual else "【手动运行失败（不代表自动交易异常）】",
        "=" * 60,
        "模式   : {}".format(status["mode"]),
        "时间   : {}".format(status["ts"]),
        "命令   : {}".format(status["cmd"]),
        "退出码 : {}".format(rc),
        "可能原因: {}".format(_hint_for(tail)),
        "",
        "--- 日志尾部 ---",
        tail,
        "",
        # ⚠️ 别指向 `main.py simulate --ths`（2026-10-30 教训）：那样跑完**不留运行记录**，
        # 巡检与面板都看不到「今天已补救」，下一个交易日还会被判成「没跑成功」。
        # --manual 写 last_run_manual.json / ALERT_manual.txt，且不覆盖定时任务的状态。
        "处理完可直接重跑:  E:\\Python32\\python.exe -u tools\\daily_job.py --manual",
        "（--manual = 手动补跑：写 state/last_run_manual.json，不覆盖 last_run.json）",
        "=" * 60,
    ]
    text = "\n".join(body)
    try:
        alert_path.write_text(text, encoding="utf-8")
        print(text)
    except Exception:
        print(text)
    if manual:
        print("[daily_job] 手动模式（--manual）：状态已写 {}，未触碰 {}".format(
            status_path.name, STATUS.name))

    # 邮件告警：消息框只在「人在电脑前」有用；人不在时**只有邮件能到达**。
    # 未配置 config/mail_alert.json 时静默跳过（send 返回 False，不报错、不影响退出码）。
    # 2026-09-14/15/16 三次失败都是「同花顺没开」，而当时人都不在电脑前 —— 加这条链路。
    # ⚠️ `--manual` 不发邮件：手动运行时**人在电脑前**，且该失败**不代表
    #    「无人值守的自动交易」异常**（否则会污染对自动交易健康度的判断）。
    if manual:
        print("[daily_job] 手动模式（--manual）失败 → 不发邮件；详情见 {}".format(
            alert_path.name))
    else:
        try:
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            from tools.mail_alert import send as _send_mail
            subject = "[agentskill] 自动交易失败 {}".format(status["date"])
            mail_body = text + "\n\n本机: {}\n配置: config/mail_alert.json\n".format(
                ROOT)
            if _send_mail(subject, mail_body):
                print("[daily_job] 告警邮件已发送")
            else:
                print("[daily_job] 邮件告警未启用/未发送（检查 config/mail_alert.json；"
                      "模板见 config/mail_alert.example.json）")
        except Exception as _e:
            print("[daily_job] 邮件告警异常（已忽略，不影响交易）: {}".format(_e))

    # Windows 弹窗提醒（本机可见时最直观；失败不影响退出码）
    _msg = ("手动运行失败（不影响自动交易告警），详见 state\\ALERT_manual.txt"
            if manual else "自动交易失败，详见 state\\ALERT.txt")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Add-Type -AssemblyName PresentationFramework;"
             "[System.Windows.MessageBox]::Show('{}','agentskill')".format(_msg)],
            timeout=8, capture_output=True)
    except Exception:
        pass
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
