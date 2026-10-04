"""每日自动交易「体检」：判断今天的自动交易是否真的跑了、跑没跑成。

给两种场景用：
  · 人工随时查看：E:\\Python32\\python.exe tools/check_alerts.py
  · 自动巡检（WorkBuddy 定时任务 / 计划任务）：读它的退出码，非 0 表示有问题

判定口径（2026-09-19 澄清）
--------------------------
只看**计划运行**（每日自动交易）的健康：`state/last_run.json` + `state/ALERT.txt`。
`tools/daily_job.py --manual` 写的是 `state/last_run_manual.json` /
`state/ALERT_manual.txt`，**只作备注提示、不计入 problems** ——
手动运行失败**不代表**「无人值守的自动交易」异常。
（2026-09-18 21:02 一次额外的手动运行失败，曾把当天 14:50 计划任务的**成功**记录
覆盖成 `ok:false`，导致本脚本次日误报「自动交易失败」。修法见 `tools/daily_job.py`。）

退出码：0 = 一切正常；1 = 有问题（失败 / 到点没跑 / 最近一个交易日无记录 / 存在告警文件）
有问题时还会尝试发告警邮件（config/mail_alert.json 未配置则静默跳过；
同一天、问题清单相同只发一次，标记 state/alert_mail_sent.json）。

**非交易日也判定「最近一个交易日有没有跑」（2026-10-01 补）**
----------------------------------------------------------------
2026-10-01（国庆）实测踩坑：计划任务已被删除，但本脚本因 `is_trading_day()` 为 False
而**跳过**「到点没跑」判定 → 照常输出「[正常]」并返回 0，属**假阴性**。
（当天靠外部巡检的只读核查才发现任务早已消失。）
→ 新增按交易日历回溯的「最近一个交易日无运行记录」判定：**不看今天是不是交易日**，
  只要已过 14:50+宽限，就以「最近一个交易日」为基准比对 `last_run.json` 的 date。
  这条检查不依赖计划任务接口（本机非提权下读不到任务库），纯日历推算，无假阳性。
"""
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATE = ROOT / "state"
STATUS = STATE / "last_run.json"
ALERT = STATE / "ALERT.txt"
# 手动运行（`daily_job.py --manual`）的记录与告警：**只提示、不计入问题**。
STATUS_MANUAL = STATE / "last_run_manual.json"
ALERT_MANUAL = STATE / "ALERT_manual.txt"
# 数据体检告警（由 tools/refresh_data.py 写）。
# ⚠️ 与 ALERT.txt **分开**：ALERT 会被 daily_job 的「成功」路径清除，
#    而数据问题不该因为「下单成功」就被掩盖。
DATA_ALERT = STATE / "DATA_ALERT.txt"

# 计划任务设定：周一~周五 14:50，留出运行时间，15:10 之后判定"今天还没跑"
RUN_HHMM = (14, 50)
GRACE_MIN = 20

# 告警邮件去重标记：同一天、问题清单相同 → 只发一次。
SENT_MARK = STATE / "alert_mail_sent.json"


