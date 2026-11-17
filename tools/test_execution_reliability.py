"""Order journaling, budgets and runner phase behavior using temporary ledgers."""
from __future__ import annotations
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
import types
import hashlib
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config
from account.portfolio import PortfolioAccount
from dataprovider.panel import Panel
from scheduler.runner import DailyRunner
from storage.db import TradeDB
from trader.broker import Order, PaperBroker
from trader.execution import ExecutionEngine


class FakeBroker:
    def __init__(self, status="submitted", post_ok=True, interrupt_at=None):
        self.status, self.post_ok, self.interrupt_at = status, post_ok, interrupt_at
        self.calls, self.reconciles = [], 0

    def update_quotes(self, prices):
        self.quotes = prices

    def reconcile(self, account, prices=None):
        self.reconciles += 1
        return self.reconciles == 1 or self.post_ok

    def submit(self, order):
        if self.interrupt_at == len(self.calls) + 1:
            raise KeyboardInterrupt("synthetic interruption")
        order.status = self.status
        if self.status == "submitted":
            order.broker_order_id = "test-contract-" + str(len(self.calls) + 1)
        self.calls.append(order)
        return order


def panel(codes=("510300.SH",)):
    idx = pd.bdate_range("2024-01-01", periods=280).strftime("%Y%m%d")
    close = pd.DataFrame({c: np.linspace(90., 120., 280) for c in codes}, index=idx)
    return Panel({"open": close, "close": close, "raw_close": close, "raw_open": close,
                  "high": close, "low": close, "volume": close * 0 + 1e6,
                  "rate": close.pct_change()}, codes, {c: "etf" for c in codes})


@contextlib.contextmanager
def runner_fixture(folder, data):
    fake_store = types.SimpleNamespace(ensure=lambda _: None, refresh=lambda _: None)
    with patch.object(config, "DB_PATH", Path(folder) / "ledger.db"), \
         patch.object(config, "SHADOW_ENABLED", False), \
         patch("scheduler.runner.DataStore", return_value=fake_store), \
         patch("scheduler.runner.build_panel", return_value=data), \
         patch("dataprovider.calendar.latest_closed_trading_day", return_value=data.dates[-1]):
        yield


