"""行情刷新 + **数据体检**（每日自动交易的前置步骤，**必须用 64 位 Python 运行**）。

背景
----
32 位 Python 没装 akshare，`DataStore.refresh()` 会静默跳过下载；
而操作同花顺又必须用 32 位 Python。所以每日流程必须拆成两步：
    ① 64 位 Python 刷行情 + 体检（本脚本）
    ② 32 位 Python 下单（main.py simulate --ths）

为什么必须挂体检（2026-09-16 的真实事故）
------------------------------------------
09-13 修好的 4 处 ETF 份额折算，**被 09-16 的刷新静默回退** ——
`refresh()` 用 akshare 的原始未复权全量历史覆盖了 CSV。
当时**没有任何机制发现这件事**，是靠人偶然翻目录才查到的。

现在有两道防线：
1. **读取时自动复权**（`dataprovider/adjust.py`，接在 `DataStore.read()` 里）
   → 即使文件被覆盖，策略读到的永远连续。**这才是根治。**
2. **本脚本的体检**（第二道）→ 复权后仍有残留异常 = 真问题，写 `state/DATA_ALERT.txt`。

检查项
------
| # | 检查 | 判据 | 不通过的后果 |
|---|---|---|---|
| 1 | **数据新鲜度** | 每只最新日期 ≥ 最近已收盘交易日 | 下单会被陈旧度守卫**拒单** |
| 2 | **复权后残留跳变** | `|日收益| > 20%` = 0 处 | 回测/信号被假涨跌污染 |
| 3 | **价格有效性** | `close > 0`、无重复日期 | 引擎算出垃圾值 |
| 4 | 份额折算自动复权 | 记录本次修了几处（**正常事件，只留痕不告警**） | — |

用法
----
    E:\\Python\\python.exe tools/refresh_data.py            # 默认池 ETF全球
    E:\\Python\\python.exe tools/refresh_data.py ETF稳健

退出码：0 = 正常；1 = 刷新失败；2 = 池名错误；3 = **体检不通过（见 state/DATA_ALERT.txt）**
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from dataprovider.adjust import scan_jumps
from dataprovider.store import DataStore

STATE = Path(__file__).resolve().parent.parent / "state"
DATA_ALERT = STATE / "DATA_ALERT.txt"
SPLIT_LOG = STATE / "split_repairs.log"

FRESH_HINT = "数据陈旧 → 下单会被 scheduler/runner.py 的陈旧度守卫拒单"


def _split_log_lines() -> int:
    try:
        return len(SPLIT_LOG.read_text(encoding="utf-8", errors="ignore").splitlines())
    except Exception:
        return 0


def health_check(s: DataStore, codes, pool: str) -> tuple:
    """返回 (problems, notes)。problems 非空 → 写告警并返回退出码 3。"""
    from dataprovider.calendar import latest_closed_trading_day

    problems, notes = [], []
    try:
        target = latest_closed_trading_day(None)
    except Exception as e:
        notes.append("交易日历不可用（{}），跳过新鲜度判定".format(e))
        target = None

    stale, residual, bad = [], [], []
    for c in codes:
        try:
            df = s.read(c)                      # 已自动复权
        except Exception as e:
            bad.append("{}: 读取失败 {}".format(c, e))
            continue
        if df.empty:
            bad.append("{}: 空文件".format(c))
            continue
        last = str(df.index[-1])
        if target and last < target:
            stale.append("{} 最新 {} < 应有 {}".format(c, last, target))
        # 复权后仍有超阈跳变 → 真问题（复权逻辑没修好，或不是份额折算）
        for dt, pc, cl, ret in scan_jumps(df):
            residual.append("{} {} {:.3f}->{:.3f} ({:+.1%})".format(c, dt, pc, cl, ret))
        if float(df["close"].min()) <= 0:
            bad.append("{}: 存在 close<=0".format(c))
        if df.index.duplicated().any():
            bad.append("{}: 存在重复日期".format(c))

    if stale:
        problems.append("【数据陈旧】{} 只（{}）".format(len(stale), FRESH_HINT))
        problems += ["    " + x for x in stale[:11]]
    if residual:
        problems.append("【复权后仍有异常跳变】{} 处 —— 复权逻辑未覆盖，需人工核查"
                        .format(len(residual)))
        problems += ["    " + x for x in residual[:11]]
    if bad:
        problems.append("【数据损坏】{} 处".format(len(bad)))
        problems += ["    " + x for x in bad[:11]]

    notes.append("体检：{} 只标的，新鲜度基准 {}".format(len(codes), target))
    return problems, notes


def main():
    pool = sys.argv[1] if len(sys.argv) > 1 else "ETF全球"
    codes = list(config.RECOMMENDED_POOLS.get(pool) or [])
    if not codes:
        print("[refresh] 未知的池名：{!r}（可选：{}）".format(
            pool, ", ".join(config.RECOMMENDED_POOLS)))
        return 2

    s = DataStore()
    n_log_before = _split_log_lines()

    try:
        s.ensure(codes)
    except Exception as e:
        print("[refresh] ensure 异常（继续尝试 refresh）：{}".format(e))

    try:
        done = s.refresh(codes)
    except Exception as e:
        print("[refresh] 刷新失败：{}".format(e))
        return 1

    latest = {}
    for c in codes:
        try:
            df = s.read(c)
            latest[c] = str(df.index[-1])
        except Exception:
            latest[c] = "N/A"
    dates = sorted(set(latest.values()))
    print("[refresh] 池={} 刷新 {} 只；最新交易日 {}".format(pool, len(done), dates))
    for c in codes:
        print("    {:12s} {}".format(c, latest[c]))

    # ---- 份额折算复权留痕（正常事件，只报告不告警）----
    n_new = _split_log_lines() - n_log_before
    if n_new > 0:
        print("[refresh] ⓘ 本次读取时自动复权了 {} 处份额折算（明细见 state/split_repairs.log）"
              .format(n_new))

    # ---- 体检 ----
    problems, notes = health_check(s, codes, pool)
    for n in notes:
        print("[refresh] {}".format(n))

    if problems:
        STATE.mkdir(parents=True, exist_ok=True)
        body = ["=" * 60,
                "【行情数据体检不通过】",
                "=" * 60,
                "时间   : {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                "池     : {}".format(pool),
                ""]
        body += problems
        body += ["",
                 "⚠️ 数据有问题时**不要继续下单**（陈旧数据会被 runner 的守卫拒单）。",
                 "排查：state/daily_run.log ｜ 复权明细 state/split_repairs.log",
                 "=" * 60]
        text = "\n".join(body)
        try:
            DATA_ALERT.write_text(text, encoding="utf-8")
        except Exception as e:
            print("[refresh] ⚠️ 告警文件写入失败：{}".format(e))
        print("\n" + text)
        return 3

    # 体检通过 → 清掉上一次的数据告警（与 daily_job 的 ALERT 分开，互不覆盖）
    try:
        if DATA_ALERT.exists():
            DATA_ALERT.unlink()
            print("[refresh] 上一次的数据告警已清除")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
