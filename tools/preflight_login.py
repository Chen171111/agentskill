# -*- coding: utf-8 -*-
"""盘前登录体检：提前确认交易客户端可用，给人工登录留出时间。

为什么需要（2026-11-17 实测定案）
--------------------------------
2026-11-16 / 11-17 连续两个交易日，14:50 的计划任务都失败在同一个前置条件上：

    [daily_job] 前置预检：NOT_LOGGED_IN —— 交易窗口在（hwnd=…），但没有交易控件
                → 模拟盘未登录

而交易时段守卫（`tools/trading_session.py` 的 `_WINDOWS`）**下午 14:56 关闭下单窗口**。
也就是说 14:50 才发现未登录，只剩 6 分钟，人工登录基本来不及；并且本工具按设计
**不代登录**。所以把同一套预检提前到 14:20 跑一次：还没登录就立刻发告警邮件，
让人有 30 分钟处理。

本脚本做什么 / 不做什么
----------------------
· 只调用 `ths_trade_up.ensure_trade_window()`：
  可能把**隐藏/最小化**的交易窗口显示出来，也可能**拉起**缺失的交易客户端
  （与 14:50 那条链路完全一致的预检动作）。
· **不登录、不下单、不写账本**。登录必须人工完成。

输出与退出码
------------
· `READY`      → 退出 0；顺手删除历史 `state/LOGIN_ALERT.txt`
· 其它状态     → 写 `state/LOGIN_ALERT.txt` + 发告警邮件；退出 2

⚠️ 刻意用**独立的** LOGIN_ALERT.txt，不碰 `state/ALERT.txt` / `DATA_ALERT.txt`：
那两个文件是 14:50 真实运行与数据体检的判据，混进来会让巡检的「最近一次运行」
口径失真。
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STATE = ROOT / "state"
LOGIN_ALERT = STATE / "LOGIN_ALERT.txt"
SHANGHAI_OFFSET = 8


def _now_text():
    from datetime import timezone, timedelta
    return datetime.now(timezone(timedelta(hours=SHANGHAI_OFFSET))).strftime("%Y-%m-%d %H:%M:%S")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="盘前登录体检（只读：不登录、不下单）")
    ap.add_argument("--timeout", type=int, default=30,
                    help="等待交易窗口就绪的秒数（默认 30）")
    ap.add_argument("--no-mail", action="store_true", help="只写告警文件，不发邮件")
    ap.add_argument("--quiet", action="store_true", help="不打印窗口处理动作明细")
    args = ap.parse_args(argv)

    from tools import ths_trade_up

    try:
        snap = ths_trade_up.ensure_trade_window(
            timeout=args.timeout, show=True, quiet=args.quiet)
    except Exception as exc:                      # 预检本身崩了也要告警，不能静默
        snap = {"ok": False, "state": "PREFLIGHT_CRASH",
                "detail": "预检调用异常: {!r}".format(exc), "actions": []}

    state = snap.get("state", "UNKNOWN")
    detail = snap.get("detail", "")
    advice = ths_trade_up.ADVICE.get(state, "查看 tools/ths_trade_up.py")

    print("=" * 60)
    print("盘前登录体检  " + _now_text())
    print("=" * 60)
    print("状态: {} —— {}".format(state, detail))
    for action in snap.get("actions") or []:
        print("预检已执行: " + action)

    if snap.get("ok"):
        print("结论: 可正常交易，14:50 的计划任务具备前置条件。")
        try:
            if LOGIN_ALERT.exists():
                LOGIN_ALERT.unlink()
                print("已清除历史告警文件 state/LOGIN_ALERT.txt")
        except OSError as exc:                    # 清不掉不影响退出码，但必须说出来
            print("[warn] 清除 LOGIN_ALERT.txt 失败: {}".format(exc))
        return 0

    print("结论: **未就绪** —— " + advice)
    body = (
        "【盘前登录体检未通过】\n"
        "时间   : {}\n"
        "状态   : {}\n"
        "详情   : {}\n"
        "建议   : {}\n\n"
        "交易时段守卫在 14:56 关闭下单窗口，14:50 才发现就只剩 6 分钟。\n"
        "请在 14:50 前完成人工登录（本工具不代登录）。\n"
        "本告警由 tools/preflight_login.py 产生，对应的定时任务为 "
        "QuantPanorama_LoginCheck（工作日 14:20）。\n"
    ).format(_now_text(), state, detail, advice)

    try:
        STATE.mkdir(parents=True, exist_ok=True)
        LOGIN_ALERT.write_text(body, encoding="utf-8")
        print("已写告警文件 state/LOGIN_ALERT.txt")
    except OSError as exc:
        print("[warn] 写告警文件失败: {}".format(exc))

    if not args.no_mail:
        try:
            from tools.mail_alert import send as send_mail
            if send_mail("[agentskill] 盘前登录体检未通过 {}".format(_now_text()[:10]), body):
                print("告警邮件已发送")
            else:
                print("邮件告警未启用/未发送（检查 config/mail_alert.json）")
        except Exception as exc:                  # 邮件失败不掩盖「未就绪」这个事实
            print("[warn] 发邮件异常: {!r}".format(exc))

    return 2


if __name__ == "__main__":
    sys.exit(main())
