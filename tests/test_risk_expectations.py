"""Independent analytic fixtures and failure cases for research risk calculations."""
import math
import unittest
from datetime import datetime, timedelta, timezone

from experiments.legacy_pilot.risk_expectations import (
    equity_to_operating_ev, forward_dcf, local_iv30, reverse_dcf,
)

ASOF = datetime(2026, 11, 24, 7, 0, tzinfo=timezone.utc)


def option_fixture(volatilities=(0.2, 0.2)):
    """Black-Scholes premiums provide an independent volatility reference."""
    rows = []
    spot, rate = 100.0, 0.02
    normal = lambda x: (1 + math.erf(x / math.sqrt(2))) / 2
    for days, sigma in zip((20, 50), volatilities):
        t = days / 365
        for strike in range(50, 151):
            d1 = (math.log(spot / strike) + (rate + sigma * sigma / 2) * t) / (sigma * math.sqrt(t))
            d2 = d1 - sigma * math.sqrt(t)
            call = spot * normal(d1) - strike * math.exp(-rate * t) * normal(d2)
            put = strike * math.exp(-rate * t) * normal(-d2) - spot * normal(-d1)
            for kind, premium in (("call", call), ("put", put)):
                premium = max(premium, 0.0)
                rows.append({"contract_id": f"{days}-{strike}-{kind}", "underlying": "fixture",
                             "kind": kind, "strike": strike, "bid": premium * 0.99,
                             "ask": max(premium * 1.01, 1e-12), "adjusted": False,
                             "quoted_at": ASOF.isoformat(),
                             "expiry": (ASOF + timedelta(days=days)).isoformat()})
    return rows


def dcf_fixture():
    return {"base_revenue": 100.0, "tax_rate": 0.25, "sales_to_capital": 2.0,
            "discount_rate": 0.1, "terminal_growth": 0.02, "terminal_margin": 0.2,
            "terminal_roic": 0.15, "operating_margins": [0.2] * 5}


class ImpliedVolatilityTests(unittest.TestCase):
    def test_recovers_known_black_scholes_volatility(self):
        result = local_iv30(option_fixture(), asof=ASOF, rate=0.02)
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["iv30"], 0.2, delta=0.003)
        self.assertAlmostEqual(result["iv30_percent"], 100 * result["iv30"])
        self.assertEqual(len(result["terms"]), 2)

    def test_interpolates_total_variance_not_volatility(self):
        result = local_iv30(option_fixture((0.2, 0.3)), asof=ASOF, rate=0.02)
        expected_variance = (20 * 0.2 ** 2 * (50 - 30) + 50 * 0.3 ** 2 * (30 - 20)) / ((50 - 20) * 30)
        self.assertAlmostEqual(result["iv30"], math.sqrt(expected_variance), delta=0.003)

    def test_price_unit_invariance(self):
        rows = option_fixture()
        scaled = [{**row, **{key: row[key] * 10 for key in ("strike", "bid", "ask")}} for row in rows]
        original = local_iv30(rows, asof=ASOF, rate=0.02)
        converted = local_iv30(scaled, asof=ASOF, rate=0.02)
        self.assertAlmostEqual(original["iv30"], converted["iv30"], places=10)

    def test_stale_quotes_are_unavailable(self):
        result = local_iv30(option_fixture(), asof=ASOF + timedelta(seconds=301), rate=0.02)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(len(result["excluded_quotes"]), 404)
        self.assertNotIn("iv30", result)

    def test_future_quotes_are_unavailable(self):
        result = local_iv30(option_fixture(), asof=ASOF - timedelta(seconds=1), rate=0.02)
        self.assertEqual(result["status"], "unavailable")

    def test_does_not_extrapolate_missing_maturity(self):
        rows = [row for row in option_fixture() if row["contract_id"].startswith("20-")]
        self.assertEqual(local_iv30(rows, asof=ASOF, rate=0.02)["status"], "unavailable")

    def test_rejects_duplicate_strike_contracts(self):
        rows = option_fixture()
        rows.append(dict(rows[0]))
        result = local_iv30(rows, asof=ASOF, rate=0.02)
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(any("duplicate" in item["reason"] for item in result["excluded_expiries"]))

    def test_rejects_adjusted_or_unverified_contracts(self):
        for adjusted in (True, None):
            with self.subTest(adjusted=adjusted):
                rows = [{**row, "adjusted": adjusted} for row in option_fixture()]
                self.assertEqual(local_iv30(rows, asof=ASOF, rate=0.02)["status"], "unavailable")

    def test_rejects_mixed_underlyings(self):
        rows = option_fixture()
        rows[0]["underlying"] = "different"
        with self.assertRaises(ValueError):
            local_iv30(rows, asof=ASOF, rate=0.02)

    def test_requires_timezone(self):
        with self.assertRaises(ValueError):
            local_iv30(option_fixture(), asof="2026-11-24T07:00:00", rate=0.02)

    def test_crossed_quotes_do_not_become_zero(self):
        rows = [{**row, "bid": 2.0, "ask": 1.0} for row in option_fixture()]
        result = local_iv30(rows, asof=ASOF, rate=0.02)
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(all("crossed" in item["reason"] for item in result["excluded_quotes"]))


