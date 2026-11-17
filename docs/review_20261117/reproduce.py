"""Offline review probes: synthetic data, temporary ledgers, no UI or live orders.

Exit 0 means the observed defects were reproduced, NOT that production passed.
Run with E:/Python/python.exe -B -X utf8 docs/review_20261117/reproduce.py.
"""
from __future__ import annotations

import ast
import contextlib
import copy
import io
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from account.portfolio import PortfolioAccount
from backtest.engine import BacktestEngine
from dataprovider.panel import Panel
from dataprovider.store import DataStore
from factors.engine import compute_factors
from factors.registry import _macd_hist
from risk.manager import RiskManager
from scheduler.runner import DailyRunner
from strategies.builtin import EtfRotationStrategy, MultiFactorStrategy
from storage.db import TradeDB
from trader.execution import ExecutionEngine
from trader.ths_parse import sanitize_pos, to_full_code


def make_panel(kind="up", codes=("510300.SH",)):
    idx = pd.bdate_range("2024-01-01", periods=280).strftime("%Y%m%d")
    values = np.linspace(90, 120, len(idx))
    if kind == "negative_momentum":
        values[-21:] = np.linspace(125, 121, 21)
    elif kind == "timing_off":
        values = np.linspace(90, 95, len(idx))
        values[-20:] = [95] + [110] * 18 + [100]
    close = pd.DataFrame({c: values.copy() for c in codes}, index=idx)
    return Panel({"close": close, "open": close.copy(), "high": close.copy(),
                  "low": close.copy(), "volume": close * 0 + 1_000_000,
                  "rate": close.pct_change()}, codes, {c: "etf" for c in codes})


class FakeBroker:
    def __init__(self, status="submitted", post_ok=True, interrupt_at=None):
        self.status, self.post_ok, self.interrupt_at = status, post_ok, interrupt_at
        self.calls = []
        self.reconciles = 0

    def update_quotes(self, prices):
        self.quotes = prices

    def reconcile(self, account, prices=None):
        self.reconciles += 1
        return True if self.reconciles == 1 else self.post_ok

    def submit(self, order):
        if self.interrupt_at == len(self.calls) + 1:
            raise KeyboardInterrupt("synthetic interruption during batch")
        order.status = self.status
        self.calls.append(order.to_dict())
        return order


@contextlib.contextmanager
def runner_env(panel, folder):
    store = types.SimpleNamespace(ensure=lambda _: None, refresh=lambda _: None)
    with patch.object(config, "DB_PATH", Path(folder) / "ledger.db"), \
         patch.object(config, "SHADOW_ENABLED", False), \
         patch("scheduler.runner.DataStore", return_value=store), \
         patch("scheduler.runner.build_panel", return_value=panel), \
         patch("dataprovider.calendar.latest_closed_trading_day", return_value=panel.dates[-1]):
        yield


def run_synthetic(panel, broker, folder, timing=None):
    with runner_env(panel, folder):
        runner = DailyRunner(panel.codes, strategy="etf_rotation", rebalance=1,
                             topk=5, timing=timing, broker=broker)
        result = runner.run_once(end=panel.dates[-1])
        return result, runner


def extract_methods(*names):
    """Execute unchanged method ASTs; never import Windows UI dependencies."""
    source = (ROOT / "trader/ths_uia.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == "UiaThsBroker")
    body = [copy.deepcopy(x) for x in cls.body
            if isinstance(x, ast.FunctionDef) and x.name in names]
    assert len(body) == len(names), "missing requested production method"
    env = {"sanitize_pos": sanitize_pos}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(ROOT / "trader/ths_uia.py"), "exec"), env)
    return env


def reconcile_fixture(price=120, empty=False):
    env = extract_methods("reconcile")
    rows = {} if empty else {"510300": {"qty": 100, "available": 100,
                                            "frozen": 0, "cost": 100., "price": price}}
    self = types.SimpleNamespace(
        fetch_balance=lambda: {"cash": 1000., "frozen": 0.,
                               "market_value": 0. if empty else 100 * price},
        fetch_position=Mock(return_value=rows),
        _pos_ok=lambda *args: True,
        _guess_full_code=to_full_code,
        _mk_price=lambda code, row, prices: prices.get(to_full_code(code), row["price"]))
    return env["reconcile"], self


def probe_empty_account():
    reconcile, self = reconcile_fixture(empty=True)
    account = PortfolioAccount()
    observed = reconcile(self, account, {})
    assert observed is False
    self.fetch_position.assert_not_called()
    return {"reconcile_ok": observed, "position_reader_called": False,
            "consequence": "legitimate all-cash balance rejected before position validation"}


