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
"""
import json
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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
    "网上股票交易系统": "同花顺交易窗口没找到 → 确认客户端已打开且已登录模拟盘（不可最小化）",
    "对账失败": "持仓/资金读取未通过校验 → 检查同花顺是否停在异常弹窗上，重跑一次",
    "行情数据陈旧": "行情没刷新 → 手动执行 E:\\Python\\python.exe tools\\refresh_data.py",
    "未出现「委托确认」弹窗": "委托未提交成功 → 检查同花顺是否弹出其它对话框挡住",
}

# ⚠️ 只认这两个参数。**任何别的参数一律拒绝执行** —— 见 main() 开头的守卫。
_ALLOWED_ARGS = {"--manual", "--simulate-failure"}


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


def main():
    STATE.mkdir(parents=True, exist_ok=True)

    # ⚠️⚠️ 参数守卫：本脚本**会真的走下单链路**（`main.py simulate --ths`）。
    # 严禁把 `--help` / `--dry-run` / `-h` 这类「看起来无害」的参数直接传进来 ——
    # 它们**不被识别**，但脚本会**照常执行整条交易链路**，一次"我只是想看看用法"
    # 就会（1）真去连交易客户端、（2）覆盖 last_run.json、（3）写 ALERT.txt。
    # （2026-09-19 23:57 事故：`daily_job.py --help` 触发了一次真实运行，
    #   失败在 broker 连接阶段 → 未产生交易，但污染了状态文件。这就是加这道守卫的原因。）
    unknown = [a for a in sys.argv[1:] if a not in _ALLOWED_ARGS]
    if unknown:
        print("[daily_job] ❌ 未知参数 {} —— **拒绝执行**："
              "本脚本会走真实下单链路，不会因为参数不认识就跳过交易。".format(unknown))
        print("           可用参数：{}".format(sorted(_ALLOWED_ARGS)))
        print("           只想看用法 → 读本文件顶部 docstring，**不要执行**。")
        return 2

    argv = [sys.executable, str(ROOT / "main.py"), "simulate", "--ths"]
    started = datetime.now()
    offset = _log_offset()

    # --manual：手动 / 额外运行 → 状态与告警写**独立文件**，
    # 不覆盖计划运行的健康记录（见模块 docstring 的「计划运行 vs 手动运行」）。
    manual = "--manual" in sys.argv
    status_path = STATUS_MANUAL if manual else STATUS
    alert_path = ALERT_MANUAL if manual else ALERT

    # 自检开关：不真的交易，直接走失败分支，用来验证「告警链路」是否有效。
    # （报警器要定期按一下，否则坏掉了也不知道）
    selftest = "--simulate-failure" in sys.argv

    rc = 1
    err = ""
    captured = []
    if selftest:
        err = "self-test: 模拟失败，用于验证告警链路"
        print("[daily_job] SELF-TEST 模拟失败（未执行真实交易）")
    else:
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

    tail = "\n".join([l for l in captured if l.strip()][-40:]) or _read_since(offset)
    ok = (rc == 0)

    status = {
        "ts": started.strftime("%Y-%m-%d %H:%M:%S"),
        "date": started.strftime("%Y%m%d"),
        "mode": "manual" if manual else "scheduled",
        "ok": ok,
        "exit_code": rc,
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
        "处理完可直接重跑:  E:\\Python32\\python.exe main.py simulate --ths",
        "（或直接重新运行 daily_run.bat）",
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
