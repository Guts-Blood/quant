"""Offline checks for formula semantics, missing sessions and information timing."""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal, assert_series_equal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factorlab.data import prepare_panel, sessions
from factorlab.factors import compute, earnings_yield, low_volatility, momentum, reversal


class FactorFormulaTests(unittest.TestCase):
    def test_reversal_known_returns(self):
        result = reversal(pd.Series([100.0, 110.0, 121.0, 108.9]), window=1)
        self.assertTrue(math.isnan(result.iloc[0]))
        for actual, expected in zip(result.iloc[1:], [-0.1, -0.1, 0.1]):
            self.assertAlmostEqual(actual, expected)

    def test_momentum_excludes_recent_sessions(self):
        prices = pd.Series([100.0, 110.0, 121.0, 10_000.0])
        self.assertAlmostEqual(momentum(prices, lookback=3, skip=1).iloc[-1], 0.21)
        prices.iloc[-1] = 1.0
        self.assertAlmostEqual(momentum(prices, lookback=3, skip=1).iloc[-1], 0.21)

    def test_lowvol_uses_sample_standard_deviation(self):
        result = low_volatility(pd.Series([0.0, 0.02]), window=2)
        self.assertTrue(math.isnan(result.iloc[0]))
        self.assertAlmostEqual(result.iloc[1], -math.sqrt(0.0002))

    def test_missing_prices_are_never_forward_filled(self):
        result = reversal(pd.Series([100.0, np.nan, 121.0]), window=1)
        self.assertTrue(result.isna().all())

    def test_missing_returns_break_full_rolling_window(self):
        result = low_volatility(pd.Series([0.0, np.nan, 0.02]), window=2)
        self.assertTrue(result.isna().all())

    def test_earnings_yield_keeps_invalid_pe_missing(self):
        result = earnings_yield(pd.Series([10.0, 20.0, 0.0, -5.0, np.nan]))
        self.assertAlmostEqual(result.iloc[0], 0.1)
        self.assertAlmostEqual(result.iloc[1], 0.05)
        self.assertTrue(result.iloc[2:].isna().all())

    def test_pure_formulas_do_not_mutate_input(self):
        original = pd.Series([100.0, 101.0, 102.0])
        before = original.copy(deep=True)
        reversal(original, window=1)
        momentum(original, lookback=2, skip=1)
        assert_series_equal(original, before)

    def test_invalid_parameters_and_prices_are_rejected(self):
        values = pd.Series([100.0, 110.0, 120.0])
        for window in [0, -1, True, 1.5]:
            with self.subTest(window=window), self.assertRaises(ValueError):
                reversal(values, window=window)
        with self.assertRaises(ValueError):
            momentum(values, lookback=2, skip=2)
        with self.assertRaises(ValueError):
            reversal(pd.Series([100.0, -1.0]), window=1)
        with self.assertRaises(ValueError):
            reversal(pd.Series([100.0, np.inf]), window=1)
        with self.assertRaises(ValueError):
            reversal(pd.Series([100.0, 110.0], index=[1, 0]), window=1)


class FactorPanelTests(unittest.TestCase):
    def setUp(self):
        self.calendar = pd.bdate_range("2024-01-02", periods=4)
        self.panel = pd.DataFrame({
            "date": list(self.calendar[:3]) * 2,
            "symbol": ["A"] * 3 + ["B"] * 3,
            "adj_close": [100.0, 110.0, 121.0, 200.0, 180.0, 162.0],
        })

    def test_symbols_do_not_share_rolling_history(self):
        result = compute(self.panel, "reversal", calendar=self.calendar[:3],
                         parameters={"window": 1})
        last = result[result.date == self.calendar[2]].set_index("symbol")
        self.assertAlmostEqual(last.loc["A", "value"], -0.1)
        self.assertAlmostEqual(last.loc["B", "value"], 0.1)
        self.assertTrue(result[result.date == self.calendar[0]].value.isna().all())

    def test_calendar_gap_does_not_become_one_session_return(self):
        incomplete = self.panel.drop(index=1)
        result = compute(incomplete, "reversal", calendar=self.calendar[:3],
                         parameters={"window": 1})
        a = result[result.symbol == "A"].set_index("date")
        self.assertEqual(a.loc[self.calendar[1], "missing_reason"], "missing_observation")
        self.assertTrue(math.isnan(a.loc[self.calendar[2], "value"]))

    def test_adding_future_observations_cannot_change_past_factors(self):
        before = compute(self.panel, "reversal", calendar=self.calendar[:3],
                         parameters={"window": 1})
        future = pd.DataFrame({"date": [self.calendar[3]] * 2, "symbol": ["A", "B"],
                               "adj_close": [9000.0, 1.0]})
        after = compute(pd.concat([self.panel, future], ignore_index=True), "reversal",
                        calendar=self.calendar, parameters={"window": 1})
        assert_frame_equal(before, after[after.date < self.calendar[3]].reset_index(drop=True))

    def test_panel_is_not_mutated(self):
        saved = self.panel.copy(deep=True)
        compute(self.panel, "reversal", calendar=self.calendar[:3], parameters={"window": 1})
        assert_frame_equal(self.panel, saved)

    def test_duplicate_observations_and_wrong_calendar_are_rejected(self):
        with self.assertRaises(ValueError):
            prepare_panel(pd.concat([self.panel, self.panel.iloc[:1]]),
                          calendar=self.calendar, required=("adj_close",))
        with self.assertRaises(ValueError):
            prepare_panel(self.panel, calendar=self.calendar[:2])
        with self.assertRaises(ValueError):
            sessions(reversed(self.calendar))

    def test_unknown_factor_is_rejected(self):
        with self.assertRaises(ValueError):
            compute(self.panel, "future_return", calendar=self.calendar)


if __name__ == "__main__":
    unittest.main(verbosity=2)
