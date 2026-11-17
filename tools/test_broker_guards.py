# -*- coding: utf-8 -*-
"""Offline broker safety regressions; only Windows/UI boundaries are replaced."""
import ctypes
import importlib.util
import math
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from account.portfolio import PortfolioAccount
from trader.broker import Order
from trader.ths_parse import match_order_row, parse_confirm_text, parse_trades


def _load_ui_modules():
    # Import the real Python implementations without calling any desktop API.
    gui = types.SimpleNamespace()
    con = types.SimpleNamespace(VK_F1=112, VK_F2=113, VK_F3=114, VK_F4=115,
                               GW_CHILD=5, GW_HWNDNEXT=2, SW_RESTORE=9, SW_SHOW=5)
    process = types.SimpleNamespace()
    user32 = types.SimpleNamespace(SetProcessDPIAware=lambda: None)
    stubs = {'win32gui': gui, 'win32con': con, 'win32process': process,
             'pywinauto': types.SimpleNamespace(Desktop=object)}
    loaded = []
    with patch.dict(sys.modules, stubs), \
         patch.object(ctypes, 'windll', types.SimpleNamespace(user32=user32), create=True), \
         patch.object(ctypes, 'WinDLL', lambda *a, **k: object(), create=True):
        for name, file in [('trader._guard_test_ths_uia', 'trader/ths_uia.py'),
                           ('tools._guard_test_trade_up', 'tools/ths_trade_up.py')]:
            spec = importlib.util.spec_from_file_location(name, str(ROOT / file))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            loaded.append(mod)
    return loaded


ths, up = _load_ui_modules()


def broker():
    b = ths.UiaThsBroker()
    b._dismiss_popups = lambda: None
    b._switch = lambda key: True
    b._page_label = lambda: '资金股票'
    b._dump_pos_failure = lambda *a: None
    return b


def held(price=120.0, qty=100):
    return {'510300': {'qty': qty, 'available': qty, 'frozen': 0,
                       'cost': 100.0, 'price': price}}


def row(contract='6285045859', status='已报', fill=0, side='买入', qty=100):
    return {'code': '510300', 'name': 'ETF', 'side': side, 'qty': qty,
            'filled_qty': fill, 'price': 10.0, 'avg_price': 9.9 if fill else 0.0,
            'status': status, 'deal_id': contract, 'broker_order_id': contract}


class BalanceTests(unittest.TestCase):
    # Catches taking the last stale control or trusting hidden-only controls.
    def balance_for(self, values):
        b = broker()
        b._find_id = lambda cid: list(values)
        b._page_label_of = lambda h: values[h][1]
        with patch.object(ths.win32gui, 'GetWindowText', lambda h: values[h][0], create=True), \
             patch.object(ths.u32, 'IsWindowVisible', lambda h: values[h][2], create=True), \
             patch.object(ths.time, 'sleep', lambda _: None):
            return b.fetch_balance()

    def test_current_visible_control_beats_stale_hidden(self):
        self.assertEqual(self.balance_for({1: ('100', '资金股票', True),
                                          2: ('999', '买入股票', False)})['cash'], 100.0)

    def test_first_visible_control_beats_later_hidden(self):
        self.assertEqual(self.balance_for({1: ('100', '其他页', True),
                                          2: ('999', '旧页', False)})['cash'], 100.0)

    def test_hidden_current_label_is_not_trusted(self):
        self.assertIsNone(self.balance_for({1: ('999', '资金股票', False)})['cash'])

    def test_empty_controls_return_missing(self):
        self.assertIsNone(self.balance_for({1: ('', '资金股票', True)})['cash'])

    def test_nonfinite_cash_is_not_returned_as_a_price(self):
        self.assertIsNone(self.balance_for({1: ('nan', '资金股票', True)})['cash'])


