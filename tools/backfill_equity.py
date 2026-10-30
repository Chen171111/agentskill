"""补齐 equity 净值缺口（仅限「持仓与现金都未变」的区间）。

为什么需要（2026-10-23 实际缺口）
--------------------------------
运行失败的那天什么都不会写，于是净值**永久缺一行**：
    20261020 已有 / 20261021 缺失（10-22 对账失败）/ 20261022 已有
缺口会让净値曲线断裂、回撤熔断与波动率目标拿到错误的历史序列，
而且**之后任何一次正常运行都不会自动补上**。所以需要这个显式工具。

安全边界（很重要）
------------------
只补「最后一次成交日之后、已记录净值日之前」的交易日。这段区间内持仓、
现金都没变，才可以用「当前持仓 × 当日收盘价 + 现金」复算。
任何跨过成交日的缺口一律拒绝（那时持仓已不同，复算必然错）。

写入前有一道**自校验闸门**：先用同一套算法复算「已存在的日期」，
与账本记录逐位比对（容差 1 分钱）。对不上就整体放弃 —— 说明持仓/现金
口径已经不一致，此时补数等于往账本里灌脏数据。

幂等：日期已存在则跳过；`--apply` 之外一律只打印不写。

用法
----
    E:\\Python\\python.exe tools/backfill_equity.py            # 只看，不写
    E:\\Python\\python.exe tools/backfill_equity.py --apply    # 真正写入
    E:\\Python\\python.exe tools/backfill_equity.py --date 20261021 --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from dataprovider.store import DataStore  # noqa: E402
from storage.db import TradeDB  # noqa: E402

TOL = 0.01          # 自校验容差（元）


def _fmt(d: str) -> str:
    return "%s-%s-%s" % (d[:4], d[4:6], d[6:8])


def _last_fill_date(db):
    """最后一次成交日期 YYYYMMDD（无成交返回 None）。"""
    with db._conn() as c:
        row = c.execute(
            "SELECT ts FROM orders WHERE status='filled' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return str(row["ts"])[:10].replace("-", "")


def _equity_rows(db):
    """{date: (cash, mv, total, frozen)}。"""
    with db._conn() as c:
        rows = c.execute(
            "SELECT date, cash, market_value, total, frozen FROM equity").fetchall()
    return {str(r["date"]): (float(r["cash"]), float(r["market_value"]),
                             float(r["total"]), float(r["frozen"] or 0.0))
            for r in rows}


def _mv_on(store, pos, date):
    """Σ(数量 × 当日收盘价)，PIT 截断（只看当日及之前）。"""
    mv, detail = 0.0, []
    for code, p in pos.items():
        df = store.read(code, end=date)
        if df.empty:
            raise RuntimeError("%s 在 %s 及之前没有任何行情" % (code, date))
        px = float(df["close"].iloc[-1])
        mv += p["qty"] * px
        detail.append("%s=%.3f" % (code[:6], px))
    return mv, detail


def main(argv=None):
    ap = argparse.ArgumentParser(description="补齐 equity 净值缺口（只用于持仓未变的区间）")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只打印）")
    ap.add_argument("--date", action="append", metavar="YYYYMMDD",
                    help="只补指定日期（可重复）；默认自动检测缺口")
    args = ap.parse_args(argv)

    db = TradeDB()
    equity = _equity_rows(db)
    if not equity:
        print("equity 表为空，无从补起")
        return 2

    pos = {k: v for k, v in db.load_positions().items() if v.get("qty", 0) > 0}
    if not pos:
        print("当前无持仓，不能用「持仓 × 收盘价」复算（空仓应走别的路径）")
        return 2

    last_fill = _last_fill_date(db)
    if not last_fill:
        print("找不到成交记录，无法界定「持仓未变」的起点，拒绝执行")
        return 2

    # 现金口径：最后一次成交之后现金不变 → 取最新一条净值记录的现金
    cash = equity[max(equity)][0]
    frozen = equity[max(equity)][3]
    if frozen > TOL:
        # 有未成交委托时「持仓/现金未变」的前提不成立：冻结资金随时会变成持仓，
        # 用当前持仓复算过去的日期必然错。
        print("拒绝：最新净值行有冻结资金 %.2f（= 有未成交委托），"
              "「持仓/现金未变」前提不成立" % frozen)
        return 2

    store = DataStore()
    # 交易日历：用第一只持仓标的的日期序列（持仓标的必有数据）
    ref = sorted(pos)[0]
    tf = store.read(ref, start=_fmt(last_fill).replace("-", ""), end=_fmt(max(equity)).replace("-", ""))
    trade_dates = [str(d) for d in tf.index]

    # 只在「最后成交日之后」的区间补 —— 跨过成交日的持仓已不同，复算必错
    candidates = [d for d in trade_dates if d > last_fill]
    if args.date:
        want = [d for d in args.date if d not in equity]
        bad = [d for d in args.date if d not in candidates]
        if bad:
            print("拒绝：%s 不在「持仓未变」区间（须 > 最后成交日 %s 且在已记录净值日之前）"
                  % (",".join(bad), last_fill))
            return 2
        gaps = want
    else:
        gaps = [d for d in candidates if d not in equity and d <= max(equity)]

    print("最后成交日     = %s" % last_fill)
    print("当前持仓       = %s" % ", ".join("%s×%d" % (k, v["qty"]) for k, v in sorted(pos.items())))
    print("现金口径       = %.2f" % cash)
    print("已记录净值区间 = %s ~ %s（%d 行）" % (min(equity), max(equity), len(equity)))
    print()

    # ---- 自校验闸门：用同一算法复算已存在的日期，必须与账本一致 ----
    import pandas as pd
    check_dates = [d for d in sorted(equity) if d > last_fill][-3:]
    if not check_dates:
        check_dates = [max(equity)]
    print("自校验（复算已有日期 vs 账本）:")
    failed = False
    for d in check_dates:
        mv, _detail = _mv_on(store, pos, d)
        total = cash + mv
        rec = equity[d][2]
        diff = abs(total - rec)
        ok = diff <= TOL
        failed = failed or not ok
        print("   %s  复算=%.2f  账本=%.2f  差=%.4f  %s"
              % (d, total, rec, diff, "OK" if ok else "✗ 不一致"))
    if failed:
        print("\n✗ 自校验未通过：持仓/现金口径与账本对不上，"
              "此时补数只会灌脏数据，已放弃。请人工核对后再跑。")
        return 3

    if not gaps:
        print("\n没有缺口，无需补齐。")
        return 0

    print("\n待补日期:")
    rows = []
    for d in gaps:
        mv, detail = _mv_on(store, pos, d)
        total = cash + mv
        rows.append((d, cash, mv, total))
        print("   %s  现金=%.2f  市值=%.2f  总资产=%.2f   (%s)"
              % (d, cash, mv, total, " ".join(detail)))

    if not args.apply:
        print("\n[dry-run] 未写入。确认无误后加 --apply 执行。")
        return 0

    for d, c, mv, total in rows:
        db.save_equity(d, c, mv, total)
        print("已写入 %s  总资产=%.2f" % (d, total))
    print("\n完成，共补 %d 行。" % len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
