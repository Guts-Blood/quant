"""Regression checks for real-window publication and data-gated simulation."""

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pandas as pd

try:
    import duckdb
except ModuleNotFoundError as exc:
    raise unittest.SkipTest("Install .[market] for pilot tests") from exc

from experiments.market_research import run
from experiments.market_statements import read_disclosures
from experiments.option_capture import CST, collect, observation, quote_audit
from experiments.scheduled_options import capture_session
from factorlab.data.asof import asof_records
from test_market_pipeline import sample
from test_risk_expectations import option_fixture


class ExecutionPilotTests(unittest.TestCase):
    def test_actual_annual_report_restatement(self):
        facts = read_disclosures(
            Path(__file__).resolve().parents[1] / "configs/market-data/pilot-disclosures.json"
        )
        facts = facts.query("symbol=='600519' and metric=='net_profit'")

        def cut(when, mode="public"):
            return asof_records(facts, when + "+08:00", mode=mode)

        self.assertTrue(cut("2023-03-31T16:00").empty)
        self.assertEqual(cut("2023-04-03T16:00").value.item(), 65375136766.75)
        self.assertEqual(cut("2024-04-08T16:00").value.item(), 65376039957.88)
        self.assertTrue(cut("2024-04-08T16:00", "observed").empty)

    def test_unknown_carried_action_produces_unavailable_receipt(self):
        dates = pd.bdate_range("2024-01-02", periods=140)
        with duckdb.connect() as db, TemporaryDirectory() as tmp:
            sample(db, dates)
            db.execute("CREATE TABLE original_execution AS SELECT * FROM execution")
            db.execute("""CREATE OR REPLACE VIEW execution AS SELECT * REPLACE (
                CASE WHEN date=(SELECT date FROM trading_calendar WHERE i=128)
                THEN 'unknown' ELSE action END AS action) FROM original_execution""")
            report = run(
                db,
                {"snapshot": "fixture", "files": []},
                "0" * 64,
                start=dates[126].date(),
                end=dates[133].date(),
                out=Path(tmp),
                mode="compare",
            )
            self.assertEqual(report["status"], "unavailable")
            self.assertIn("unsupported held-security", str(report["reasons"]))
            self.assertEqual(report["effective_rebalances"], 0)
            self.assertFalse(list(Path(tmp).glob("*-daily.csv")))
            self.assertTrue((Path(tmp) / "candidates.csv").is_file())

    def test_holiday_and_two_independent_incomplete_batches(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            holiday = datetime(2026, 10, 1, 15, 1, tzinfo=CST)
            self.assertEqual(capture_session(folder, now=lambda: holiday)["status"], "closed")
            clock = datetime(2026, 11, 25, 15, 1, tzinfo=CST)

            def now():
                return clock

            def sleep(seconds):
                nonlocal clock
                clock += timedelta(seconds=seconds)

            rows = [{**q, "quoted_at": clock.isoformat(), "underlying": "510500"} for q in option_fixture()]

            def capture(folder, **kwargs):
                return collect(
                    folder,
                    fetch=lambda *args, **kw: {"quotes": rows[:1], "expected_contracts": len(rows)},
                    **kwargs,
                )

            receipt = capture_session(folder, now=now, sleep=sleep, capture=capture)
            self.assertEqual(len({a["sha256"] for a in receipt["attempts"]}), 2)
            audit = quote_audit(observation(folder / receipt["selected"], "daily"))
            self.assertEqual(audit["missing_contracts"], len(rows) - 1)
            self.assertEqual(audit["status"], "unavailable")
            self.assertTrue(audit["expiries"])


if __name__ == "__main__":
    unittest.main()