def _maybe_mail(problems, notes):
    """有问题时发告警邮件（config/mail_alert.json 未配置则静默跳过）。

    为什么：2026-09-30 的教训 —— 计划任务被整个删掉、14:50 从未触发，
    「跑了但失败」的邮件告警（daily_job）根本没机会发出；
    而本脚本当时只写本机退出码，靠外部巡检才被发现。
    「到点没跑」这类故障必须由体检脚本自己走邮件通道（人不在场也能收到）。

    去重：同一天、问题清单相同 → 只发一次，避免巡检反复运行时刷邮箱；
    问题清单有变化（新问题出现）→ 再次发送并覆盖标记。
    """
    today = date.today().strftime("%Y%m%d")
    try:
        prev = json.loads(SENT_MARK.read_text(encoding="utf-8"))
        if prev.get("date") == today and prev.get("problems") == problems:
            print("\n[邮件] 今日同问题已告警过，跳过重复发送（{}）".format(SENT_MARK.name))
            return
    except Exception:
        prev = None
    try:
        from tools.mail_alert import send
        body = "agentskill 自动交易体检发现问题（{}）：\n\n".format(
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        body += "问题：\n" + "\n".join("  ✗ " + p for p in problems) + "\n\n"
        body += "备注：\n" + "\n".join(notes) + "\n\n"
        body += ("排查入口：state/daily_run.log ｜ 本脚本：tools/check_alerts.py\n"
                 "任务在册核对：双击 check_schedule.bat（只读）\n"
                 "若「到点没跑」持续出现，用 tools/schedule/register_task.ps1 重注册。")
        ok = send("[agentskill] 自动交易异常 " + today, body)
    except Exception as e:
        print("\n[邮件] 发送出错（不影响退出码）：{}".format(e))
        return
    if ok:
        try:
            SENT_MARK.write_text(
                json.dumps({"date": today, "problems": problems},
                           ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:
            pass
        print("\n[邮件] 告警已发送（同日同问题去重，标记 {}）".format(SENT_MARK.name))
    else:
        print("\n[邮件] 未启用/未发送（检查 config/mail_alert.json；"
              "模板见 config/mail_alert.example.json）")


def _load_status():
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except Exception:
        return None


def _prev_trading_day(today, is_trading_day, lookback=30):
    """今天（含）往前第一个交易日。

    非交易日也要能回答「上一交易日是几号」——2026-10-01 的假阴性就是
    「节假日干脆不判定」造成的，所以这里必须能跨节假日回溯。
    """
    d = today
    for _ in range(lookback):
        try:
            if is_trading_day(d):
                return d
        except Exception:
            return None
        d -= timedelta(days=1)
    return None


def main():
    from dataprovider.calendar import is_trading_day

    now = datetime.now()
    problems = []
    notes = []

    # 今天是否交易日（2026-09-25 起 daily_job 在非交易日按设计跳过，写 skipped 状态）
    today = date.today()
    try:
        trading = is_trading_day(today)
    except Exception as e:
        trading = False
        notes.append("交易日历不可用（{}），跳过「到点没跑」判定".format(e))
    # 到点没跑：今天是交易日、且已过 14:50+宽限，但最后一次运行不是今天
    due = now.hour * 60 + now.minute >= (RUN_HHMM[0] * 60 + RUN_HHMM[1] + GRACE_MIN)
    # 今天（含）往前第一个交易日 —— 非交易日也要有基准，否则节假日会漏检（见文件头说明）
    prev_td = _prev_trading_day(today, is_trading_day) if not trading else today
    if not trading and prev_td is not None:
        notes.append("今天（{}）是非交易日；「最近一个交易日」判定基准 = {}".format(
            today.strftime("%Y-%m-%d"), prev_td.strftime("%Y-%m-%d")))

    st = _load_status()
    if st is None:
        problems.append("从未产生运行记录（state/last_run.json 不存在）—— 自动交易可能一次都没跑过")
    else:
        notes.append("最近一次运行：{}  退出码={}  {}".format(
            st.get("ts"), st.get("exit_code"), st.get("summary") or ""))
        if st.get("skipped") == "non-trading-day":
            # 2026-09-25 新增：daily_job 在非交易日（节假日/周末）按设计跳过，
            # 记录为 ok=true + skipped —— 这是正常状态，不是故障。
            notes.append("  └ 今日为非交易日，计划运行已按设计跳过（skipped=non-trading-day）")
        if not st.get("ok"):
            problems.append("最近一次运行失败（退出码 {}）".format(st.get("exit_code")))

        # 到点没跑：今天是交易日、且已过 14:50+宽限，但最后一次运行不是今天
        if trading and due:
            if str(st.get("date")) != today.strftime("%Y%m%d"):
                problems.append(
                    "今天是交易日且已过 {}:{}，但今天没有运行记录（最近一次是 {}）"
                    "—— 计划任务可能没触发".format(
                        RUN_HHMM[0], RUN_HHMM[1], st.get("ts")))

        # 最近一个交易日无运行记录 —— **不看今天是不是交易日**（2026-10-01 补）。
        # 为什么：国庆假期里计划任务被删，上面那条因 is_trading_day()=False 被跳过，
        # 脚本照常报「正常」，把「任务已从本机消失」这类硬故障漏掉了。
        # 只有已过 14:50+宽限才判，避免盘中（运行前）误报。
        # 只在「今天非交易日」时判，避免与上面那条交易日判定重复报同一件事。
        if due and (not trading) and prev_td is not None:
            last_date = str(st.get("date") or "")
            if last_date and last_date < prev_td.strftime("%Y%m%d"):
                problems.append(
                    "最近一个交易日 {} 没有运行记录（最近一次是 {}）"
                    "—— 计划任务可能没触发、或已被删除".format(
                        prev_td.strftime("%Y-%m-%d"), st.get("ts")))

    if ALERT.exists():
        if trading:
            problems.append("存在告警文件 state/ALERT.txt")
        else:
            # 非交易日：daily_job 按设计跳过、不写新告警，此时存在的 ALERT 必是
            # 上一个交易日的旧告警 → 只提示不计入问题，避免节假日巡检反复报
            # 「自动交易异常」（2026-09-25 中秋节实际发生）。
            # 下一个交易日恢复全量判定：运行成功自动清 ALERT，失败则 ALERT 更新。
            notes.append("（非交易日，不计入问题）存在旧告警 state/ALERT.txt，留待下一交易日处理：")
        try:
            notes.append(ALERT.read_text(encoding="utf-8", errors="ignore")[:1500])
        except Exception:
            pass

    # 手动运行记录（daily_job.py --manual）：**只提示、不计入 problems** ——
    # 手动运行失败不代表「无人值守的自动交易」异常。
    if STATUS_MANUAL.exists():
        try:
            mj = json.loads(STATUS_MANUAL.read_text(encoding="utf-8"))
            notes.append("另有一次**手动运行**（--manual）记录：{}  退出码={}  {}".format(
                mj.get("ts"), mj.get("exit_code"), mj.get("summary") or ""))
        except Exception:
            pass
    if ALERT_MANUAL.exists():
        notes.append("另有**手动运行**失败记录 state/ALERT_manual.txt"
                     "（不计入问题；自动交易自身状态见上）")

    if DATA_ALERT.exists():
        problems.append("存在**数据体检**告警 state/DATA_ALERT.txt")
        try:
            notes.append(DATA_ALERT.read_text(encoding="utf-8", errors="ignore")[:1500])
        except Exception:
            pass

    print("=" * 60)
    print("agentskill 自动交易体检  {}".format(now.strftime("%Y-%m-%d %H:%M:%S")))
    print("=" * 60)
    for n in notes:
        print(n)
    if problems:
        print("\n[有问题]")
        for p in problems:
            print("  ✗ {}".format(p))
        print("\n排查入口：state/daily_run.log ｜ 数据问题看 state/DATA_ALERT.txt")
        print("若怀疑「到点没跑」：双击 check_schedule.bat 只读核对任务是否还在册。")
        _maybe_mail(problems, notes)
        return 1
    print("\n[正常] 最近一次运行成功；无告警文件，也没有「到点未跑 / 最近一个交易日无记录」的情况。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
