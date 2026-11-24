"""Cross-sectional transformations must not fit across time or misalign rows."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from factorlab.transforms import combine, neutralize, standardize, winsorize


class TransformTests(unittest.TestCase):
    def frame(self, values, name="x"):
        return pd.DataFrame({"date": "2024-01-02", "symbol": [f"S{i}" for i in range(len(values))],
                             "factor": name, "value": values})

    def test_winsorization_matches_known_quartiles(self):
        result = winsorize(self.frame([0.0, 1.0, 2.0, 100.0]), lower=0.25, upper=0.75)
        np.testing.assert_allclose(result.value, [0.75, 1.0, 2.0, 26.5])

    def test_standardization_uses_only_each_session(self):
        first = self.frame([1.0, 2.0, 3.0])
        future = self.frame([1000.0, 1.0, 2.0]).assign(date="2024-01-03")
        before = standardize(first)
        after = standardize(pd.concat([first, future], ignore_index=True))
        np.testing.assert_allclose(before.value, [-np.sqrt(1.5), 0.0, np.sqrt(1.5)])
        assert_frame_equal(before, after.iloc[:3].reset_index(drop=True))

    def test_missing_and_constant_values_are_explicit(self):
        result = standardize(self.frame([5.0, 5.0, np.nan]))
        self.assertEqual(result.value.iloc[:2].tolist(), [0.0, 0.0])
        self.assertTrue(pd.isna(result.value.iloc[2]))

    def test_neutralization_removes_known_exposures_with_nonrange_index(self):
        size = np.array([1., 2., 3., 4., 1., 2., 3., 4.])
        industry = np.array(["a"] * 4 + ["b"] * 4)
        noise = np.array([-1., 1., -1., 1., 1., -1., 1., -1.])
        factors = self.frame(2 * size + (industry == "b") * 10 + noise)
        factors.index = np.arange(10, 26, 2)
        exposures = factors[["date", "symbol"]].copy()
        exposures["industry"] = industry
        exposures["market_cap"] = np.exp(size)
        exposures["available_at"] = "2024-01-02T15:05:00+08:00"
        result = neutralize(factors, exposures, min_observations=5)
        np.testing.assert_allclose(result.value, noise, atol=1e-12)
        self.assertEqual(set(result.transform_status), {"ok"})

    def test_late_exposure_is_rejected(self):
        factors = self.frame([1.0, 2.0, 3.0])
        exposures = factors[["date", "symbol"]].assign(
            industry="a", market_cap=[10.0, 20.0, 30.0],
            available_at="2024-01-03T15:05:00+08:00")
        with self.assertRaises(ValueError):
            neutralize(factors, exposures, min_observations=2)

    def test_blend_missing_policy_is_declared(self):
        first = self.frame([1.0, 2.0], name="a")
        second = self.frame([1.0, np.nan], name="b")
        components = pd.concat([first, second], ignore_index=True)
        strict = combine(components, {"a": 0.5, "b": 0.5})
        self.assertTrue(pd.isna(strict.value.iloc[1]))
        lowest = combine(components, {"a": 0.5, "b": 0.5}, missing_policy="lowest")
        np.testing.assert_allclose(lowest.value, [0.75, 0.75])
        self.assertEqual(lowest.missing_reason.iloc[1], "lowest_rank_imputation")

    def test_transformations_preserve_source(self):
        original = self.frame([1.0, 2.0, 100.0])
        saved = original.copy(deep=True)
        standardize(original)
        winsorize(original)
        assert_frame_equal(original, saved)


if __name__ == "__main__":
    unittest.main(verbosity=2)
