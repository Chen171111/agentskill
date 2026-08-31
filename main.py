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
                    DEFAULT_TOP_K, DEFAULT_REBALANCE, DEFAULT_DD_CIRCUIT, ensure_dirs)
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
        strategy_params={"risk_parity": args.risk_parity} if args.risk_parity else None,
        stability_min_overlap=args.stability,
    )
    print("\n===== 绩效指标 =====")
    print(format_metrics(out["metrics"]))
    print("\n区间: {} ~ {}".format(out["meta"]["start"], out["meta"]["end"]))
    return out


def _make_broker(args):
    """根据命令行参数构造券商对象。--ths 表示接入同花顺客户端(含模拟炒股)。"""
    if getattr(args, "ths", None):
        from trader.broker import ThsBroker
        return ThsBroker(exe_path=getattr(args, "ths_exe", None)).connect()
    return None


def cmd_run(args):
    from scheduler.runner import DailyRunner
    codes = _codes(args)
    runner = DailyRunner(codes, strategy=args.strategy, topk=args.topk,
                         rebalance=args.rebalance, timing=args.timing,
                         broker=_make_broker(args))
    result = runner.run_once()
    _print_run_result(result)


def cmd_daemon(args):
    from scheduler.runner import DailyRunner, Scheduler
    codes = _codes(args)
    runner = DailyRunner(codes, strategy=args.strategy, topk=args.topk,
                         rebalance=args.rebalance, timing=args.timing,
                         broker=_make_broker(args))
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


def cmd_ths_check(args):
    """测试同花顺连接：连接客户端并读取资金/持仓。"""
    from trader.broker import ThsBroker
    print("正在测试同花顺连接（xiadan.exe）...")
    broker = ThsBroker(exe_path=args.ths_exe)
    try:
        broker.connect()
        print("[OK] 连接成功！")
    except Exception as e:
        print("[FAIL] 连接失败: {} {}".format(type(e).__name__, str(e)[:200]))
        return
    # 资金
    try:
        bal = broker.balance
        print("\n===== 资金状况 =====")
        b = bal[0] if isinstance(bal, list) and bal else bal
        if isinstance(b, dict):
            for k, v in b.items():
                print("  {}: {}".format(k, v))
        else:
            print(" ", bal)
    except Exception as e:
        print("[资金读取失败]", str(e)[:150])
    # 持仓
    try:
        pos = broker.position
        print("\n===== 持仓 =====")
        plist = pos if isinstance(pos, list) else ([pos] if pos else [])
        if not plist:
            print(" （空仓）")
        else:
            for p in plist:
                if isinstance(p, dict):
                    print("  {} {} 持仓{} 成本{} 现价{}".format(
                        p.get("证券代码", ""), p.get("证券名称", ""), p.get("当前持仓", ""),
                        p.get("参考成本价", ""), p.get("参考市价", "")))
                else:
                    print(" ", p)
    except Exception as e:
        print("[持仓读取失败]", str(e)[:150])


def cmd_simulate(args):
    """模拟交易：选股 → 评估 → 下单（默认本地模拟撮合，--ths 接同花顺模拟盘）。"""
    from scheduler.runner import DailyRunner
    from trader.broker import ThsBroker
    codes = _codes(args)
    broker = None
    if getattr(args, "ths", None):
        broker = ThsBroker(exe_path=getattr(args, "ths_exe", None)).connect()
    runner = DailyRunner(codes, strategy=args.strategy, topk=args.topk,
                         rebalance=args.rebalance, timing=args.timing, broker=broker)
    r = runner.run_once()
    if r.get("status") == "no_data":
        print("无有效数据")
        return

    print("\n===== 模拟交易日 {} =====".format(r.get("date")))
    print("策略: {}   标的数: {}".format(r.get("strategy"), len(codes)))

    print("\n【① 选股】策略候选（按目标权重排序）：")
    if not r.get("selection"):
        print("  （策略未选出任何标的）")
    for s in r.get("selection", []):
        mom = s.get("momentum20")
        mom_s = "{:+.2f}%".format(mom) if mom is not None else "   NA "
        print("  {:<8} {:<8} 目标权重 {:>6.2%}  20日动量 {}".format(
            s.get("name", ""), s.get("code"), s.get("weight"), mom_s))

    print("\n【② 评估】绝对动量质检（20日动量≤0 剔除）+ 风控：")
    for e in r.get("evaluation", []):
        tag = "√ 合格" if e.get("qualified") else "× 剔除"
        print("  [{}] {} ({}) {}".format(tag, e.get("name"), e.get("code"),
                                          e.get("reason", "")))

    print("\n【③ 下单】{}".format(
        "无合格标的 → 空仓" if r.get("empty") else "按风控后权重调仓"))
    orders = r.get("orders", [])
    if not orders:
        print("  （无订单）")
    for o in orders:
        side = "买入" if o.get("side") == "buy" else "卖出"
        px = o.get("filled_price") or o.get("price")
        print("  {} {} {}股 @ {:.3f}  [{}]".format(side, o.get("code"), o.get("qty"),
                                                    px, o.get("reason", "")))

    acc = r.get("account", {})
    print("\n账户：现金 {:.2f}  市值 {:.2f}  总资产 {:.2f}".format(
        acc.get("cash", 0), acc.get("market_value", 0), acc.get("total_equity", 0)))
    pos = acc.get("positions", [])
    if pos:
        print("当前持仓：")
        for p in pos:
            print("  {} {}股  成本 {:.3f}  现价 {:.3f}  浮盈 {:+.2%}".format(
                p.get("code"), p.get("qty"), p.get("cost", 0),
                p.get("price", 0), p.get("pct", 0)))


