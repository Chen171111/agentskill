"""Offline strategy regression tests; no network, live ledger, UI, or orders.

Run: python -B -X utf8 tools/test_strategy_integrity.py
Each case names the wrong behavior it rejects. Fixtures use hand-derived prices
and exact split ratios; inferred events are tested separately as approximations.
"""
import copy
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from backtest.account import BacktestAccount
from backtest.engine import BacktestEngine
from dataprovider.panel import Panel, build_panel
from dataprovider.store import DataStore
from factors.engine import compute_factors
from factors.registry import _macd_hist
from strategies.base import Strategy
from strategies.builtin import EtfRotationStrategy, MeanReversionStrategy, MultiFactorStrategy


class OneBuy(Strategy):
    stops_enabled = False

    def generate_weights(self, date, factors, panel):
        self._since += 1
        return {panel.codes[0]: .3} if self._since == 1 else None


class TracedAccount(BacktestAccount):
    """Test-only observations around the real account, never fake trades."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.observed, self.fees = {}, 0.

    def _fee(self, notional, is_sell, code):
        return super()._fee(notional, is_sell, code)

    def trade(self, *args, **kwargs):
        # Fee calls include affordability probes, so derive paid costs from the
        # independently priced cash/position transition rather than call counts.
        before_cash = self.cash
        before_qty = {c: p['qty'] for c, p in self.positions.items()}
        super().trade(*args, **kwargs)
        opens = args[1]
        turnover = sum((p['qty'] - before_qty.get(c, 0)) * opens[c]
                       for c, p in self.positions.items() if c in opens)
        turnover += sum(-q * opens[c] for c, q in before_qty.items()
                        if c not in self.positions and c in opens)
        self.fees += before_cash - self.cash - turnover

    def mark_to_close(self, date, prices):
        super().mark_to_close(date, prices)
        self.observed[date] = (copy.deepcopy(self.positions), self.cash, self.fees)


def price_panel(values, code='510300.SH'):
    idx = pd.bdate_range('2024-01-01', periods=len(values)).strftime('%Y%m%d')
    close = pd.DataFrame({code: values}, index=idx)
    return Panel({'close': close, 'open': close.copy(), 'high': close.copy(),
                  'low': close.copy(), 'volume': close * 0 + 1e6,
                  'rate': close.pct_change()}, [code], {code: 'etf'})


class StrategyIntegrity(unittest.TestCase):
    def api(self, module, name):
        try:
            fn = getattr(importlib.import_module(module), name, None)
        except ModuleNotFoundError:
            fn = None
        self.assertTrue(callable(fn), module + '.' + name + ' shared API is missing')
        return fn

    def test_eligibility_preserves_non_rebalance_and_unrounded_small_gain(self):
        fn = self.api('strategies.eligibility', 'filter_positive_momentum')
        date = '20240131'
        values = pd.DataFrame({'tiny': [0.00000001], 'zero': [0.], 'down': [-.01],
                               'nan': [np.nan], 'inf': [np.inf]}, index=[date])
        self.assertIsNone(fn(None, {}, date))
        self.assertEqual(fn({}, {}, date), {})
        weights = {c: .1 for c in ('tiny', 'zero', 'down', 'nan', 'inf', 'missing')}
        self.assertEqual(fn(weights, {'momentum20': values}, date), {'tiny': .1})
        self.assertEqual(fn(weights, {}, date), {})

    def test_default_etf_does_not_buy_negative_twenty_day_momentum(self):
        values = np.linspace(90, 120, 280)
        # Both decision days have negative 20-day momentum; a 21-bar decline
        # would leave the penultimate signal positive and correctly buy once.
        values[-22:] = np.linspace(125, 121, 22)
        full = price_panel(values)
        panel = full.slice(full.dates[-2], full.dates[-1])
        panel.fields['close_full'] = full.get('close')
        factors = compute_factors(full, ['momentum20'])
        engine = BacktestEngine(panel, factors, EtfRotationStrategy(rebalance_every=1),
                                dd_circuit=False, vol_target=None)
        engine.run()
        self.assertEqual(engine.acc.positions, {})

    def test_mean_reversion_is_allowed_to_buy_negative_momentum(self):
        panel = price_panel([10., 10.])
        factors = {'bias20': pd.DataFrame({'510300.SH': [-.1, -.1]}, index=panel.dates),
                   'momentum20': pd.DataFrame({'510300.SH': [-.1, -.1]}, index=panel.dates)}
        engine = BacktestEngine(panel, factors, MeanReversionStrategy(rebalance_every=1),
                                dd_circuit=False, vol_target=None)
        engine.run()
        self.assertGreater(engine.acc.positions['510300.SH']['qty'], 0)

    def test_macd_cross_asset_score_does_not_depend_on_price_units(self):
        close = pd.Series(100 + np.arange(90) * .1)
        a = _macd_hist({'close': close})
        b = _macd_hist({'close': close * .5})
        np.testing.assert_allclose(a, b, rtol=1e-10, atol=1e-12)

    def test_engine_executes_and_values_raw_prices_after_historic_split(self):
        panel = price_panel([20., 20.])
        panel.fields['raw_open'] = pd.DataFrame({'510300.SH': [10., 10.]}, index=panel.dates)
        panel.fields['raw_close'] = panel.fields['raw_open'].copy()
        strategy = OneBuy()
        strategy.stops_enabled = False
        engine = BacktestEngine(panel, {}, strategy, init_cash=10000,
                                dd_circuit=False, vol_target=None)
        engine.acc = BacktestAccount(10000, {'commission_rate': 0., 'min_commission': 5.,
                                            'slippage_rate': 0., 'sell_tax_rate': 0.})
        result = engine.run()
        self.assertEqual(engine.acc.positions['510300.SH']['qty'], 300)
        self.assertEqual(engine.acc.cash, 6995.)
        self.assertEqual(float(result.equity.iloc[-1]['value']), 9995.)

    def test_effective_split_changes_quantity_cost_peak_without_cash_or_fee(self):
        acc = BacktestAccount(1000)
        acc.positions = {'510300.SH': {'qty': 100, 'cost': 20., 'peak': 24.}}
        acc._last_px = {'510300.SH': 22.}
        fn = getattr(acc, 'apply_splits', None)
        self.assertTrue(callable(fn), 'BacktestAccount.apply_splits is missing')
        fn({'510300.SH': 2.})
        self.assertEqual(acc.positions['510300.SH'], {'qty': 200, 'cost': 10., 'peak': 12.})
        self.assertEqual(acc.cash, 1000.)
        self.assertEqual(acc.total({}), 3200.)

    def test_split_odd_lot_can_be_fully_liquidated(self):
        acc = BacktestAccount(1000, {'commission_rate': 0., 'min_commission': 0.,
                                     'slippage_rate': 0., 'sell_tax_rate': 0.})
        acc.positions = {'510300.SH': {'qty': 300, 'cost': 15., 'peak': 15.}}
        acc.apply_splits({'510300.SH': 1.5})
        acc.trade({}, {'510300.SH': 10.})
        self.assertEqual(acc.positions, {})
        self.assertEqual(acc.cash, 5500.)

    def test_engine_applies_split_before_next_open_and_adjusts_limit_reference(self):
        panel = price_panel([10., 10., 5.])
        panel.fields['raw_close'] = panel.get('close').copy()
        panel.fields['raw_open'] = panel.get('open').copy()
        panel.fields['close'] = pd.DataFrame({'510300.SH': [10., 10., 10.]}, index=panel.dates)
        panel.fields['split_ratio'] = pd.DataFrame({'510300.SH': [1., 1., 2.]}, index=panel.dates)
        class BuyThenExit(OneBuy):
            def generate_weights(self, date, factors, panel):
                self._since += 1
                return {panel.codes[0]: .3} if self._since == 1 else ({} if self._since == 2 else None)
        strategy = BuyThenExit()
        strategy.stops_enabled = False
        engine = BacktestEngine(panel, {}, strategy, init_cash=10000,
                                dd_circuit=False, vol_target=None)
        engine.acc = BacktestAccount(10000, {'commission_rate': 0., 'min_commission': 0.,
                                            'slippage_rate': 0., 'sell_tax_rate': 0.})
        engine.run()
        # 300 shares bought at 10 become 600 before selling at 5. Treating 5
        # as a limit-down from 10 blocks that sale; missing the quantity event
        # sells only half the position value.
        self.assertEqual(engine.acc.positions, {})
        self.assertEqual(engine.acc.cash, 10000.)

    def test_timing_without_explicit_benchmark_uses_first_signal_column(self):
        fn = self.api('risk.timing', 'timing_scale')
        panel = price_panel([110.] * 19 + [90.])
        close = panel.get('close').assign(second=[200.] * 20)
        self.assertEqual(fn('bias', panel.dates[-1], close), 0.)
        engine = BacktestEngine(panel, {}, OneBuy(), timing='bias')
        self.assertEqual(engine._timing_scale(panel.dates[-1]), 0.)

    def test_timing_history_missing_or_short_is_conservative(self):
        fn = self.api('risk.timing', 'timing_scale')
        panel = price_panel([10.] * 19)
        for mode in ('ma20', 'abs_mom', 'bias', 'rsrs'):
            with self.subTest(mode=mode):
                self.assertEqual(fn(mode, panel.dates[-1], panel.get('close')), .3)
                self.assertEqual(fn(mode, '20231229', panel.get('close')), .3)
        self.assertEqual(fn(None, panel.dates[-1], None), 1.)

    def test_timing_is_causal_and_explicit_benchmark_overrides_first_column(self):
        fn = self.api('risk.timing', 'timing_scale')
        panel = price_panel([110.] * 19 + [90.] + [1000.] * 50)
        date = panel.dates[19]
        for mode in ('ma20', 'abs_mom', 'bias'):
            with self.subTest(mode=mode):
                prefix = panel.get('close').loc[:date]
                self.assertEqual(fn(mode, date, panel.get('close')), fn(mode, date, prefix))
        benchmark = pd.Series(np.arange(10., 80.), index=panel.dates)
        self.assertEqual(fn('ma20', date, panel.get('close'), benchmark=benchmark), 1.)

    def test_timing_rejects_unknown_modes_instead_of_ignoring_them(self):
        fn = self.api('risk.timing', 'timing_scale')
        with self.assertRaises(ValueError):
            fn('unknown', '20240101', None)

    def test_rsrs_uses_aligned_causal_high_low_and_needs_variation(self):
        fn = self.api('risk.timing', 'timing_scale')
        panel = price_panel(np.arange(10., 90.))
        close = panel.get('close')
        low = close.copy()
        high = close + np.arange(80).reshape(-1, 1) ** 2 * .01
        date = panel.dates[65]
        self.assertEqual(fn('rsrs', date, close, high, low), 1.)
        self.assertEqual(fn('rsrs', date, close, high.loc[:date], low.loc[:date]), 1.)
        # Different date indexes cannot be treated as positional pairs.
        self.assertEqual(fn('rsrs', date, close, high.drop(index=panel.dates[40]), low), .3)
        self.assertEqual(fn('rsrs', date, close, close * 2, low), .3)

    def test_pipeline_timing_keeps_warmup_before_backtest_start(self):
        import pipeline
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            idx = pd.bdate_range('2024-01-01', periods=40).strftime('%Y%m%d')
            values = np.array([110.] * 38 + [90., 90.])
            pd.DataFrame({'date': idx, 'open': values, 'high': values,
                          'low': values, 'close': values, 'volume': 1e6,
                          'split_ratio': 1.}).to_csv(
                              folder / '510300.SH.csv', index=False)
            def temporary_store():
                store = DataStore()
                store.split_log = folder / 'splits.log'
                return store
            with patch.object(config, 'STOCK_DIR', folder), patch.object(config, 'INDEX_DIR', folder), \
                 patch.object(pipeline, 'DataStore', side_effect=temporary_store):
                out = pipeline.run_backtest(['510300.SH'], strategy='mean_reversion',
                                            start=idx[-2], end=idx[-1], rebalance=1,
                                            timing='bias', dd_circuit=False, vol_target=None)
            self.assertEqual(out['result'].holdings[idx[-1]], [])


class SplitDataIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.stock_patch = patch.object(config, 'STOCK_DIR', self.folder)
        self.index_patch = patch.object(config, 'INDEX_DIR', self.folder)
        self.stock_patch.start()
        self.index_patch.start()

    def tearDown(self):
        self.index_patch.stop()
        self.stock_patch.stop()
        self.tmp.cleanup()

    def store(self):
        store = DataStore()
        store.split_log = self.folder / 'split.log'
        return store

    def write(self, code, values, ratios=None):
        idx = pd.bdate_range('2024-01-01', periods=len(values)).strftime('%Y%m%d')
        frame = pd.DataFrame({'date': idx, 'open': values, 'high': values,
                              'low': values, 'close': values, 'volume': 1e6})
        if ratios is not None:
            frame['split_ratio'] = ratios
        frame.to_csv(self.folder / (code + '.csv'), index=False)
        return list(idx)

    def test_explicit_split_ratio_preserves_real_market_move_and_raw_price(self):
        self.write('510300.SH', [100., 55., 56.], [1., 2., 1.])
        frame = self.store().read('510300.SH')
        self.assertIn('raw_close', frame.columns)
        self.assertEqual(list(frame['raw_close']), [100., 55., 56.])
        self.assertEqual(list(frame['close']), [100., 110., 112.])
        self.assertEqual(list(frame['split_ratio']), [1., 2., 1.])
        self.assertAlmostEqual(float(frame.iloc[1]['rate']), .10)
        self.assertEqual(list(frame['split_inferred']), [0., 0., 0.])

    def test_ordinary_return_never_becomes_a_split_ratio(self):
        self.write('510300.SH', [100., 104., 101.])
        frame = self.store().read('510300.SH')
        self.assertIn('split_ratio', frame.columns)
        self.assertEqual(list(frame['split_ratio']), [1., 1., 1.])

    def test_inferred_jump_is_marked_as_an_estimate(self):
        self.write('510300.SH', [100., 50., 51.])
        frame = self.store().read('510300.SH')
        self.assertIn('split_inferred', frame.columns)
        self.assertEqual(list(frame['split_ratio']), [1., 2., 1.])
        self.assertEqual(list(frame['split_inferred']), [0., 1., 0.])

    def test_panel_propagates_raw_prices_and_events(self):
        idx = self.write('510300.SH', [100., 50., 51.], [1., 2., 1.])
        panel = build_panel(self.store(), ['510300.SH'])
        for field in ('raw_open', 'raw_high', 'raw_low', 'raw_close', 'split_ratio', 'split_inferred'):
            self.assertIsNotNone(panel.get(field), field)
        self.assertEqual(float(panel.get('raw_close').loc[idx[1], '510300.SH']), 50.)

    def test_future_split_does_not_change_signal_history(self):
        idx = self.write('510300.SH', [100., 102., 51., 52.], [1., 1., 2., 1.])
        store = self.store()
        prefix = store.read('510300.SH', end=idx[1])
        extended = store.read('510300.SH')
        pd.testing.assert_frame_equal(prefix, extended.loc[:idx[1]])

    def test_extending_past_split_preserves_earlier_choices_shares_cash_and_costs(self):
        idx = None
        for code, slope in [('510300.SH', .1), ('510500.SH', .08)]:
            values = 100 + np.arange(90) * slope
            ratios = np.ones(90)
            if code == '510300.SH':
                values[60:] *= .5
                ratios[60] = 2.
            idx = self.write(code, values, ratios)
        store = self.store()
        observed = []
        for end in (idx[45], idx[-1]):
            panel = build_panel(store, ['510300.SH', '510500.SH'], end=end)
            factors = compute_factors(panel, ['macd_hist'])
            strategy = MultiFactorStrategy(topk=1, rebalance_every=1,
                                           specs=[('macd_hist', 1, 1.)])
            strategy.stops_enabled = False
            engine = BacktestEngine(panel, factors, strategy, init_cash=100000,
                                    dd_circuit=False, vol_target=None)
            engine.acc = TracedAccount(100000)
            result = engine.run()
            observed.append((result, engine.acc))
        a, b = observed
        self.assertEqual(a[0].holdings, {d: b[0].holdings[d] for d in a[0].holdings})
        self.assertEqual(a[1].observed, {d: b[1].observed[d] for d in a[1].observed})
        pd.testing.assert_frame_equal(a[0].equity, b[0].equity.loc[:idx[45]])


if __name__ == '__main__':
    unittest.main(verbosity=2)