class ReconcileTests(unittest.TestCase):
    def reconcile(self, balance, positions, prices=None, account=None):
        b = broker()
        b.fetch_balance = lambda: balance
        b.fetch_position = lambda validator: positions
        a = account or PortfolioAccount(500)
        ok = b.reconcile(a, prices or {})
        return ok, a

    # Catches cash-only rejection and invalid readback mutating the ledger.
    def test_verified_cash_only_account_is_accepted_and_clears_old_positions(self):
        a = PortfolioAccount(500)
        a.positions = {'510300.SH': {'qty': 100, 'cost': 10, 'peak': 20}}
        ok, a = self.reconcile({'cash': 1234.0, 'market_value': 0.0, 'total': 1234.0,
                                'frozen': 0.0}, {}, account=a)
        self.assertTrue(ok)
        self.assertEqual((a.cash, a.positions), (1234.0, {}))

    def test_empty_read_requires_total_crosscheck(self):
        for total in [None, float('nan'), float('inf'), 2000.0]:
            with self.subTest(total=total):
                ok, a = self.reconcile({'cash': 1000.0, 'market_value': 0.0,
                                        'total': total, 'frozen': 0.0}, {})
                self.assertFalse(ok)
                self.assertEqual(a.cash, 500.0)

    def test_empty_read_rejects_nonfinite_or_negative_balances(self):
        good = {'cash': 1000.0, 'market_value': 0.0, 'total': 1000.0, 'frozen': 0.0}
        for key, value in [('cash', float('nan')), ('cash', -1),
                           ('market_value', None), ('market_value', float('nan')),
                           ('market_value', -1), ('frozen', float('inf'))]:
            with self.subTest(key=key, value=value):
                self.assertFalse(self.reconcile(dict(good, **{key: value}), {})[0])

    def test_zero_market_value_rejects_nonempty_position_table(self):
        self.assertFalse(self.reconcile({'cash': 1000.0, 'market_value': 0.0,
                                         'total': 1000.0, 'frozen': 0.0}, held())[0])

    def test_peak_tracks_trusted_current_price_then_preserves_it_on_decline(self):
        a = PortfolioAccount(500)
        a.positions = {'510300.SH': {'qty': 100, 'cost': 100, 'peak': 100}}
        ok, a = self.reconcile({'cash': 500.0, 'market_value': 12000.0,
                                'total': 12500.0, 'frozen': 0.0}, held(),
                               {'510300.SH': 120.0}, a)
        self.assertTrue(ok)
        self.assertEqual(a.positions['510300.SH']['peak'], 120.0)
        ok, a = self.reconcile({'cash': 500.0, 'market_value': 10100.0,
                                'total': 10600.0, 'frozen': 0.0}, held(101),
                               {'510300.SH': 101.0}, a)
        self.assertTrue(ok)
        self.assertEqual(a.positions['510300.SH']['peak'], 120.0)

    def test_peak_resets_after_full_exit_and_reentry(self):
        a = PortfolioAccount(500)
        a.positions = {'510300.SH': {'qty': 100, 'cost': 100, 'peak': 200}}
        ok, a = self.reconcile({'cash': 10000.0, 'market_value': 0.0,
                                'total': 10000.0, 'frozen': 0.0}, {}, account=a)
        self.assertTrue(ok)
        ok, a = self.reconcile({'cash': 0.0, 'market_value': 10000.0,
                                'total': 10000.0, 'frozen': 0.0}, held(100),
                               {'510300.SH': 100.0}, a)
        self.assertTrue(ok)
        self.assertEqual(a.positions['510300.SH']['peak'], 100.0)

    def test_no_trusted_quote_or_mark_cannot_use_cost_as_current_price(self):
        for bad in [None, 0, float('nan'), float('inf')]:
            with self.subTest(mark=bad):
                self.assertFalse(broker()._pos_ok(held(bad),
                    {'cash': 500, 'market_value': 10000, 'total': 10500},
                    {'510300.SH': float('nan')}))

    def test_nonfinite_quote_falls_back_to_validated_ocr_mark(self):
        ok, a = self.reconcile({'cash': 500.0, 'market_value': 12000.0,
                                'total': 12500.0, 'frozen': 0.0}, held(),
                               {'510300.SH': float('nan')})
        self.assertTrue(ok)
        self.assertEqual(a.positions['510300.SH']['peak'], 120.0)