class ReverseDCFTests(unittest.TestCase):
    def test_constant_cash_flow_matches_perpetuity(self):
        model = {**dcf_fixture(), "terminal_growth": 0.0}
        self.assertAlmostEqual(forward_dcf(0.0, model)["enterprise_value"], 15 / 0.1, places=10)

    def test_recovers_known_growth(self):
        model = dcf_fixture()
        target = forward_dcf(0.08, model)["enterprise_value"]
        result = reverse_dcf(target, model, growth_bounds=(0, 0.5))
        self.assertEqual(result["status"], "root_found")
        self.assertAlmostEqual(result["roots"][0]["implied_revenue_growth"], 0.08, places=7)
        self.assertEqual(result["uniqueness"], "not_proven_by_grid_search")

    def test_valuation_unit_invariance(self):
        model = dcf_fixture()
        target = forward_dcf(0.08, model)["enterprise_value"]
        scaled = {**model, "base_revenue": model["base_revenue"] * 1e6}
        result = reverse_dcf(target * 1e6, scaled, growth_bounds=(0, 0.5))
        self.assertAlmostEqual(result["roots"][0]["implied_revenue_growth"], 0.08, places=7)

    def test_reinvestment_has_a_cost(self):
        model = dcf_fixture()
        capital_intensive = {**model, "sales_to_capital": 1.0}
        self.assertLess(forward_dcf(0.1, capital_intensive)["enterprise_value"],
                        forward_dcf(0.1, model)["enterprise_value"])

    def test_reports_two_roots_in_nonmonotonic_model(self):
        model = {**dcf_fixture(), "base_revenue": 1.0, "tax_rate": 0.0,
                 "operating_margins": [0.4, 0.4], "terminal_margin": 0.0,
                 "terminal_growth": 0.0}
        target = forward_dcf(0.2, model)["enterprise_value"]
        result = reverse_dcf(target, model, growth_bounds=(0, 2))
        self.assertEqual(result["status"], "multiple_roots_found")
        self.assertAlmostEqual(result["roots"][0]["implied_revenue_growth"], 0.2, places=6)
        self.assertAlmostEqual(result["roots"][1]["implied_revenue_growth"], 1.7, places=6)

    def test_outside_search_range_is_not_a_fake_growth_number(self):
        result = reverse_dcf(1e12, dcf_fixture(), growth_bounds=(0, 0.1))
        self.assertEqual(result["status"], "no_bracketed_root")
        self.assertEqual(result["roots"], [])

    def test_invalid_terminal_assumptions_fail(self):
        for changes in ({"terminal_growth": 0.1}, {"terminal_roic": 0.01},
                        {"discount_rate": float("nan")}, {"sales_to_capital": 0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                forward_dcf(0.1, {**dcf_fixture(), **changes})

    def test_operating_ev_bridge(self):
        result = equity_to_operating_ev(market_cap=100, debt=20, cash=10,
                                       minority_interests=5, nonoperating_assets=3)
        self.assertEqual(result["operating_enterprise_value"], 112)

    def test_no_silent_zero_for_missing_bridge_input(self):
        with self.assertRaises(ValueError):
            equity_to_operating_ev(market_cap=100, debt=None, cash=10,
                                   minority_interests=0, nonoperating_assets=0)


if __name__ == "__main__":
    unittest.main()
