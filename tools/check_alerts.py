"""每日自动交易「体检」：判断今天的自动交易是否真的跑了、跑没跑成。

给两种场景用：
  · 人工随时查看：E:\\Python32\\python.exe tools/check_alerts.py
  · 自动巡检（WorkBuddy 定时任务 / 计划任务）：读它的退出码，非 0 表示有问题

退出码：0 = 一切正常；1 = 有问题（失败 / 到点没跑 / 存在告警文件）
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATE = ROOT / "state"
STATUS = STATE / "last_run.json"
ALERT = STATE / "ALERT.txt"
# 数据体检告警（由 tools/refresh_data.py 写）。
# ⚠️ 与 ALERT.txt **分开**：ALERT 会被 daily_job 的「成功」路径清除，
#    而数据问题不该因为「下单成功」就被掩盖。
DATA_ALERT = STATE / "DATA_ALERT.txt"

# 计划任务设定：周一~周五 14:50，留出运行时间，15:10 之后判定"今天还没跑"
RUN_HHMM = (14, 50)
GRACE_MIN = 20


def _load_status():
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except Exception:
        return None


def main():
    from dataprovider.calendar import is_trading_day

    now = datetime.now()
    problems = []
    notes = []

    st = _load_status()
    if st is None:
        problems.append("从未产生运行记录（state/last_run.json 不存在）—— 自动交易可能一次都没跑过")
    else:
        notes.append("最近一次运行：{}  退出码={}  {}".format(
            st.get("ts"), st.get("exit_code"), st.get("summary") or ""))
        if not st.get("ok"):
            problems.append("最近一次运行失败（退出码 {}）".format(st.get("exit_code")))

        # 到点没跑：今天是交易日、且已过 14:50+宽限，但最后一次运行不是今天
        today = date.today()
        due = now.hour * 60 + now.minute >= (RUN_HHMM[0] * 60 + RUN_HHMM[1] + GRACE_MIN)
        try:
            trading = is_trading_day(today)
        except Exception as e:
            trading = False
            notes.append("交易日历不可用（{}），跳过「到点没跑」判定".format(e))
        if trading and due:
            if str(st.get("date")) != today.strftime("%Y%m%d"):
                problems.append(
                    "今天是交易日且已过 {}:{}，但今天没有运行记录（最近一次是 {}）"
                    "—— 计划任务可能没触发".format(
                        RUN_HHMM[0], RUN_HHMM[1], st.get("ts")))

    if ALERT.exists():
        problems.append("存在告警文件 state/ALERT.txt")
        try:
            notes.append(ALERT.read_text(encoding="utf-8", errors="ignore")[:1500])
        except Exception:
            pass

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
        return 1
    print("\n[正常] 最近一次运行成功，且今天没有到点未跑的情况。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
