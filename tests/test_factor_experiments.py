"""Offline end-to-end experiments, provenance, split purging and failed scans."""
from __future__ import annotations

import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import pandas as pd

from factorlab.cli import main
from factorlab.demo import create_demo
from factorlab.experiments import ExperimentFailure, run_experiment, run_file, scan_file
from factorlab.experiments.config import validate_config


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = create_demo(self.root / "inputs")
        self.config = json.loads(self.path.read_text(encoding="utf-8"))

    def tearDown(self):
        self.temporary.cleanup()

    def run_config(self, config=None):
        return run_experiment(self.config if config is None else config,
                              base_dir=self.path.parent, output_root=self.root / "results")

    def test_complete_local_run_has_traceable_artifacts(self):
        folder = self.run_config()
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual({item["role"] for item in manifest["evaluated_splits"]}, {"train", "validation", "test"})
        self.assertIn("experiments/runner.py", manifest["code"]["source_files"])
        self.assertIn("pandas", manifest["environment"]["packages"])
        for name in ("report.html", "nav.png", "ic_decay.png", "factors.csv", "targets.csv"):
            path = folder / name
            self.assertTrue(path.is_file(), name)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), manifest["artifacts"][name]["sha256"])
        report = (folder / "report.html").read_text(encoding="utf-8")
        self.assertIn("data:image/png;base64,", report)
        self.assertNotIn("https://", report)
        self.assertGreater(sum(row["fills"] for row in manifest["backtests"]), 0)
        for split in self.config["splits"]:
            labels = pd.read_csv(folder / split["name"] / "horizon-5" / "labels.csv")
            self.assertTrue((pd.to_datetime(labels.label_end) <= pd.Timestamp(split["end"])).all())
            self.assertTrue((pd.to_datetime(labels.label_start) >= pd.Timestamp(split["start"])).all())
            orders = pd.read_csv(folder / split["name"] / "backtest" / "orders.csv")
            self.assertTrue((pd.to_datetime(orders.signal_date) < pd.to_datetime(orders.date)).all())

    def test_repeat_run_preserves_inputs_and_numerical_results(self):
        input_hash = hashlib.sha256((self.path.parent / "panel.csv").read_bytes()).hexdigest()
        first, second = self.run_config(), self.run_config()
        self.assertNotEqual(first, second)
        for file in ("factors.csv", "diagnostics.csv", "validation/backtest/daily.csv", "validation/backtest/orders.csv"):
            self.assertEqual((first / file).read_bytes(), (second / file).read_bytes())
        self.assertEqual(hashlib.sha256((self.path.parent / "panel.csv").read_bytes()).hexdigest(), input_hash)

    def test_failed_experiment_retains_configuration_and_reason(self):
        config = copy.deepcopy(self.config)
        config["factors"][0]["parameters"]["window"] = 0
        with self.assertRaises(ExperimentFailure) as caught:
            self.run_config(config)
        output = caught.exception.output
        record = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"]["type"], "ValueError")
        self.assertEqual(record["config"]["factors"][0]["parameters"]["window"], 0)
        self.assertTrue((output / "error.txt").is_file())
        self.assertFalse((output / "report.html").exists())

    def test_scan_keeps_failures_and_excludes_test_outputs(self):
        config = copy.deepcopy(self.config)
        config["scan"] = {"max_trials": 2, "grid": {"factors.rev.parameters.window": [0, 2]}}
        path = self.path.parent / "test-scan.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        output = scan_file(path, output_root=self.root / "scans")
        record = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "completed_with_failures")
        self.assertEqual([row["status"] for row in record["trials"]], ["failed", "completed"])
        self.assertFalse(record["automatic_winner_selection"])
        trial = output / record["trials"][1]["run"]
        result = json.loads((trial / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual({row["role"] for row in result["evaluated_splits"]}, {"train", "validation"})
        self.assertEqual([row["role"] for row in result["excluded_splits"]], ["test"])
        self.assertFalse((trial / "test").exists())
        factors = pd.read_csv(trial / "factors_raw.csv")
        self.assertLessEqual(pd.to_datetime(factors.date).max(), pd.Timestamp(config["splits"][1]["end"]))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), config)

    def test_overlapping_splits_are_rejected(self):
        config = copy.deepcopy(self.config)
        config["splits"][1]["start"] = config["splits"][0]["end"]
        with self.assertRaisesRegex(ExperimentFailure, "splits overlap"):
            self.run_config(config)

    def test_lowest_rank_blend_never_reintroduces_excluded_names(self):
        config = copy.deepcopy(self.config)
        config["blend"] = {"name": "combo", "weights": {"rev": 1, "mom": 1}, "missing_policy": "lowest"}
        config["portfolio"]["factor"] = "combo"
        folder = self.run_config(config)
        scores = pd.read_csv(folder / "factors.csv")
        universe = pd.read_csv(folder / "universe.csv")
        joined = scores.loc[scores.factor == "combo"].merge(universe, on=["date", "symbol"], validate="one_to_one")
        self.assertTrue(joined.loc[~joined.eligible, "value"].isna().all())
        self.assertTrue(joined.loc[joined.eligible, "value"].notna().any())

    def test_configuration_rejects_ambiguous_universe_and_typos(self):
        for key, value in (("universe", {"mode": "all_stocks"}), ("unknown_option", True)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_config(self.config | {key: value})
        with self.assertRaises(FileExistsError):
            create_demo(self.path.parent)

    def test_cli_and_relative_input_paths(self):
        with redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(main(["factors"]), 0)
        self.assertIn("reversal", captured.getvalue())
        output = run_file(self.path, output_root=self.root / "file-run")
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["config_source"]["path"], str(self.path.resolve()))
        self.assertEqual(manifest["data"]["files"][0]["path"], str((self.path.parent / "panel.csv").resolve()))


if __name__ == "__main__":
    unittest.main()
