"""Numerical diagnostic checks independent of portfolio execution."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from factorlab.evaluation import evaluate, factor_correlations


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.factors = pd.DataFrame({"date": ["2024-01-02"] * 4,
                                     "symbol": ["A", "B", "C", "D"],
                                     "factor": "example", "value": [1.0, 2.0, 3.0, 4.0]})
        self.labels = pd.DataFrame({"date": ["2024-01-02"] * 4,
                                    "symbol": ["A", "B", "C", "D"],
                                    "forward_return": [0.0, 0.1, 0.2, 0.3]})

    def test_known_rank_ic_and_group_spread(self):
        report = evaluate(self.factors, self.labels, quantiles=2, min_observations=4)
        day = report["daily"].iloc[0]
        self.assertAlmostEqual(day.rank_ic, 1.0)
        self.assertAlmostEqual(day.group_spread, 0.2)
        self.assertEqual(report["groups"]["count"].tolist(), [2, 2])
        self.assertAlmostEqual(report["groups"].mean_forward_return.iloc[0], 0.05)
        self.assertAlmostEqual(report["groups"].mean_forward_return.iloc[1], 0.25)

    def test_coverage_is_counted_before_missing_outcomes_are_dropped(self):
        labels = self.labels.copy()
        labels.loc[3, "forward_return"] = np.nan
        day = evaluate(self.factors, labels, quantiles=2, min_observations=2)["daily"].iloc[0]
        self.assertEqual(day.factor_observations, 4)
        self.assertEqual(day.outcome_observations, 3)
        self.assertEqual(day.factor_coverage, 1.0)
        self.assertEqual(day.outcome_coverage, 0.75)

    def test_constant_factor_does_not_create_an_arbitrary_spread(self):
        factors = self.factors.assign(value=1.0)
        report = evaluate(factors, self.labels, quantiles=2, min_observations=4)
        self.assertEqual(report["daily"].status.iloc[0], "constant_factor")
        self.assertTrue(report["daily"].group_spread.isna().all())
        self.assertTrue(report["groups"].empty)

    def test_insufficient_sample_is_reported(self):
        report = evaluate(self.factors, self.labels, quantiles=2, min_observations=20)
        self.assertEqual(report["daily"].status.iloc[0], "insufficient_sample")
        self.assertTrue(report["daily"].rank_ic.isna().all())

    def test_duplicate_labels_are_rejected(self):
        duplicate = pd.concat([self.labels, self.labels.iloc[:1]])
        with self.assertRaises(ValueError):
            evaluate(self.factors, duplicate, quantiles=2, min_observations=4)

    def test_pairwise_redundancy_uses_joint_observations(self):
        inverse = self.factors.assign(factor="inverse", value=-self.factors.value)
        sparse = self.factors.assign(factor="sparse", value=[1.0, 2.0, np.nan, np.nan])
        correlations = factor_correlations(pd.concat([self.factors, inverse, sparse]),
                                            min_observations=3)
        self.assertEqual(len(correlations), 1)
        self.assertAlmostEqual(correlations.rank_correlation.iloc[0], -1.0)
        self.assertEqual(correlations.observations.iloc[0], 4)

    def test_eligibility_is_explicit_and_complete(self):
        mask = self.factors[["date", "symbol"]].assign(eligible=[True, True, True, False])
        report = evaluate(self.factors, self.labels, eligibility=mask,
                          quantiles=2, min_observations=2)
        self.assertEqual(report["daily"].eligible.iloc[0], 3)
        with self.assertRaises(ValueError):
            evaluate(self.factors, self.labels, eligibility=mask.iloc[:3],
                     quantiles=2, min_observations=2)

    def test_evaluation_does_not_mutate_inputs(self):
        factors, labels = self.factors.copy(deep=True), self.labels.copy(deep=True)
        evaluate(self.factors, self.labels, quantiles=2, min_observations=4)
        assert_frame_equal(factors, self.factors)
        assert_frame_equal(labels, self.labels)


if __name__ == "__main__":
    unittest.main(verbosity=2)
