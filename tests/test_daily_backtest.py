"""Timing and execution invariants for the daily profile (offline fixtures)."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import numpy as np
import pandas as pd

from factorlab.backtest.daily import simulate
from factorlab.portfolio import target_weights

ZERO_COSTS = {"commission_bps": 0, "min_commission": 0, "transfer_bps": 0,
              "sell_tax_bps": 0, "slippage_bps": 0, "participation": 1}


class DailyBacktestTests(unittest.TestCase):
    def setUp(self):
        self.calendar = pd.bdate_range("2024-01-02", periods=4)
        self.panel = pd.DataFrame([{"date": day, "symbol": code, "open": price,
            "close": price, "volume": 100000, "tradable": True, "action": "none",
            "limit_down": 1, "limit_up": 100}
            for day in self.calendar for code, price in [("AAA", 10.0), ("BBB", 20.0)]])
        self.targets = pd.DataFrame([{"date": day, "symbol": "AAA", "weight": 0.9,
                                    "signal_valid": True} for day in self.calendar])

    def run_case(self, panel=None, targets=None, **kwargs):
        options = {"calendar": self.calendar, "initial_cash": 10000,
                   "costs": ZERO_COSTS, "start": self.calendar[1]}
        options.update(kwargs)
        return simulate(self.panel if panel is None else panel,
                        self.targets if targets is None else targets, **options)

    def test_signal_executes_next_session(self):
        result = self.run_case(end=self.calendar[1])
        order = result["orders"].iloc[0]
        self.assertEqual(order.signal_date, self.calendar[0])
        self.assertEqual(order.date, self.calendar[1])
        self.assertEqual(order.filled_shares, 900)
        self.assertLess(pd.Timestamp(order.known_at), self.calendar[1].tz_localize("Asia/Shanghai"))
        self.assertEqual(result["summary"]["ending_nav"], 10000)

    def test_current_signal_cannot_change_current_orders(self):
        changed = self.targets.copy()
        changed.loc[changed.date >= self.calendar[1], "weight"] = 0
        baseline = self.run_case(end=self.calendar[1])
        variant = self.run_case(targets=changed, end=self.calendar[1])
        pd.testing.assert_frame_equal(baseline["orders"], variant["orders"])

    def test_order_size_locked_before_open(self):
        changed = self.panel.copy()
        changed.loc[(changed.date == self.calendar[1]) & (changed.symbol == "AAA"), ["open", "close"]] = 12
        result = self.run_case(panel=changed, end=self.calendar[1])
        order = result["orders"].iloc[0]
        self.assertEqual(order.requested_shares, 900)
        self.assertEqual(order.reason, "insufficient_cash")
        self.assertEqual(order.filled_shares, 0)

    def test_prior_volume_limits_capacity(self):
        changed = self.panel.copy()
        changed.loc[(changed.date == self.calendar[0]) & (changed.symbol == "AAA"), "volume"] = 100
        result = self.run_case(panel=changed, end=self.calendar[1])
        self.assertEqual(result["orders"].iloc[0].reason, "volume_cap")
        self.assertIn("prior_session", result["assumptions"]["capacity"])

    def test_future_daily_volume_does_not_resize_orders(self):
        changed = self.panel.copy()
        changed.loc[changed.date >= self.calendar[1], "volume"] = 2000000
        pd.testing.assert_frame_equal(self.run_case(end=self.calendar[1])["orders"],
                                      self.run_case(panel=changed, end=self.calendar[1])["orders"])

    def test_suspension_and_price_limit_rejections(self):
        for field, value, reason in [("tradable", False, "suspended"), ("limit_up", 10, "price_limit")]:
            with self.subTest(reason=reason):
                changed = self.panel.copy()
                changed.loc[(changed.date == self.calendar[1]) & (changed.symbol == "AAA"), field] = value
                self.assertEqual(self.run_case(panel=changed, end=self.calendar[1])["orders"].iloc[0].reason, reason)

    def test_missing_signal_holds_existing_position(self):
        targets = self.targets.loc[self.targets.date == self.calendar[0]]
        result = self.run_case(targets=targets)
        self.assertEqual(len(result["orders"]), 1)
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(result["daily"].iloc[-1].positions, 1)
        with self.assertRaisesRegex(ValueError, "missing prior-session"):
            self.run_case(targets=targets, missing_signal="error")

    def test_missing_signal_cash_policy_sells_next_day(self):
        targets = self.targets.loc[self.targets.date == self.calendar[0]]
        result = self.run_case(targets=targets, missing_signal="cash")
        self.assertEqual(result["orders"].side.tolist(), ["buy", "sell"])
        self.assertEqual(result["orders"].filled_shares.tolist(), [900, 900])
        self.assertEqual(result["daily"].iloc[-1].positions, 0)

    def test_missing_held_mark_fails_closed(self):
        changed = self.panel.loc[~((self.panel.date == self.calendar[2]) & (self.panel.symbol == "AAA"))]
        with self.assertRaisesRegex(ValueError, "missing held-security"):
            self.run_case(panel=changed)

    def test_unsupported_action_on_holding_fails_closed(self):
        changed = self.panel.copy()
        changed.loc[(changed.date == self.calendar[2]) & (changed.symbol == "AAA"), "action"] = "ex"
        with self.assertRaisesRegex(ValueError, "unsupported held-security"):
            self.run_case(panel=changed)

    def test_cash_dividend_accrual_and_payment(self):
        changed = self.panel.copy()
        ex_day = (changed.date == self.calendar[2]) & (changed.symbol == "AAA")
        changed.loc[ex_day, "action"] = "cash"
        changed.loc[ex_day, "cash_per_share"] = 0.1
        changed.loc[ex_day, "pay_date"] = self.calendar[3].date().isoformat()
        changed.loc[(changed.date >= self.calendar[2]) & (changed.symbol == "AAA"), ["open", "close"]] = 9.9
        result = self.run_case(panel=changed, rebalance_every=10)
        self.assertEqual(result["daily"].receivables.tolist(), [0, 90, 0])
        self.assertEqual(result["daily"].nav.tolist(), [10000, 10000, 10000])
        self.assertEqual(result["daily"].iloc[-1].cash, 1090)

    def test_fees_reduce_nav_and_are_itemized(self):
        costs = ZERO_COSTS | {"commission_bps": 3, "min_commission": 5, "transfer_bps": 0.1}
        result = self.run_case(end=self.calendar[1], costs=costs)
        self.assertAlmostEqual(result["summary"]["fees"], 5.09)
        self.assertAlmostEqual(result["summary"]["ending_nav"], 9994.91)
        self.assertLess(result["summary"]["max_drawdown"], 0)

    def test_failed_sale_does_not_allow_extra_position(self):
        targets = pd.DataFrame([{"date": self.calendar[0], "symbol": "AAA", "weight": 0.4, "signal_valid": True},
                                {"date": self.calendar[1], "symbol": "BBB", "weight": 0.4, "signal_valid": True}])
        changed = self.panel.copy()
        changed.loc[(changed.date == self.calendar[2]) & (changed.symbol == "AAA"), "tradable"] = False
        result = self.run_case(panel=changed, targets=targets, max_positions=1, end=self.calendar[2])
        self.assertEqual(result["orders"].reason.tolist(), ["filled", "suspended", "position_cap"])

    def test_invalid_weights_and_missing_bands_rejected(self):
        targets = self.targets.copy()
        targets["weight"] = 1.1
        with self.assertRaises(ValueError):
            self.run_case(targets=targets)
        changed = self.panel.copy()
        changed["limit_down"] = np.nan
        with self.assertRaises(ValueError):
            self.run_case(panel=changed)


class PortfolioTests(unittest.TestCase):
    def scores(self):
        return pd.DataFrame({"date": ["2024-01-02"] * 3, "symbol": ["BBB", "AAA", "CCC"],
                             "factor": ["test"] * 3, "value": [2.0, 2.0, np.nan]})

    def test_deterministic_ties_and_unfilled_slots_stay_cash(self):
        result = target_weights(self.scores(), top_n=3, cash_buffer=0.1)
        self.assertEqual(result.symbol.tolist(), ["AAA", "BBB", "CCC"])
        np.testing.assert_allclose(result.weight, [0.3, 0.3, 0])
        one = target_weights(self.scores(), top_n=1, cash_buffer=0)
        self.assertEqual(one.loc[one.weight > 0, "symbol"].tolist(), ["AAA"])

    def test_no_valid_scores_are_marked_missing(self):
        scores = self.scores()
        scores["value"] = np.nan
        result = target_weights(scores)
        self.assertFalse(result.signal_valid.any())
        self.assertEqual(result.weight.sum(), 0)

    def test_no_mutation_or_leverage(self):
        scores = self.scores()
        before = scores.copy(deep=True)
        target_weights(scores)
        pd.testing.assert_frame_equal(scores, before)
        for buffer in [-0.1, 1, float("nan")]:
            with self.assertRaises(ValueError):
                target_weights(scores, cash_buffer=buffer)


if __name__ == "__main__":
    unittest.main()
