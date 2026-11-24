"""Reproducible local experiments from data to diagnostics and portfolio results."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
import sys
import traceback
from uuid import uuid4

import numpy as np
import pandas as pd

from .. import __version__
from ..artifacts import atomic_write, write_json
from ..backtest.daily import simulate
from ..data.inputs import load_inputs
from ..evaluation.metrics import evaluate, factor_correlations
from ..factors import compute
from ..labels import forward_returns, within_split
from ..portfolio import target_weights
from ..reporting import render_report
from ..transforms import combine, neutralize, standardize, winsorize
from ..universe import eligible_universe
from .config import load_config, validate_config


class ExperimentFailure(RuntimeError):
    def __init__(self, output, message):
        self.output = Path(output)
        super().__init__(f"{message}; failure record: {self.output / 'manifest.json'}")


def now():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [clean(item) for item in value]
    if isinstance(value, np.generic):
        return clean(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    return value


def fingerprint(path):
    content = Path(path).read_bytes()
    return {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}


def code_identity():
    package = Path(__file__).resolve().parents[1]
    hashes = {path.relative_to(package).as_posix(): fingerprint(path)
              for path in sorted(package.rglob("*.py"))}
    root = next((parent for parent in package.parents if (parent / "pyproject.toml").is_file()), None)
    revision = None
    if root is not None:
        hashes["../pyproject.toml"] = fingerprint(root / "pyproject.toml")
        git = root / ".git"
        if git.is_dir() and (git / "HEAD").is_file():
            head = (git / "HEAD").read_text(encoding="utf-8").strip()
            if head.startswith("ref: "):
                ref = head[5:]
                ref_path = git / ref
                if ref_path.is_file():
                    revision = ref_path.read_text(encoding="utf-8").strip()
                elif (git / "packed-refs").is_file():
                    for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
                        if line.endswith(" " + ref):
                            revision = line.split()[0]
            else:
                revision = head
    return {"factorlab_version": __version__, "git_revision": revision,
            "identity_basis": "source_file_hashes_include_uncommitted_code",
            "source_files": hashes}


def environment():
    packages = {}
    for name in ("numpy", "pandas", "matplotlib"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = "not-installed"
    return {"python": sys.version, "platform": platform.platform(), "packages": packages}


def save_table(output, name, frame):
    path = Path(output) / name
    atomic_write(path, frame.to_csv(index=False, lineterminator="\n"))


def _splits(config, calendar):
    splits = []
    for item in config["splits"]:
        start, end = pd.Timestamp(item["start"]), pd.Timestamp(item["end"])
        if start not in calendar or end not in calendar or start > end:
            raise ValueError("split bounds must be ordered calendar sessions")
        splits.append(item | {"start": start.date().isoformat(), "end": end.date().isoformat()})
    splits.sort(key=lambda value: value["start"])
    role_order = {"train": 0, "validation": 1, "test": 2}
    for left, right in zip(splits, splits[1:]):
        if left["end"] >= right["start"]:
            raise ValueError("splits overlap")
        if left["role"] == "explore" or right["role"] == "explore":
            raise ValueError("exploration must be an independent single-split experiment")
        if role_order[left["role"]] > role_order[right["role"]]:
            raise ValueError("split roles must follow train, validation, test chronology")
    return splits


def _diagnostic_summary(daily, *, split, role, horizon):
    rows = []
    for factor, part in daily.groupby("factor", sort=True):
        ic = part.rank_ic.dropna()
        scale = ic.std(ddof=1)
        rows.append({"split": split, "role": role, "factor": factor, "horizon": horizon,
            "sessions": len(part), "ic_observations": len(ic), "mean_rank_ic": ic.mean(),
            "rank_ic_ir": ic.mean() / scale if scale > 0 else np.nan,
            "mean_group_spread": part.group_spread.mean(),
            "factor_coverage": part.factor_coverage.mean(), "outcome_coverage": part.outcome_coverage.mean()})
    return rows


def run_experiment(config, *, base_dir, output_root=None, config_source=None, split_roles=None):
    """Run a fresh immutable directory; record ordinary failures before raising.

    split_roles is used by scans to exclude held-out diagnostics and portfolios.
    Input hashes and source hashes, not HEAD alone, identify the actual run.
    """
    base_dir = Path(base_dir).resolve()
    root = Path(output_root).resolve() if output_root is not None else (base_dir / config.get("output_dir", "../outputs")).resolve()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:10]
    output = root / run_id
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"schema_version": 1, "run_id": run_id, "status": "running", "started_at": now(),
                "config": clean(config), "config_source": config_source,
                "code": code_identity(), "environment": environment()}
    write_json(output / "manifest.json", manifest)
    try:
        config = validate_config(config)
        manifest["config"] = config
        write_json(output / "config.resolved.json", config)
        data = load_inputs(config["data"], base_dir=base_dir)
        manifest["data"] = data.provenance
        splits = _splits(config, data.calendar)
        selected = [split for split in splits if split_roles is None or split["role"] in split_roles]
        if not selected:
            raise ValueError("no eligible experiment splits")
        manifest["evaluated_splits"] = selected
        manifest["excluded_splits"] = [split for split in splits if split not in selected]
        # Scans retain warm-up history but never calculate features or labels in held-out periods.
        last_day = max(pd.Timestamp(split["end"]) for split in selected)
        calendar = data.calendar[data.calendar <= last_day]
        panel = data.panel.loc[data.panel.date <= last_day].copy()
        universe_options = {key: value for key, value in config["universe"].items() if key != "mode"}
        clock = config["data"].get("signal_time", "16:00")
        universe = eligible_universe(panel, calendar=calendar, membership=data.membership,
                                     signal_time=clock, availability_mode=config["data"]["availability_mode"], **universe_options)
        save_table(output, "universe.csv", universe)
        parts = []
        for spec in config["factors"]:
            factor = compute(panel, spec["name"], calendar=calendar, parameters=spec.get("parameters"))
            factor["factor"] = spec["id"]
            parts.append(factor)
        raw = pd.concat(parts, ignore_index=True)
        save_table(output, "factors_raw.csv", raw)
        scores = raw.merge(universe[["date", "symbol", "eligible"]], on=["date", "symbol"], validate="many_to_one")
        scores.loc[~scores.eligible, "value"] = np.nan
        scores.loc[~scores.eligible, "missing_reason"] = "outside_eligible_universe"
        scores = scores.drop(columns="eligible")
        for transform in config.get("transforms", []):
            name = transform["name"]
            options = {key: value for key, value in transform.items() if key != "name"}
            if name == "winsorize":
                scores = winsorize(scores, **options)
            elif name == "standardize":
                scores = standardize(scores)
            else:
                scores = neutralize(scores, panel, signal_time=clock, **options)
        if "blend" in config:
            eligible_keys = universe.loc[universe.eligible, ["date", "symbol"]]
            components = scores.merge(eligible_keys, on=["date", "symbol"], validate="many_to_one")
            if components.empty:
                blend = universe[["date", "symbol"]].copy()
                blend["value"] = np.nan
                blend["missing_reason"] = "outside_eligible_universe"
            else:
                observed_blend = combine(components, **config["blend"])
                blend = universe[["date", "symbol"]].merge(
                    observed_blend.drop(columns="factor"), on=["date", "symbol"], how="left", validate="one_to_one")
                blend["missing_reason"] = blend.missing_reason.fillna("outside_eligible_universe")
            blend["factor"] = config["blend"]["name"]
            scores = pd.concat([scores, blend], ignore_index=True)
        save_table(output, "factors.csv", scores)
        portfolio_options = config.get("portfolio", {}).copy()
        chosen_factor = portfolio_options.pop("factor", config["factors"][0]["id"])
        targets = target_weights(scores.loc[scores.factor == chosen_factor], **portfolio_options)
        save_table(output, "targets.csv", targets)
        label_options = config.get("labels", {}).copy()
        horizons = label_options.pop("horizons", [1, 5, 20])
        label_options.setdefault("price_column", "adj_open")
        label_options.setdefault("entry_lag", 1)
        outcomes = {horizon: forward_returns(panel, calendar=calendar, horizon=horizon, **label_options)
                    for horizon in horizons}
        evaluation_options = config.get("evaluation", {})
        diagnostics, backtests, curves = [], [], {}
        for split in selected:
            name, start, end, role = (split[key] for key in ("name", "start", "end", "role"))
            selected_scores = scores.loc[scores.date.between(start, end)]
            correlations = factor_correlations(selected_scores,
                              min_observations=evaluation_options.get("min_observations", 20))
            save_table(output, f"{name}/factor_correlations.csv", correlations)
            for horizon, labels in outcomes.items():
                purged = within_split(labels, start, end)
                save_table(output, f"{name}/horizon-{horizon}/labels.csv", purged)
                result = evaluate(selected_scores, purged, eligibility=universe, **evaluation_options)
                for table, frame in result.items():
                    save_table(output, f"{name}/horizon-{horizon}/{table}.csv", frame)
                diagnostics.extend(_diagnostic_summary(result["daily"], split=name, role=role, horizon=horizon))
            execution_options = config.get("backtest", {}).copy()
            enabled = execution_options.pop("enabled", True)
            if enabled:
                execution_options.setdefault("max_positions", portfolio_options.get("top_n", 10))
                result = simulate(panel, targets, calendar=calendar, start=start, end=end,
                                  signal_time=clock, **execution_options)
                for table in ("daily", "orders", "holdings", "events"):
                    save_table(output, f"{name}/backtest/{table}.csv", result[table])
                write_json(output / name / "backtest" / "summary.json", clean(result["summary"]))
                manifest.setdefault("execution_assumptions", {})[name] = result["assumptions"]
                backtests.append({"split": name, "role": role, "factor": chosen_factor,
                                  **{key: value for key, value in result["summary"].items() if key != "rejections"}})
                curves[name] = (result["daily"], result["summary"]["initial_cash"])
        diagnostics = pd.DataFrame(diagnostics)
        backtests = pd.DataFrame(backtests)
        save_table(output, "diagnostics.csv", diagnostics)
        save_table(output, "backtests.csv", backtests)
        manifest["diagnostics"] = clean(diagnostics.to_dict("records"))
        manifest["backtests"] = clean(backtests.to_dict("records"))
        render_report(output, manifest=manifest, diagnostics=diagnostics, backtests=backtests, curves=curves)
        manifest["artifacts"] = {path.relative_to(output).as_posix(): fingerprint(path)
            for path in sorted(output.rglob("*")) if path.is_file() and path.name != "manifest.json"}
        manifest.update(status="completed", finished_at=now())
        write_json(output / "manifest.json", clean(manifest))
        return output
    except Exception as exc:
        manifest.update(status="failed", finished_at=now(), error={"type": type(exc).__name__, "message": str(exc)})
        atomic_write(output / "error.txt", traceback.format_exc())
        write_json(output / "manifest.json", clean(manifest))
        raise ExperimentFailure(output, str(exc)) from exc


def run_file(path, *, output_root=None):
    config, source, raw = load_config(path)
    return run_experiment(config, base_dir=source.parent, output_root=output_root,
                          config_source={"path": str(source), "sha256": hashlib.sha256(raw).hexdigest()})
