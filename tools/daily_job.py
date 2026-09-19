"""每日自动交易入口（**用 32 位 Python 运行**）。

为什么不让 daily_run.bat 直接调 main.py：
    bat 没法记录结构化结果。失败时只留一大段日志，而无人值守时**没有人会去看**，
    任务就"悄悄不跑了"。本脚本在 main.py 外面包一层，负责：
      · 把运行结果写成结构化状态 state/last_run.json
      · 失败时写显眼的 state/ALERT.txt（含原因 + 日志尾部），成功时自动清除
      · 失败时**发告警邮件**（config/mail_alert.json；未配置则静默跳过）——
        消息框只在人在电脑前有用，邮件是无人值守时唯一能到达的通道
      · 用非零退出码把失败向上传递（Task Scheduler 的 LastTaskResult 会体现）

用法：
    E:\\Python32\\python.exe tools/daily_job.py
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
STATUS = STATE / "last_run.json"
ALERT = STATE / "ALERT.txt"

# 常见失败的排查提示
_HINTS = {
    "网上股票交易系统": "同花顺交易窗口没找到 → 确认客户端已打开且已登录模拟盘（不可最小化）",
    "对账失败": "持仓/资金读取未通过校验 → 检查同花顺是否停在异常弹窗上，重跑一次",
    "行情数据陈旧": "行情没刷新 → 手动执行 E:\\Python\\python.exe tools\\refresh_data.py",
    "未出现「委托确认」弹窗": "委托未提交成功 → 检查同花顺是否弹出其它对话框挡住",
}


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
    argv = [sys.executable, str(ROOT / "main.py"), "simulate", "--ths"]
    started = datetime.now()
    offset = _log_offset()

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
        "ok": ok,
        "exit_code": rc,
        "cmd": "main.py simulate --ths",
        "rebalanced": ("非调仓日" not in tail) if ok else None,
        "summary": next((l.strip() for l in reversed(tail.splitlines())
                         if "总资产" in l), ""),
        "error": err,
    }
    try:
        STATUS.write_text(json.dumps(status, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    except Exception:
        pass

    if ok:
        # 成功就清掉旧告警，避免"狼来了"
        try:
            if ALERT.exists():
                ALERT.unlink()
        except Exception:
            pass
        print("[daily_job] OK  {}".format(status["summary"]))
        return 0

    body = [
        "=" * 60,
        "【自动交易失败告警】",
        "=" * 60,
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
        ALERT.write_text(text, encoding="utf-8")
        print(text)
    except Exception:
        print(text)

    # 邮件告警：消息框只在「人在电脑前」有用；人不在时**只有邮件能到达**。
    # 未配置 config/mail_alert.json 时静默跳过（send 返回 False，不报错、不影响退出码）。
    # 2026-09-14/15/16 三次失败都是「同花顺没开」，而当时人都不在电脑前 —— 加这条链路。
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
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Add-Type -AssemblyName PresentationFramework;"
             "[System.Windows.MessageBox]::Show("
             "'自动交易失败，详见 state\\ALERT.txt','agentskill')"],
            timeout=8, capture_output=True)
    except Exception:
        pass
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