class PositionEvidenceTests(unittest.TestCase):
    def read(self, raw):
        b = broker()
        b._read_grid_text = lambda: raw
        b._dump_position_frames = lambda frames: None
        with patch.object(ths.time, 'sleep', lambda _: None):
            return b.fetch_position(validator=lambda p: not p, tries=1)

    def test_blank_ocr_is_not_verified_empty_holdings(self):
        self.assertIsNone(self.read(''))
        self.assertIsNone(self.read('unreadable image'))

    def test_position_headers_verify_empty_holdings(self):
        self.assertEqual(self.read('证券代码 证券名称 股票余额 可用余额 冻结数量'), {})

    def test_failed_position_read_never_clears_account(self):
        b = broker()
        b.fetch_balance = lambda: {'cash': 1000, 'market_value': 0, 'total': 1000}
        b.fetch_position = lambda validator: None
        a = PortfolioAccount(500)
        a.positions = {'510300.SH': {'qty': 100, 'cost': 10, 'peak': 20}}
        self.assertFalse(b.reconcile(a, {}))
        self.assertEqual((a.cash, len(a.positions)), (500, 1))


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        guard = patch('tools.trading_session.require_trading_session', return_value=None)
        guard.start()
        self.addCleanup(guard.stop)

    def test_session_is_rechecked_at_final_confirmation_after_ui_wait(self):
        with patch('tools.trading_session.require_trading_session',
                   side_effect=RuntimeError('session closed during UI wait')):
            ok, _, clicked = self.confirm('证券代码：510300\n买入价格：10\n买入数量：100')
        self.assertFalse(ok)
        self.assertNotIn(ths._CONFIRM_YES, clicked)
        self.assertIn(ths._CONFIRM_NO, clicked)

    def confirm(self, text, market=False, price=10):
        b = broker()
        b.use_market_price = market
        b._read_confirm_text = lambda dlg: text
        clicked = []
        b._click_dialog_button = lambda dlg, prefixes: clicked.append(prefixes) or True
        o = Order('510300.SH', 'buy', 100, price)
        ok, _ = b._confirm_dialog(1, expect_code='510300', expect_qty=100, order=o)
        return ok, o, clicked

    def test_parser_retains_confirmed_side(self):
        self.assertEqual(parse_confirm_text('证券代码：510300\n卖出价格：10\n卖出数量：100').get('side'), 'sell')

    def test_confirmation_rejects_missing_or_conflicting_fields(self):
        for text in [
            '证券代码：510300\n买入价格：10',
            '证券代码：510300\n买入数量：100',
            '买入价格：10\n买入数量：100',
            '证券代码：510300\n卖出价格：10\n卖出数量：100',
            '证券代码：510500\n名称510300\n买入价格：10\n买入数量：100',
            '证券代码：510300\n买入价格：10\n卖出数量：100',
            '证券代码：510300\n买入价格：-10\n买入数量：100',
            '证券代码：510300\n买入价格：10\n买入数量：100.5',
        ]:
            with self.subTest(text=text):
                ok, o, clicked = self.confirm(text)
                self.assertFalse(ok)
                self.assertNotIn(ths._CONFIRM_YES, clicked)
                self.assertEqual(o.price, 10.0)

    def test_limit_price_mismatch_is_rejected(self):
        self.assertFalse(self.confirm('证券代码：510300\n买入价格：100\n买入数量：100')[0])

    def test_limit_price_allows_only_rounding_within_half_tick(self):
        self.assertTrue(self.confirm('证券代码：510300\n买入价格：10.0004\n买入数量：100')[0])
        self.assertFalse(self.confirm('证券代码：510300\n买入价格：10.001\n买入数量：100')[0])

    def test_market_mode_accepts_valid_live_price_and_records_it(self):
        ok, o, _ = self.confirm('证券代码：510300\n买入价格：100\n买入数量：100', market=True)
        self.assertTrue(ok)
        self.assertEqual(o.price, 100.0)

    def test_live_confirmation_cannot_exceed_cash_or_target_budget(self):
        for cash_limit, max_notional in [(1004, None), (2000, 999)]:
            with self.subTest(cash=cash_limit, target=max_notional):
                b = broker()
                b._read_confirm_text = lambda dlg: '证券代码：510300\n买入价格：10\n买入数量：100'
                clicks = []
                b._click_dialog_button = lambda dlg, p: clicks.append(p) or True
                o = Order('510300.SH', 'buy', 100, 9)
                o.cash_limit, o.max_notional = cash_limit, max_notional
                self.assertFalse(b._confirm_dialog(1, order=o)[0])
                self.assertNotIn(ths._CONFIRM_YES, clicks)

    def test_market_mode_rejects_nonfinite_zero_negative_prices(self):
        for price in ['nan', 'inf', '0', '-10', '1e309']:
            with self.subTest(price=price):
                self.assertFalse(self.confirm('证券代码：510300\n买入价格：'+price+'\n买入数量：100', market=True)[0])


