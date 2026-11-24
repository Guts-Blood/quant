"""Availability, revision and split-boundary tests for factor research."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from factorlab.data.asof import asof_records, latest_fundamentals, validate_fundamentals
from factorlab.labels import forward_returns, within_split


class FundamentalTimingTests(unittest.TestCase):
    def setUp(self):
        self.first = dict(symbol="A", field="net_income", period_end="2023-12-31",
                          published_at="2024-03-10T18:00:00+08:00",
                          available_at="2024-03-11T09:00:00+08:00",
                          ingested_at="2024-03-11T09:05:00+08:00",
                          revision=0, value=100.0, source_id="annual-v1")
        revised = dict(self.first, published_at="2024-03-20T18:00:00+08:00",
                       available_at="2024-03-21T09:00:00+08:00",
                       ingested_at="2024-03-21T09:05:00+08:00",
                       revision=1, value=80.0, source_id="annual-v2")
        self.records = pd.DataFrame([self.first, revised])

    def test_disclosure_and_availability_are_separate(self):
        early = asof_records(self.records, "2024-03-10T20:00:00+08:00", mode="public")
        self.assertTrue(early.empty)
        at_boundary = asof_records(self.records, "2024-03-11T09:00:00+08:00", mode="public")
        self.assertEqual(at_boundary.value.tolist(), [100.0])

    def test_correction_cannot_rewrite_prior_snapshot(self):
        before = asof_records(self.records, "2024-03-15T15:00:00+08:00", mode="public")
        after = asof_records(self.records, "2024-03-22T15:00:00+08:00", mode="public")
        self.assertEqual(before.value.tolist(), [100.0])
        self.assertEqual(after.value.tolist(), [80.0])
        self.assertEqual(after.source_id.tolist(), ["annual-v2"])

    def test_downloaded_later_is_not_actually_observed_earlier(self):
        downloaded = pd.DataFrame([dict(self.first, ingested_at="2026-11-24T12:00:00+08:00")])
        reconstructed = asof_records(downloaded, "2024-03-15T15:00:00+08:00", mode="public")
        observed = asof_records(downloaded, "2024-03-15T15:00:00+08:00", mode="observed")
        self.assertTrue(reconstructed.historical_reconstruction.all())
        self.assertTrue(observed.empty)

    def test_missing_latest_revision_does_not_fall_back_to_old_value(self):
        records = self.records.copy()
        records.loc[1, "value"] = np.nan
        result = latest_fundamentals(records, "2024-03-22T15:00:00+08:00", mode="public")
        self.assertEqual(result.revision.tolist(), [1])
        self.assertTrue(result.value.isna().all())

    def test_unpublished_and_ambiguous_records_are_rejected(self):
        with self.assertRaises(ValueError):
            asof_records(self.records, "2024-03-15", mode="public")
        with self.assertRaises(ValueError):
            validate_fundamentals(pd.concat([self.records, self.records.iloc[:1]]))
        invalid = pd.DataFrame([dict(self.first, available_at="2024-03-09T09:00:00+08:00")])
        with self.assertRaises(ValueError):
            validate_fundamentals(invalid)

    def test_selection_does_not_mutate_source(self):
        before = self.records.copy(deep=True)
        asof_records(self.records, "2024-03-15T15:00:00+08:00", mode="public")
        assert_frame_equal(before, self.records)


class LabelTimingTests(unittest.TestCase):
    def setUp(self):
        self.calendar = pd.bdate_range("2024-01-02", periods=5)
        self.panel = pd.DataFrame({"date": self.calendar, "symbol": "A",
                                   "adj_close": [100.0, 110.0, 121.0, 133.1, 146.41]})

    def test_default_label_starts_next_session(self):
        labels = forward_returns(self.panel, calendar=self.calendar,
                                 price_column="adj_close", horizon=2)
        self.assertEqual(labels.loc[0, "label_start"], self.calendar[1])
        self.assertEqual(labels.loc[0, "label_end"], self.calendar[3])
        self.assertAlmostEqual(labels.loc[0, "forward_return"], 0.21)
        self.assertTrue(labels.loc[2:, "forward_return"].isna().all())

    def test_missing_entry_observation_stays_missing(self):
        labels = forward_returns(self.panel.drop(index=1), calendar=self.calendar,
                                 price_column="adj_close", horizon=1)
        self.assertTrue(pd.isna(labels.loc[0, "forward_return"]))

    def test_label_cannot_cross_training_boundary(self):
        labels = forward_returns(self.panel, calendar=self.calendar,
                                 price_column="adj_close", horizon=2)
        train = within_split(labels, self.calendar[0], self.calendar[3])
        self.assertEqual(train.date.tolist(), [self.calendar[0]])

    def test_same_session_label_is_explicitly_diagnostic(self):
        labels = forward_returns(self.panel, calendar=self.calendar,
                                 price_column="adj_close", horizon=1, entry_lag=0)
        self.assertEqual(set(labels.timing), {"same_session_diagnostic"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