def probe_peak():
    reconcile, self = reconcile_fixture(price=120)
    account = PortfolioAccount()
    account.positions = {"510300.SH": {"qty": 100, "cost": 100., "peak": 100., "mark": 100.}}
    assert reconcile(self, account, {"510300.SH": 120.})
    observed_peak = account.positions["510300.SH"]["peak"]
    actual_stops = RiskManager().stop_codes(account.positions, {"510300.SH": 101.})
    control = copy.deepcopy(account.positions)
    control["510300.SH"]["peak"] = 120.
    control_stops = RiskManager().stop_codes(control, {"510300.SH": 101.})
    assert observed_peak == 100. and not actual_stops and control_stops == {"510300.SH"}
    return {"cost": 100, "observed_high": 120, "saved_peak": observed_peak,
            "later_price": 101, "actual_stop": bool(actual_stops), "control_stop": bool(control_stops)}


def probe_hidden_balance():
    env = extract_methods("_read_static", "fetch_balance")
    env.update({"time": types.SimpleNamespace(sleep=lambda _: None),
                "win32gui": types.SimpleNamespace(GetWindowText=lambda h: "100" if h % 10 == 1 else "999"),
                "u32": types.SimpleNamespace(IsWindowVisible=lambda h: h % 10 == 1),
                "_MENUS": {"position": "current"}})
    for i, name in enumerate(("_BAL_TOTAL", "_BAL_CASH", "_BAL_BALANCE", "_BAL_FROZEN", "_BAL_WITHDRAW", "_BAL_MKTVAL")):
        env[name] = i + 1
    self = types.SimpleNamespace(_dismiss_popups=lambda: None, _switch=lambda _: None,
        _page_label=lambda: "current", _page_label_of=lambda h: "current" if h % 10 == 1 else "hidden",
        _find_id=lambda cid: [cid * 10 + 1, cid * 10 + 2], _f=float)
    self._read_static = types.MethodType(env["_read_static"], self)
    candidates = self._read_static(env["_BAL_CASH"])
    balance = env["fetch_balance"](self)
    assert candidates == ["100", "999"] and balance["cash"] == 999.
    return {"candidates_current_then_hidden": candidates, "expected_cash": 100, "observed_cash": balance["cash"]}


def probe_signal_divergence():
    panel = make_panel("negative_momentum")
    with tempfile.TemporaryDirectory() as tmp:
        live, _ = run_synthetic(panel, FakeBroker(), tmp)
    narrow = panel.slice(panel.dates[-2], panel.dates[-1])
    narrow.fields["close_full"] = panel.get("close")
    from scheduler.runner import DEFAULT_FACTORS
    factors = compute_factors(panel, DEFAULT_FACTORS)
    strategy = EtfRotationStrategy(rebalance_every=1)
    bt = BacktestEngine(narrow, factors, strategy, dd_circuit=False, vol_target=None)
    bt.run()
    assert live["selection"] and not live["final_weights"]
    assert bt.acc.positions.get("510300.SH", {}).get("qty", 0) > 0
    return {"live_momentum_percent": live["selection"][0]["momentum20"],
            "live_weights": live["final_weights"], "backtest_holds_etf": True}


def probe_rejected_success():
    panel = make_panel()
    with tempfile.TemporaryDirectory() as tmp:
        result, runner = run_synthetic(panel, FakeBroker(status="rejected"), tmp)
        phase = runner.db.get_state("strategy_since")
    assert result["orders"] and all(o["status"] == "rejected" for o in result["orders"])
    assert result["status"] == "ok" and phase == "1"
    return {"order_statuses": [o["status"] for o in result["orders"]],
            "run_status": result["status"], "advanced_phase": phase}


def probe_post_reconcile_success():
    with tempfile.TemporaryDirectory() as tmp:
        result, runner = run_synthetic(make_panel(), FakeBroker(post_ok=False), tmp)
        phase, equity = runner.db.get_state("strategy_since"), runner.db.load_latest_equity()
    assert result["status"] == "ok" and phase == "1" and equity is None
    return {"post_reconcile_ok": False, "run_status": result["status"],
            "advanced_phase": phase, "equity_written": equity is not None}


def probe_timing_ignored():
    panel = make_panel("timing_off")
    with tempfile.TemporaryDirectory() as tmp:
        live, _ = run_synthetic(panel, FakeBroker(), tmp, timing="bias")
    bt = BacktestEngine(panel, {}, EtfRotationStrategy(), timing="bias",
                        benchmark=panel.get("close")["510300.SH"])
    scale = bt._timing_scale(panel.dates[-1])
    assert scale == 0. and live["final_weights"].get("510300.SH") == 0.3
    return {"timing": "bias", "backtest_scale": scale, "live_weights": live["final_weights"]}


def probe_sale_budget():
    account = PortfolioAccount(init_cash=0)
    account.positions = {"510300.SH": {"qty": 1000, "cost": 10., "peak": 10.}}
    broker = FakeBroker()
    orders = ExecutionEngine(broker).rebalance(account, {"510500.SH": .3},
                                               {"510300.SH": 10., "510500.SH": 10.})
    buys = [o for o in orders if o.side == "buy"]
    assert account.cash == 0 and buys and buys[0].qty > 0
    return {"available_cash": account.cash, "sell_status": orders[0].status,
            "buy_notional_submitted": buys[0].qty * buys[0].price}