class OrderReadbackTests(unittest.TestCase):
    def test_parser_preserves_long_contract_number_exactly(self):
        text = '14:50:03 510300 ETF 买入 已报 100 0 10 0 0 00062850458591234567 上海A股'
        r = parse_trades(text)[0]
        self.assertEqual(r['broker_order_id'], '00062850458591234567')

    def test_identity_match_cannot_override_conflicting_side_or_quantity(self):
        for r in [row(side='卖出'), row(qty=200)]:
            with self.subTest(row=r):
                self.assertIsNone(match_order_row([r], '510300.SH', 'buy', 100,
                                                  broker_order_id='6285045859'))

    def test_ambiguous_identical_orders_never_select_first_row(self):
        rows = [row('6285045858', '已成', 100), row()]
        self.assertIsNone(match_order_row(rows, '510300.SH', 'buy', 100))
        self.assertIsNone(match_order_row(rows[::-1], '510300.SH', 'buy', 100))

    def test_contract_id_selects_new_order_independent_of_row_order(self):
        rows = [row('6285045858', '已成', 100), row()]
        o = Order('510300.SH', 'buy', 100, 10)
        o.broker_order_id = '6285045859'
        broker().sync_fill(o, rows)
        self.assertEqual(o.status, 'submitted')
        self.assertEqual(o.filled_qty, 0)
        self.assertIsNone(o.filled_price)

    def test_unknown_submission_without_contract_never_steals_old_single_row(self):
        o = Order('510300.SH', 'buy', 100, 10)
        o.status = 'unknown'
        broker().sync_fill(o, [row(status='已成', fill=100)])
        self.assertEqual((o.status, o.filled_qty, o.filled_price), ('unknown', 0, None))

    def test_unknown_contract_never_falls_back_to_other_order(self):
        o = Order('510300.SH', 'buy', 100, 10)
        o.broker_order_id = '6285045999'
        o.status = 'unknown'
        broker().sync_fill(o, [row(status='已成', fill=100)])
        self.assertEqual((o.status, o.filled_qty, o.filled_price), ('unknown', 0, None))

    def test_parser_preserves_rejection_and_unknown_state(self):
        for state, expected in [('废单', '废单'), ('无法识别', 'unknown'), ('待撤', '待撤')]:
            with self.subTest(state=state):
                text = '14:50:03 510300 ETF 买入 '+state+' 100 0 10 0 0 6285045859 上海A股'
                self.assertEqual(parse_trades(text)[0]['status'], expected)

    def test_readback_preserves_terminal_and_partial_states(self):
        for state, filled, expected in [('已撤', 0, 'cancelled'), ('废单', 0, 'rejected'),
                                       ('部分成交', 30, 'partially_filled'),
                                       ('unknown', 0, 'unknown'), ('已成', 100, 'filled')]:
            with self.subTest(state=state):
                o = Order('510300.SH', 'buy', 100, 10)
                o.broker_order_id = '6285045859'
                broker().sync_fill(o, [row(status=state, fill=filled)])
                self.assertEqual(o.status, expected)
                self.assertEqual(o.filled_qty, filled)
                self.assertEqual(o.filled_price, 9.9 if filled else None)

    def test_cancelled_order_keeps_its_partial_fill(self):
        o = Order('510300.SH', 'buy', 100, 10)
        o.broker_order_id = '6285045859'
        broker().sync_fill(o, [row(status='已撤', fill=30)])
        self.assertEqual((o.status, o.filled_qty, o.filled_price), ('cancelled', 30, 9.9))

    def test_submit_without_receipt_retains_unknown_result(self):
        b = broker()
        b._fill_order_form = lambda *a, **k: None
        b._ctrl = lambda *a, **k: 1
        b._bm_click = lambda h: None
        b._find_confirm_dialog = lambda: (2, [])
        b._confirm_dialog = lambda *a, **k: (True, 'confirmed')
        b._dismiss_info = lambda: False
        o = Order('510300.SH', 'buy', 100, 10)
        with patch.object(ths.win32gui, 'GetWindowText', lambda h: '买入', create=True), \
             patch.object(ths.time, 'sleep', lambda _: None):
            b._submit_page(o, 'buy')
        self.assertEqual(o.status, 'unknown')

    def test_receipt_contract_number_is_retained_before_dismissal(self):
        b = broker()
        b._owned_top_windows = lambda: [1]
        b._find_class = lambda cls, root=None: [2] if cls == 'Button' else [3]
        b._click_dialog_button = lambda *a: True
        with patch.object(ths.win32gui, 'GetClassName', lambda h: '#32770', create=True), \
             patch.object(ths.win32gui, 'GetWindowText', lambda h: '确定' if h == 2 else '委托成功，合同编号：6285045859', create=True), \
             patch.object(ths.time, 'sleep', lambda _: None):
            receipt = b._dismiss_info(timeout=0.01)
        self.assertIsInstance(receipt, dict)
        self.assertEqual(receipt.get('broker_order_id'), '6285045859')
        self.assertEqual(receipt.get('status'), 'submitted')

    def test_accepted_receipt_sets_broker_identity(self):
        b = broker()
        b._fill_order_form = lambda *a, **k: None
        b._ctrl = lambda *a, **k: 1
        b._bm_click = lambda h: None
        b._find_confirm_dialog = lambda: (2, [])
        b._confirm_dialog = lambda *a, **k: (True, 'confirmed')
        b._dismiss_info = lambda: {'status': 'submitted', 'broker_order_id': '6285045859'}
        o = Order('510300.SH', 'buy', 100, 10)
        with patch.object(ths.win32gui, 'GetWindowText', lambda h: '买入', create=True), \
             patch.object(ths.time, 'sleep', lambda _: None):
            b._submit_page(o, 'buy')
        self.assertEqual((o.status, o.broker_order_id), ('submitted', '6285045859'))


