"""Record point-in-time reverse DCF and scenarios against a frozen shortlist."""

import argparse
from datetime import datetime
import json
from pathlib import Path

import pandas as pd

from .legacy_pilot.risk_expectations import equity_to_operating_ev, forward_dcf, reverse_dcf
from .market_snapshot import write_once
from .market_version import checksum
from .option_capture import CST, encode, timestamp


def annotate(run_dir, inputs, output):
    run = json.loads((run_dir / "run.json").read_bytes())
    candidates = run_dir / "candidates.csv"
    if checksum(candidates) != run["outputs"]["candidates.csv"]:
        raise ValueError("Candidate file does not match the original run")
    raw = json.loads(inputs.read_bytes())
    rows = pd.read_csv(candidates, dtype={"symbol": str}).to_dict("records")
    keys = {(r["signal_date"], r["symbol"]) for r in rows}
    entries = {}
    for entry in raw:
        key = entry["signal_date"], entry["symbol"]
        if key not in keys or key in entries:
            raise ValueError(f"Duplicate or non-shortlisted DCF input: {key}")
        entries[key] = entry
    notes = []
    for row in rows:
        day, code = row["signal_date"], row["symbol"]
        item = {
            "signal_date": day,
            "trade_date": row["trade_date"],
            "symbol": code,
            "rank": row["rank"],
            "price": row["close"],
        }
        entry = entries.get((day, code))
        if entry is None:
            notes.append(dict(item, status="missing_inputs"))
            continue
        cutoff = datetime.fromisoformat(day + "T" + run["config"]["signal_time"]).replace(tzinfo=CST)
        if not entry.get("source_id") or timestamp(entry["available_at"]) > cutoff:
            raise ValueError(f"Missing source or scenario arrived after the signal: {day} {code}")
        if entry["shares"] <= 0:
            raise ValueError("Positive shares outstanding required")
        bridge = equity_to_operating_ev(
            market_cap=row["close"] * entry["shares"],
            **{k: entry[k] for k in ("debt", "cash", "minority_interests", "nonoperating_assets")},
        )
        model = entry["model"]
        implied = reverse_dcf(bridge["operating_enterprise_value"], model)
        scenarios = {}
        for label in ("attainable", "stress"):
            growth = entry[label + "_growth"]
            scenarios[label] = {
                "growth": growth,
                "operating_ev": forward_dcf(growth, model)["enterprise_value"],
            }
        notes.append(
            dict(
                item,
                status=implied["status"],
                inputs=entry,
                operating_ev_bridge=bridge,
                implied_growth_roots=implied["roots"],
                scenarios=scenarios,
            )
        )
    value = {
        "snapshot": run["snapshot"],
        "manifest_sha256": run["manifest_sha256"],
        "original_candidates_sha256": checksum(candidates),
        "inputs_sha256": checksum(inputs),
        "run_sha256": checksum(run_dir / "run.json"),
        "method_sha256": checksum(Path(__file__)),
        "notes": notes,
    }
    payload = encode(value)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.resolve() in {inputs.resolve(), candidates.resolve(), (run_dir / "run.json").resolve()}:
        raise ValueError("Choose a separate output file")

    write_once(output, payload)
    return value


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", type=Path)
    p.add_argument("inputs", type=Path)
    p.add_argument("output", type=Path)
    a = p.parse_args()
    result = annotate(a.run_dir, a.inputs, a.output)
    print(f"{len(result['notes'])} original shortlist records: {a.output}")


if __name__ == "__main__":
    main()
