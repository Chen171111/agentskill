"""失败告警邮件 —— 让 `daily_job` 在失败那一刻**自己发信**。

为什么需要
----------
原有告警只到**本机**：写 `state/ALERT.txt` + 弹 Windows 消息框。
但计划任务 14:50 跑的时候人往往不在电脑前，消息框等于没弹
（2026-09-14 / 09-15 / 09-16 三次失败都没有被及时看到）。
**邮件是唯一「人不在电脑前也能收到」的通道**，且不依赖 WorkBuddy 是否在运行。

配置（**不进 git**）
-------------------
`config/mail_alert.json`：

    {
      "enabled": true,
      "smtp_host": "smtp.qq.com",
      "smtp_port": 465,
      "use_ssl": true,
      "username": "you@qq.com",
      "password": "邮箱授权码（不是登录密码！）",
      "sender": "you@qq.com",
      "recipients": ["you@qq.com"]
    }

- 文件不存在 / `enabled=false` / 关键字段缺失 → **静默跳过**（返回 False），
  绝不干扰交易主流程。
- `password` 也可用环境变量 `AGENTSKILL_SMTP_PASSWORD` 覆盖（避免明文落盘）。

常见邮箱 SMTP
-------------
| 邮箱 | 服务器 | 端口 | 说明 |
|---|---|---|---|
| QQ | smtp.qq.com | 465 (SSL) | 授权码在「设置→账户→IMAP/SMTP服务」开启后生成 |
| 163 | smtp.163.com | 465 (SSL) | 同上 |
| Gmail | smtp.gmail.com | 465 (SSL) | 需「应用专用密码」 |
| Outlook | smtp.office365.com | 587 (STARTTLS) | 需 `"use_ssl": false` |

自检
----
    E:\\Python32\\python.exe tools\\mail_alert.py --test
未配置时会明确提示"未配置，跳过"。
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
import sys
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "mail_alert.json"
TIMEOUT = 25          # SMTP 可能卡住，必须有超时，否则会拖死 14:50 的任务


def load_config():
    """读配置；不可用返回 None（调用方据此静默跳过）。"""
    if not CONFIG.exists():
        return None
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(cfg, dict) or not cfg.get("enabled", True):
        return None
    pwd = os.environ.get("AGENTSKILL_SMTP_PASSWORD") or cfg.get("password")
    if not pwd or not all(cfg.get(k) for k in
                          ("smtp_host", "username", "sender", "recipients")):
        return None
    cfg = dict(cfg)
    cfg["password"] = pwd
    return cfg


def send(subject: str, body: str) -> bool:
    """发送告警邮件。返回是否真的发出去了。

    **任何异常都被吞掉** —— 告警本身绝不能影响交易主流程，
    也不该把 daily_job 的退出码搞乱。
    """
    cfg = load_config()
    if not cfg:
        return False
    try:
        to = cfg["recipients"]
        if isinstance(to, str):
            to = [to]
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr(("agentskill", str(cfg["sender"])))
        msg["To"] = ", ".join(str(x) for x in to)

        host = str(cfg["smtp_host"])
        port = int(cfg.get("smtp_port", 465))
        if cfg.get("use_ssl", True):
            srv = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT,
                                   context=ssl.create_default_context())
        else:
            srv = smtplib.SMTP(host, port, timeout=TIMEOUT)
            srv.starttls(context=ssl.create_default_context())
        try:
            srv.login(str(cfg["username"]), str(cfg["password"]))
            srv.sendmail(str(cfg["sender"]), [str(x) for x in to], msg.as_string())
        finally:
            try:
                srv.quit()
            except Exception:
                pass
        return True
    except Exception:
        return False


def _selftest() -> int:
    cfg = load_config()
    if not cfg:
        print("[mail_alert] 未配置或未启用 → 跳过（这是正常状态，不是错误）")
        print("  配置路径: {}".format(CONFIG))
        print("  模板:     config/mail_alert.example.json")
        return 0
    print("[mail_alert] 配置已加载:")
    print("  服务器: {}:{}  SSL={}".format(cfg["smtp_host"],
                                            cfg.get("smtp_port", 465),
                                            cfg.get("use_ssl", True)))
    print("  发件人: {}".format(cfg["sender"]))
    print("  收件人: {}".format(cfg["recipients"]))
    ok = send("[agentskill] 邮件告警自检", "这是一封自检邮件。收到即表示告警链路已通。")
    print("[mail_alert] 发送{}".format("成功 ✓" if ok else "失败 ✗（检查授权码/服务器/端口）"))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--test" in sys.argv:
        raise SystemExit(_selftest())
    print(__doc__)
    raise SystemExit(0)