def probe_batch_persistence():
    panel = make_panel(codes=("510300.SH", "510500.SH"))
    broker = FakeBroker(interrupt_at=2)
    with tempfile.TemporaryDirectory() as tmp:
        interrupted = False
        with runner_env(panel, tmp):
            runner = DailyRunner(panel.codes, strategy="etf_rotation", rebalance=1, broker=broker)
            try:
                runner.run_once(end=panel.dates[-1])
            except KeyboardInterrupt:
                interrupted = True
            rows = runner.db.recent_orders(10)
    assert interrupted and len(broker.calls) == 1 and len(rows) == 0
    return {"orders_submitted_before_interruption": len(broker.calls), "orders_persisted": len(rows)}


def probe_within_window_lookahead():
    idx = pd.bdate_range("2024-01-01", periods=90).strftime("%Y%m%d")
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        for code, slope in (("510300.SH", .1), ("510500.SH", .08)):
            px = 100 + np.arange(len(idx)) * slope
            if code == "510300.SH":
                px[60:] *= .5
            pd.DataFrame({"date": idx, "open": px, "close": px,
                          "high": px, "low": px, "volume": 1e6}).to_csv(folder / (code + ".csv"), index=False)
        with patch.object(config, "STOCK_DIR", folder), patch.object(config, "INDEX_DIR", folder):
            store = DataStore()
            store.split_log = folder / "splits.log"
            picks, values = {}, {}
            for label, end in (("prefix", idx[45]), ("extended", idx[-1])):
                factor = pd.DataFrame({c: _macd_hist({"close": store.read(c, end=end)["close"]})
                                       for c in ("510300.SH", "510500.SH")})
                strategy = MultiFactorStrategy(topk=1, rebalance_every=1,
                                               specs=[("macd_hist", 1, 1.)])
                w = strategy.generate_weights(idx[45], {"macd_hist": factor}, None)
                picks[label] = next(iter(w))
                values[label] = {c: float(factor.loc[idx[45], c]) for c in factor.columns}
    assert picks["prefix"] != picks["extended"]
    return {"decision_date": str(idx[45]), "future_split_date": str(idx[60]),
            "selected": picks, "macd_hist": values}


def syntax_inventory():
    paths = sorted(ROOT.rglob("*.py"))
    failures = []
    for file in paths:
        try:
            compile(file.read_bytes(), str(file), "exec")
        except (SyntaxError, UnicodeError) as exc:
            failures.append({"path": str(file.relative_to(ROOT)), "error": str(exc)})
    assert not failures, failures
    return {"files_checked": len(paths), "failures": failures,
            "paths": [str(p.relative_to(ROOT)) for p in paths]}


def self_test():
    """Negative controls: a fixed behavior must invalidate a defect assertion."""
    rejected_mutations = 0
    with patch.object(RiskManager, "stop_codes", return_value=set()):
        try:
            probe_peak()
        except AssertionError:
            rejected_mutations += 1
    original_extract = extract_methods

    def fixed_balance(*names):
        env = original_extract(*names)
        if "fetch_balance" in names:
            env["fetch_balance"] = lambda self: {
                "cash": float(self._read_static(env["_BAL_CASH"])[0])}
        return env

    with patch.dict(globals(), {"extract_methods": fixed_balance}):
        try:
            probe_hidden_balance()
        except AssertionError:
            rejected_mutations += 1
    original_macd = _macd_hist
    with patch.dict(globals(), {"_macd_hist": lambda px: original_macd(px) / px["close"]}):
        try:
            probe_within_window_lookahead()
        except AssertionError:
            rejected_mutations += 1
    assert rejected_mutations == 3, rejected_mutations
    print("negative controls rejected: 3/3")


def main():
    if sys.argv[1:] == ["--self-test"]:
        self_test()
        return
    if sys.argv[1:]:
        raise SystemExit("Only --self-test is accepted")
    probes = [probe_empty_account, probe_hidden_balance, probe_peak,
              probe_signal_divergence, probe_rejected_success, probe_post_reconcile_success,
              probe_timing_ignored, probe_sale_budget, probe_batch_persistence,
              probe_within_window_lookahead]
    results = {}
    for probe in probes:
        with contextlib.redirect_stdout(io.StringIO()):
            results[probe.__name__] = probe()
        print(probe.__name__ + ": reproduced")
    results["syntax_inventory"] = syntax_inventory()
    output = Path(__file__).with_name("results.json")
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"defect_scenarios_reproduced": len(probes),
                      "total_defect_scenarios": len(probes),
                      "syntax_files": results["syntax_inventory"]["files_checked"],
                      "result_path": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