def cmd_reset(args):
    """清空模拟盘状态（持仓/订单/净值），从头开始。"""
    from pathlib import Path
    from config import DB_PATH
    db = Path(DB_PATH)
    if db.exists():
        db.unlink()
        print("[OK] 已清空模拟盘状态：{}".format(db))
    else:
        print("当前无模拟盘状态文件，无需重置。")


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
                        choices=["momentum", "etf_rotation", "mean_reversion", "cross_moving",
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
    sp.add_argument("--timing", default=None, choices=[None, "ma20", "abs_mom", "rsrs", "bias"])
    sp.add_argument("--dd-circuit", action=argparse.BooleanOptionalAction,
                    default=DEFAULT_DD_CIRCUIT, help="回撤熔断（默认开，--no-dd-circuit 关闭）")
    sp.add_argument("--vol-target", type=float, default=None,
                    help="波动率目标仓位，默认15%%（传 0 关闭）")
    sp.add_argument("--risk-parity", action="store_true", help="风险平价加权（按波动率倒数分配，替代等权）")
    sp.add_argument("--stability", type=float, default=None,
                    help="信号稳定性过滤：本期与上期TopK重叠度低于此值(0~1)则空仓，如 0.8")
    sp.set_defaults(func=cmd_backtest)

    sp = sub.add_parser("run", help="模拟盘单次运行")
    add_common(sp)
    sp.add_argument("--timing", default=None, choices=[None, "ma20", "abs_mom", "rsrs", "bias"])
    sp.add_argument("--ths", action="store_true", help="接入同花顺客户端(含模拟炒股)下单")
    sp.add_argument("--ths-exe", default=None, help="同花顺 xiadan.exe 路径，如 C:\\同花顺\\xiadan.exe")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("simulate", help="模拟交易：选股→评估→下单（默认 ETF全球+etf_rotation）")
    sp.add_argument("--strategy", default="etf_rotation",
                    choices=["momentum", "etf_rotation", "mean_reversion",
                             "cross_moving", "multifactor", "lianban_lead"])
    sp.add_argument("--pool", default="ETF全球", help="推荐池名（默认 ETF全球）")
    sp.add_argument("--codes", default="", help="自定义代码（覆盖 pool）")
    sp.add_argument("--topk", type=int, default=DEFAULT_TOP_K)
    sp.add_argument("--rebalance", type=int, default=DEFAULT_REBALANCE)
    sp.add_argument("--timing", default=None, choices=[None, "ma20", "abs_mom", "rsrs", "bias"])
    sp.add_argument("--ths", action="store_true", help="接入同花顺模拟盘下单")
    sp.add_argument("--ths-exe", default=None, help="同花顺 xiadan.exe 路径")
    sp.set_defaults(func=cmd_simulate)

    sp = sub.add_parser("daemon", help="每日定时自动运行")
    add_common(sp)
    sp.add_argument("--timing", default=None, choices=[None, "ma20", "abs_mom", "rsrs", "bias"])
    sp.add_argument("--ths", action="store_true", help="接入同花顺客户端(含模拟炒股)下单")
    sp.add_argument("--ths-exe", default=None, help="同花顺 xiadan.exe 路径，如 C:\\同花顺\\xiadan.exe")
    sp.set_defaults(func=cmd_daemon)

    sp = sub.add_parser("status", help="查看持仓/订单")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("reset", help="清空模拟盘状态（持仓/订单/净值），重新开始")
    sp.set_defaults(func=cmd_reset)

    sp = sub.add_parser("ths-check", help="测试同花顺连接并读取资金/持仓")
    sp.add_argument("--ths-exe", default=None, help="同花顺 xiadan.exe 路径，如 C:\\同花顺\\xiadan.exe")
    sp.set_defaults(func=cmd_ths_check)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()