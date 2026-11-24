"""Explicit finite parameter grids; retain all trials and exclude held-out splits."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
from html import escape
from itertools import product
import json
import math
from pathlib import Path
from uuid import uuid4

import pandas as pd

from ..artifacts import atomic_write, write_json
from .config import allowed, load_config, validate_config
from .runner import ExperimentFailure, clean, code_identity, environment, fingerprint, now, run_experiment, save_table


def _set_parameter(config, path, value):
    pieces = path.split(".")
    if pieces[0] == "factors" and len(pieces) == 4 and pieces[2] == "parameters":
        factor = next((item for item in config["factors"] if item["id"] == pieces[1]), None)
        if factor is None:
            raise ValueError(f"unknown factor id in scan path: {path}")
        factor.setdefault("parameters", {})[pieces[3]] = value
        return
    if pieces[0] not in {"portfolio", "backtest", "blend"} or len(pieces) < 2:
        raise ValueError(f"scan path cannot change data or evaluation splits: {path}")
    target = config
    for part in pieces[:-1]:
        target = target.setdefault(part, {})
        if not isinstance(target, dict):
            raise ValueError(f"scan path is not a table: {path}")
    target[pieces[-1]] = value


def scan_file(path, *, output_root=None):
    config, source, raw = load_config(path)
    config = validate_config(config)
    scan = config.get("scan", {})
    allowed(scan, {"grid", "max_trials"}, "scan")
    grid = scan.get("grid")
    if not isinstance(grid, dict) or not grid or any(not isinstance(values, list) or not values for values in grid.values()):
        raise ValueError("scan.grid must contain nonempty lists of candidate values")
    maximum = scan.get("max_trials", 64)
    count = math.prod(len(values) for values in grid.values())
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1 or count > maximum:
        raise ValueError(f"grid has {count} trials; increase the explicit max_trials to permit it")
    roles = {split["role"] for split in config["splits"]}
    if not {"train", "validation"}.issubset(roles):
        raise ValueError("parameter scans require separate train and validation splits")
    for key, values in grid.items():
        _set_parameter(copy.deepcopy(config), key, values[0])
    root = Path(output_root).resolve() if output_root is not None else (source.parent / config.get("output_dir", "../outputs")).resolve()
    scan_id = "scan-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:10]
    output = root / scan_id
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"schema_version": 1, "scan_id": scan_id, "status": "running", "started_at": now(),
        "config": config, "config_source": {"path": str(source), "sha256": hashlib.sha256(raw).hexdigest()},
        "planned_trials": count, "evaluated_roles": ["train", "validation"],
        "excluded_roles": ["test", "explore"], "automatic_winner_selection": False,
        "code": code_identity(), "environment": environment(), "trials": []}
    write_json(output / "manifest.json", manifest)
    metrics, portfolio_rows = [], []
    keys = list(grid)
    for index, values in enumerate(product(*(grid[key] for key in keys)), start=1):
        parameters = dict(zip(keys, values))
        trial_config = copy.deepcopy(config)
        trial_config.pop("scan", None)
        for key, value in parameters.items():
            _set_parameter(trial_config, key, value)
        row = {"trial": index, "parameters": parameters}
        try:
            trial_dir = run_experiment(trial_config, base_dir=source.parent,
                          output_root=output / "trials", config_source=manifest["config_source"],
                          split_roles={"train", "validation"})
            result = json.loads((trial_dir / "manifest.json").read_text(encoding="utf-8"))
            row.update(status="completed", run=trial_dir.relative_to(output).as_posix())
            metrics.extend({"trial": index, **record} for record in result["diagnostics"])
            portfolio_rows.extend({"trial": index, **record} for record in result["backtests"])
        except ExperimentFailure as exc:
            row.update(status="failed", run=exc.output.relative_to(output).as_posix(), error=str(exc))
        manifest["trials"].append(row)
        write_json(output / "manifest.json", clean(manifest))
    flat = [{**row, "parameters": json.dumps(row["parameters"], ensure_ascii=False, sort_keys=True)}
            for row in manifest["trials"]]
    save_table(output, "trials.csv", pd.DataFrame(flat))
    save_table(output, "diagnostics.csv", pd.DataFrame(metrics))
    save_table(output, "backtests.csv", pd.DataFrame(portfolio_rows))
    failed = sum(row["status"] == "failed" for row in manifest["trials"])
    manifest.update(status="completed_with_failures" if failed else "completed", finished_at=now(),
                    completed_trials=count - failed, failed_trials=failed)
    links = "".join(f'<li>Trial {row["trial"]}: {escape(row["status"])} — '
                     f'<a href="{escape(row["run"], quote=True)}/manifest.json">record</a></li>'
                     for row in manifest["trials"])
    html = f'''<!doctype html><html lang="en"><meta charset="utf-8">
<title>{escape(config['name'])} scan</title><style>body{{font:15px/1.6 system-ui;max-width:1100px;margin:36px auto;padding:20px}}table{{border-collapse:collapse}}th,td{{padding:10px;border:1px solid #ddd;text-align:left}}</style>
<h1>{escape(config['name'])}: parameter scan</h1>
<p>{count} trials; {failed} failures. All trials are retained in configured order.</p>
<p>Only train and validation periods are evaluated. Test periods are excluded.
No winner is selected automatically. Repeated searches on validation data can also overfit;
freeze the chosen specification before a separate held-out evaluation.</p>
{pd.DataFrame(flat).to_html(index=False, escape=True, border=0)}<ul>{links}</ul>
<p><a href="diagnostics.csv">Diagnostics</a> · <a href="backtests.csv">Portfolio comparison</a> ·
<a href="trials.csv">All trials</a> · <a href="manifest.json">Manifest</a></p></html>'''
    atomic_write(output / "report.html", html)
    manifest["artifacts"] = {path.name: fingerprint(path) for path in output.iterdir()
                             if path.is_file() and path.name != "manifest.json"}
    write_json(output / "manifest.json", clean(manifest))
    return output
