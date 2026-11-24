"""Historical data and universe checks using local synthetic CSV fixtures."""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from factorlab.data.inputs import load_inputs
from factorlab.universe import eligible_universe


class InputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.calendar = pd.bdate_range("2024-01-02", periods=3)
        self.panel = pd.DataFrame({"date": self.calendar.strftime("%Y-%m-%d"),
            "symbol": "600000.SH", "adj_close": [100.0, 110.0, 121.0],
            "amount": 100_000_000, "is_st": False, "tradable": True,
            "available_at": [day.strftime("%Y-%m-%d") + "T15:05:00+08:00" for day in self.calendar]})
        pd.DataFrame({"date": self.calendar}).to_csv(self.root / "calendar.csv", index=False)
        self.spec = {"panel": "panel.csv", "calendar": "calendar.csv",
                     "source_id": "synthetic_fixture", "availability_mode": "public"}

    def load(self, frame=None, **options):
        (self.panel if frame is None else frame).to_csv(self.root / "panel.csv", index=False)
        return load_inputs(dict(self.spec, **options), base_dir=self.root)

    def test_load_preserves_identity_and_records_provenance(self):
        data = self.load()
        self.assertEqual(data.panel.symbol.unique().tolist(), ["600000.SH"])
        self.assertAlmostEqual(data.panel["return"].iloc[1], 0.1)
        self.assertEqual(len(data.provenance["files"]), 2)
        self.assertEqual(len(data.provenance["files"][0]["sha256"]), 64)
        self.assertTrue(data.provenance["historical_reconstruction"])

    def test_missing_calendar_session_does_not_get_filled(self):
        data = self.load(self.panel.drop(index=1))
        self.assertTrue(data.panel["return"].isna().all())

    def test_late_daily_information_is_rejected(self):
        frame = self.panel.copy()
        frame.loc[0, "available_at"] = "2024-01-03T15:05:00+08:00"
        with self.assertRaises(ValueError):
            self.load(frame)

    def test_premature_daily_close_timestamp_is_rejected(self):
        frame = self.panel.copy()
        frame.loc[0, "available_at"] = "2024-01-02T10:00:00+08:00"
        with self.assertRaises(ValueError):
            self.load(frame)

    def test_observed_mode_rejects_later_download(self):
        frame = self.panel.assign(ingested_at="2026-11-24T12:00:00+08:00")
        with self.assertRaises(ValueError):
            self.load(frame, availability_mode="observed")

    def test_duplicate_header_is_rejected(self):
        self.load()
        (self.root / "panel.csv").write_text("date,symbol,symbol\n2024-01-02,A,B\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_inputs(self.spec, base_dir=self.root)


class UniverseTests(unittest.TestCase):
    def setUp(self):
        self.calendar = pd.bdate_range("2024-01-02", periods=3)
        self.panel = pd.DataFrame({"date": self.calendar, "symbol": "A",
            "adj_close": [100.0, 110.0, 121.0], "amount": 100_000_000,
            "is_st": False, "tradable": True})
        self.events = pd.DataFrame([
            {"symbol": "A", "effective_date": "2024-01-02",
             "available_at": "2024-01-01T18:00:00+08:00", "is_member": True, "source_id": "entry"},
            {"symbol": "A", "effective_date": "2024-01-03",
             "available_at": "2024-01-04T12:00:00+08:00", "is_member": False, "source_id": "late_exit"},
        ])

    def universe(self, panel=None, **options):
        return eligible_universe(self.panel if panel is None else panel, calendar=self.calendar,
                                 min_history=1, liquidity_window=1, min_amount=1, **options)

    def test_later_exit_announcement_cannot_change_past_membership(self):
        result = self.universe(membership=self.events)
        self.assertEqual(result.member.tolist(), [True, True, False])
        self.assertEqual(result.eligible.tolist(), [True, True, False])

    def test_st_and_halts_have_explicit_reasons(self):
        panel = self.panel.copy()
        panel.loc[1, "is_st"] = True
        panel.loc[2, "tradable"] = False
        result = self.universe(panel)
        self.assertEqual(result.eligible.tolist(), [True, False, False])
        self.assertIn("st_status", result.exclusion_reason.iloc[1])
        self.assertIn("not_tradable", result.exclusion_reason.iloc[2])

    def test_missing_observation_is_not_a_tradable_row(self):
        result = self.universe(self.panel.drop(index=1))
        self.assertFalse(result.eligible.iloc[1])
        self.assertIn("missing_observation", result.exclusion_reason.iloc[1])

    def test_observed_membership_requires_capture_and_excludes_late_events(self):
        with self.assertRaisesRegex(ValueError, "ingested_at"):
            self.universe(membership=self.events, availability_mode="observed")
        events = self.events.assign(ingested_at=["2024-01-02T16:30:00+08:00", "2024-01-05T10:00:00+08:00"])
        observed = self.universe(membership=events, availability_mode="observed")
        self.assertEqual(observed.member.tolist(), [False, True, True])

    def test_duplicate_membership_events_are_rejected(self):
        events = pd.concat([self.events, self.events.iloc[:1]])
        with self.assertRaises(ValueError):
            self.universe(membership=events)


if __name__ == "__main__":
    unittest.main(verbosity=2)