class ExecutionTests(unittest.TestCase):
    def test_explicit_rejection_survives_broker_exception(self):
        class RefusalBroker(FakeBroker):
            def submit(self, order):
                order.status = "rejected"
                raise RuntimeError("confirmation refused before commit")
        orders = ExecutionEngine(RefusalBroker()).rebalance(
            PortfolioAccount(init_cash=10000), {"510300.SH": .5}, {"510300.SH": 10.})
        self.assertEqual(orders[0].status, "rejected")

    def test_previous_day_order_never_matches_reused_today_contract(self):
        from datetime import datetime
        from tools.trading_session import SHANGHAI
        data = panel()
        with tempfile.TemporaryDirectory() as tmp, runner_fixture(tmp, data), \
             patch("scheduler.runner.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 17, 13, tzinfo=SHANGHAI)
            broker = FakeBroker()
            broker.orders_valid_for_day = True
            broker.fetch_today_orders = lambda: [{"broker_order_id": "REUSED"}]
            broker.sync_fill = lambda *a, **k: self.fail("prior-day order matched today's rows")
            runner = DailyRunner(data.codes, broker=broker)
            order = Order("510300.SH", "buy", 100, 10.)
            order.status, order.broker_order_id = "submitted", "REUSED"
            order.submitted_at = "2026-11-16T10:00:00+08:00"
            runner.db.save_order(order)
            self.assertEqual(runner._recover_pending({}), [])
            row = runner.db.recent_orders()[0]
            self.assertEqual(row["status"], "expired_unknown")
            self.assertEqual(row["filled_qty"], 0)

    def test_submission_day_is_converted_to_shanghai_before_expiry(self):
        from datetime import datetime
        from tools.trading_session import SHANGHAI
        data = panel()
        with tempfile.TemporaryDirectory() as tmp, runner_fixture(tmp, data), \
             patch("scheduler.runner.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 17, 13, tzinfo=SHANGHAI)
            broker = FakeBroker()
            broker.orders_valid_for_day = True
            broker.sync_fill = lambda *a, **k: None
            runner = DailyRunner(data.codes, broker=broker)
            order = Order("510300.SH", "buy", 100, 10.)
            order.status = "submitted"
            order.submitted_at = "2026-11-16T23:30:00-05:00"
            runner.db.save_order(order)
            self.assertEqual(len(runner._recover_pending({})), 1)
            self.assertEqual(runner.db.recent_orders()[0]["status"], "submitted")

    def test_runner_eligibility_uses_unrounded_factor_values(self):
        data = panel(("510300.SH", "510500.SH"))
        momentum = pd.DataFrame({"510300.SH": 1e-8, "510500.SH": -1e-8}, index=data.dates)
        with tempfile.TemporaryDirectory() as tmp, runner_fixture(tmp, data), \
             patch("scheduler.runner.compute_factors", return_value={"momentum20": momentum}), \
             patch("scheduler.runner.RiskManager.filter_weights", side_effect=lambda w, *a, **k: dict(w)):
            broker = FakeBroker(status="rejected")
            runner = DailyRunner(data.codes, strategy="etf_rotation", rebalance=1, broker=broker)
            runner.strategy.generate_weights = lambda *a: {c: .5 for c in data.codes}
            result = runner.run_once(data.dates[-1])
            self.assertEqual([o.code for o in broker.calls], ["510300.SH"])
            self.assertEqual(result["final_weights"], {"510300.SH": .5})
            self.assertEqual([r["qualified"] for r in result["evaluation"]], [True, False])

    def test_runner_applies_shared_ma_timing_before_orders(self):
        data = panel()
        data.fields["close"].iloc[-20:, 0] = np.linspace(130., 80., 20)
        momentum = pd.DataFrame(1., index=data.dates, columns=data.codes)
        with tempfile.TemporaryDirectory() as tmp, runner_fixture(tmp, data), \
             patch("scheduler.runner.compute_factors", return_value={"momentum20": momentum}), \
             patch("scheduler.runner.RiskManager.filter_weights", side_effect=lambda w, *a, **k: dict(w)):
            runner = DailyRunner(data.codes, strategy="etf_rotation", rebalance=1,
                                 timing="ma20", broker=FakeBroker(status="rejected"))
            runner.strategy.generate_weights = lambda *a: {"510300.SH": .6}
            result = runner.run_once(data.dates[-1])
            self.assertEqual(result["final_weights"], {"510300.SH": .18})

    def test_deprecated_broker_cannot_submit(self):
        from trader.broker import ThsBroker
        broker = object.__new__(ThsBroker)
        broker._connected = True
        broker.user = types.SimpleNamespace(buy=lambda *a, **k: self.fail("legacy buy called"))
        with self.assertRaisesRegex(RuntimeError, "UiaThsBroker"):
            broker.submit(Order("510300.SH", "buy", 100, 10))

    def test_stop_reconcile_failure_preserves_ledger(self):
        data = panel()
        with tempfile.TemporaryDirectory() as tmp, runner_fixture(tmp, data):
            db = TradeDB()
            db.set_state("strategy_since", "0")
            db.save_positions({"510300.SH": {"qty": 100, "cost": 200., "peak": 200.,
                                             "mark": 120., "available": 100, "frozen": 0}})
            db.save_equity(data.dates[-2], 1000., 12000., 13000.)
            before_pos, before_equity = db.load_positions(), db.load_latest_equity()
            runner = DailyRunner(data.codes, strategy="etf_rotation", rebalance=5,
                                 broker=FakeBroker(post_ok=False))
            # A sale was submitted, then its account confirmation failed.  The
            # execution exception must preserve the last trusted account rows.
            with self.assertRaisesRegex(RuntimeError, "reconcile|对账"):
                runner.run_once(data.dates[-1])
            self.assertEqual(db.load_positions(), before_pos)
            self.assertEqual(db.load_latest_equity(), before_equity)
            self.assertEqual(db.get_state("strategy_since"), "0")
            self.assertEqual(len(db.recent_orders()), 1)
            self.assertEqual(db.recent_orders()[0]["side"], "sell")

    def test_real_connection_is_guarded_before_client_creation(self):
        import main
        calls = []
        def create(**kwargs):
            calls.append("created")
            return types.SimpleNamespace(connect=lambda: "synthetic broker")
        fake = types.SimpleNamespace(UiaThsBroker=create)
        args = types.SimpleNamespace(ths=True, dry=False, ths_exe="synthetic.exe")
        with patch.dict(sys.modules, {"trader.ths_uia": fake}), \
             patch("tools.trading_session.require_trading_session", side_effect=RuntimeError("closed")):
            with self.assertRaisesRegex(RuntimeError, "closed"):
                main._make_broker(args)
        self.assertEqual(calls, [])

    def test_cli_failed_run_exits_nonzero(self):
        import main
        args = types.SimpleNamespace(pool="ETF全球", strategy="etf_rotation", topk=5,
                                     rebalance=5, timing=None, ths=False)
        fake = types.SimpleNamespace(run_once=lambda: {"status": "failed", "error": "rejected"})
        with patch("scheduler.runner.DailyRunner", return_value=fake), \
             patch.object(main, "_print_run_result", return_value=None):
            with self.assertRaises(SystemExit) as exc:
                main.cmd_run(args)
        self.assertNotEqual(exc.exception.code, 0)

    def test_runner_dry_flag_never_calls_underlying_submit_or_writes_ledger(self):
        data = panel()
        with tempfile.TemporaryDirectory() as tmp, runner_fixture(tmp, data):
            db = TradeDB()
            db.set_state("strategy_since", "0")
            ledger = Path(db.db_path)
            before = hashlib.sha256(ledger.read_bytes()).hexdigest()
            broker = FakeBroker()
            runner = DailyRunner(data.codes, strategy="etf_rotation", rebalance=1,
                                 broker=broker, dry_run=True)
            result = runner.run_once(data.dates[-1])
            self.assertEqual(result["status"], "ok")
            self.assertEqual(broker.calls, [])
            self.assertEqual(hashlib.sha256(ledger.read_bytes()).hexdigest(), before)
            self.assertEqual(db.get_state("strategy_since"), "0")

    def test_runner_uses_raw_executable_price(self):
        data = panel()
        data.fields["close"] = data.fields["raw_close"] * 4
        broker = FakeBroker()
        with tempfile.TemporaryDirectory() as tmp, runner_fixture(tmp, data):
            runner = DailyRunner(data.codes, strategy="etf_rotation", rebalance=1, broker=broker)
            runner.run_once(data.dates[-1])
            self.assertTrue(broker.calls)
            self.assertEqual(broker.calls[0].price, 120.)

    def test_pending_intent_blocks_resubmission(self):
        data = panel()
        broker = FakeBroker()
        with tempfile.TemporaryDirectory() as tmp, runner_fixture(tmp, data):
            runner = DailyRunner(data.codes, strategy="etf_rotation", rebalance=1, broker=broker)
            self.assertEqual(runner.run_once(data.dates[-1])["status"], "pending")
            self.assertEqual(runner.run_once(data.dates[-1])["status"], "pending")
            self.assertEqual(len(broker.calls), 1)
    def test_unfilled_sale_cannot_fund_buy(self):
        account = PortfolioAccount(init_cash=0)
        account.positions = {"510300.SH": {"qty": 1000, "cost": 10., "peak": 10.}}
        orders = ExecutionEngine(FakeBroker()).rebalance(
            account, {"510500.SH": .3}, {"510300.SH": 10., "510500.SH": 10.})
        self.assertEqual([o.side for o in orders], ["sell"])

    def test_minimum_commission_cannot_make_paper_cash_negative(self):
        account = PortfolioAccount(init_cash=30)
        ExecutionEngine(PaperBroker()).rebalance(account, {"510300.SH": .5, "510500.SH": .5},
                                                 {"510300.SH": .1, "510500.SH": .1})
        self.assertGreaterEqual(account.cash, 0.)

    def test_sell_is_capped_by_available_shares(self):
        account = PortfolioAccount(init_cash=100)
        account.positions = {"510300.SH": {"qty": 1000, "available": 100,
                              "frozen": 900, "cost": 10., "peak": 10.}}
        orders = ExecutionEngine(PaperBroker()).rebalance(account, {}, {"510300.SH": 10.})
        self.assertEqual(orders[0].qty, 100)
        self.assertEqual(account.positions["510300.SH"]["qty"], 900)

    def test_contract_identity_survives_db_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = TradeDB(Path(tmp) / "ledger.db")
            order = Order("510300.SH", "buy", 100, 10)
            order.broker_order_id = "synthetic-contract-1001"
            db.save_order(order)
            self.assertEqual(db.recent_orders()[0].get("broker_order_id"), "synthetic-contract-1001")

    def test_available_and_frozen_survive_db_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = TradeDB(Path(tmp) / "ledger.db")
            db.save_positions({"510300.SH": {"qty": 1000, "cost": 10., "peak": 12.,
                                            "mark": 11., "available": 100, "frozen": 900}})
            result = db.load_positions()["510300.SH"]
            self.assertEqual(result.get("available"), 100)
            self.assertEqual(result.get("frozen"), 900)

    def test_all_rejected_orders_do_not_advance_phase(self):
        data = panel()
        with tempfile.TemporaryDirectory() as tmp, runner_fixture(tmp, data):
            runner = DailyRunner(data.codes, strategy="etf_rotation", rebalance=1,
                                 broker=FakeBroker(status="rejected"))
            result = runner.run_once(data.dates[-1])
            self.assertEqual(result["status"], "failed")
            self.assertIn(runner.db.get_state("strategy_since"), (None, "0"))

    def test_failed_post_trade_reconcile_does_not_advance_phase(self):
        data = panel()
        with tempfile.TemporaryDirectory() as tmp, runner_fixture(tmp, data):
            runner = DailyRunner(data.codes, strategy="etf_rotation", rebalance=1,
                                 broker=FakeBroker(post_ok=False))
            result = runner.run_once(data.dates[-1])
            self.assertEqual(result["status"], "failed")
            self.assertIn(runner.db.get_state("strategy_since"), (None, "0"))

    def test_completed_signal_is_processed_once(self):
        data = panel()
        with tempfile.TemporaryDirectory() as tmp, runner_fixture(tmp, data):
            first = DailyRunner(data.codes, strategy="etf_rotation", rebalance=5)
            first.run_once(data.dates[-1])
            second = DailyRunner(data.codes, strategy="etf_rotation", rebalance=5)
            result = second.run_once(data.dates[-1])
            self.assertEqual(second.db.get_state("strategy_since"), "1")
            self.assertEqual(result["status"], "already_processed")

    def test_interrupted_batch_has_intents_before_submission(self):
        data = panel(("510300.SH", "510500.SH"))
        with tempfile.TemporaryDirectory() as tmp, runner_fixture(tmp, data):
            runner = DailyRunner(data.codes, strategy="etf_rotation", rebalance=1,
                                 broker=FakeBroker(interrupt_at=2))
            with self.assertRaises(KeyboardInterrupt):
                runner.run_once(data.dates[-1])
            rows = runner.db.recent_orders(10)
            self.assertEqual(len(rows), 2)
            self.assertEqual({r["status"] for r in rows}, {"submitted", "unknown"})
            self.assertIn(runner.db.get_state("strategy_since"), (None, "0"))

    def test_peak_tracks_new_trusted_high(self):
        from account import portfolio
        helper = getattr(portfolio, "position_peak", None)
        self.assertIsNotNone(helper)
        self.assertEqual(helper({"qty": 100, "peak": 100}, 120, 100, qty=100), 120.)
        self.assertEqual(helper(None, 101, 100, qty=100), 101.)

    def test_ledger_lock_refuses_second_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = TradeDB(Path(tmp) / "ledger.db")
            self.assertTrue(callable(getattr(db, "run_lock", None)))
            with db.run_lock():
                with self.assertRaises(RuntimeError):
                    with TradeDB(Path(tmp) / "ledger.db").run_lock():
                        self.fail("second runner entered")

    def test_pending_orders_are_recoverable_without_limit_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = TradeDB(Path(tmp) / "ledger.db")
            self.assertTrue(callable(getattr(db, "unresolved_orders", None)))
            for i in range(25):
                order = Order("510300.SH", "buy", 100, 10)
                order.status = "unknown"
                db.save_order(order)
            self.assertEqual(len(db.unresolved_orders()), 25)


if __name__ == "__main__":
    unittest.main(verbosity=2)