class ReadinessTests(unittest.TestCase):
    def test_all_four_controls_are_required(self):
        def found(ids):
            children = {1: 2}
            siblings = {n: n+1 for n in range(2, len(ids)+1)}
            with patch.object(up.win32gui, 'GetWindow', lambda h, kind: children.get(h, 0) if kind == up.win32con.GW_CHILD else siblings.get(h, 0), create=True), \
                 patch.object(up.win32gui, 'GetDlgCtrlID', lambda h: ids[h-2], create=True):
                return up._has_trade_controls(1)
        self.assertFalse(found([1032]))
        self.assertTrue(found([1032, 1034, 1015, 1016]))

    def test_minimized_visible_window_is_not_ready(self):
        w = {'hwnd': 1, 'title': up.TRADE_TITLE, 'pid': 123,
             'visible': True, 'iconic': True, 'controls': True}
        with patch.object(up, 'find_trade_windows', lambda: [dict(w)]), \
             patch.object(up, 'trade_proc_pids', lambda: [123]):
            self.assertEqual(up.snapshot()['state'], 'ICONIC')

    def test_ensure_returns_refreshed_state_after_last_restore(self):
        w = {'hwnd': 1, 'title': up.TRADE_TITLE, 'pid': 123,
             'visible': False, 'iconic': True, 'controls': True}
        def show(hwnd, iconic):
            w.update(visible=True, iconic=False)
            return True, 'restored'
        with patch.object(up, 'find_trade_windows', lambda: [dict(w)]), \
             patch.object(up, 'trade_proc_pids', lambda: [123]), \
             patch.object(up, 'show_trade_window', show):
            result = up.ensure_trade_window(timeout=0, quiet=True)
        self.assertEqual(result['state'], 'READY')
        self.assertTrue(result['ok'])
        self.assertFalse(result['iconic'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
