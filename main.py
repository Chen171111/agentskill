"""agentskill：自动化量化交易模型。

用法：
    python main.py backtest --strategy momentum --codes 000300.SH,399006.SZ --start 20190101 --end 20251231
    python main.py run       --strategy momentum --codes 000300.SH,399006.SZ   # 模拟盘单次运行
    python main.py daemon    --strategy momentum --codes 000300.SH,399006.SZ   # 每日定时自动运行
    python main.py status                                                      # 查看持仓/净值
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (RECOMMENDED_POOLS, DEFAULT_STRATEGY, DEFAULT_START, DEFAULT_END,
                    DEFAULT_TOP_K, DEFAULT_REBALANCE, ensure_dirs)
from analysis.metrics import format_metrics


def _codes(args) -> list:
    if args.pool == "个股动量":
        from dataprovider.store import fetch_index_cons_sample
        return fetch_index_cons_sample(n=20)
    if args.pool and args.pool in RECOMMENDED_POOLS:
        return list(RECOMMENDED_POOLS[args.pool])
    return [c.strip() for c in args.codes.split(",") if c.strip()]


def cmd_backtest(args):
    from pipeline import run_backtest
    codes = _codes(args)
    print("正在回测 {} / {} / {} ~ {}".format(args.strategy, ",".join(codes),
                                              args.start or DEFAULT_START,
                                              args.end or DEFAULT_END))
    out = run_backtest(
        codes, strategy=args.strategy, start=args.start, end=args.end,
        topk=args.topk, rebalance=args.rebalance, benchmark=args.benchmark,
        timing=args.timing, dd_circuit=args.dd_circuit, vol_target=args.vol_target,
    )
    print("\n===== 绩效指标 =====")
    print(format_metrics(out["metrics"]))
    print("\n区间: {} ~ {}".format(out["meta"]["start"], out["meta"]["end"]))
    return out


def cmd_run(args):
    from scheduler.runner import DailyRunner
    codes = _codes(args)
    runner = DailyRunner(codes, strategy=args.strategy, topk=args.topk,
                         rebalance=args.rebalance, timing=args.timing)
    result = runner.run_once()
    _print_run_result(result)


def cmd_daemon(args):
    from scheduler.runner import DailyRunner, Scheduler
    codes = _codes(args)
    runner = DailyRunner(codes, strategy=args.strategy, topk=args.topk,
                         rebalance=args.rebalance, timing=args.timing)
    Scheduler(runner).run_forever()


def cmd_status(args):
    from storage.db import TradeDB
    db = TradeDB()
    pos = db.load_positions()
    print("\n===== 当前持仓 =====")
    if not pos:
        print("（空仓）")
    for c, p in pos.items():
        print("  {}  数量={}  成本={:.4f}  峰值={:.4f}".format(c, p["qty"], p["cost"], p["peak"]))
    print("\n===== 最近订单 =====")
    for o in db.recent_orders(10):
        print("  {} {} {} @ {:.2f} ({})".format(o["ts"], o["side"], o["code"],
                                                 o["filled_price"] or o["price"], o["status"]))


def _print_run_result(result):
    if result.get("status") == "no_data":
        print("无有效数据")
        return
    print("\n===== 交易日 {} =====".format(result.get("date")))
    print("策略: {}".format(result.get("strategy")))
    print("\n目标权重:")
    for c, w in result.get("target_weights", {}).items():
        print("  {}  {:.2%}".format(c, w))
    print("\n订单:")
    if not result.get("orders"):
        print("  （无调仓）")
    for o in result.get("orders", []):
        print("  {} {} {}股 @ {:.2f} 手续费 {:.2f}".format(
            o["side"], o["code"], o["qty"], o["filled_price"], o["fee"]))
    acc = result.get("account", {})
    print("\n账户: 现金 {:.2f}  市值 {:.2f}  总资产 {:.2f}".format(
        acc.get("cash", 0), acc.get("market_value", 0), acc.get("total_equity", 0)))


def main():
    ensure_dirs()
    p = argparse.ArgumentParser(description="agentskill 自动化量化交易模型")
    sub = p.add_subparsers(dest="cmd", required=True)

    # 公共参数
    def add_common(sp):
        sp.add_argument("--strategy", default=DEFAULT_STRATEGY,
                        choices=["momentum", "mean_reversion", "cross_moving",
                                 "multifactor", "lianban_lead"])
        sp.add_argument("--codes", default="000300.SH,000905.SH,399006.SZ,000688.SH")
        sp.add_argument("--pool", default=None, help="推荐池名（覆盖 codes）")
        sp.add_argument("--topk", type=int, default=DEFAULT_TOP_K)
        sp.add_argument("--rebalance", type=int, default=DEFAULT_REBALANCE)

    sp = sub.add_parser("backtest", help="历史回测")
    add_common(sp)
    sp.add_argument("--start", default=DEFAULT_START)
    sp.add_argument("--end", default=DEFAULT_END)
    sp.add_argument("--benchmark", default=None)
    sp.add_argument("--timing", default=None, choices=[None, "ma20", "abs_mom", "rsrs"])
    sp.add_argument("--dd-circuit", action="store_true", help="回撤熔断（组合回撤分档降仓）")
    sp.add_argument("--vol-target", type=float, default=None,
                    help="波动率目标仓位（如 0.15 表示年化波动目标15%%）")
    sp.set_defaults(func=cmd_backtest)

    sp = sub.add_parser("run", help="模拟盘单次运行")
    add_common(sp)
    sp.add_argument("--timing", default=None, choices=[None, "ma20", "abs_mom", "rsrs"])
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("daemon", help="每日定时自动运行")
    add_common(sp)
    sp.add_argument("--timing", default=None, choices=[None, "ma20", "abs_mom", "rsrs"])
    sp.set_defaults(func=cmd_daemon)

    sp = sub.add_parser("status", help="查看持仓/订单")
    sp.set_defaults(func=cmd_status)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()