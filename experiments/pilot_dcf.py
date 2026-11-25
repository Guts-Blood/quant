"""Reconcile one shortlisted non-financial company to its point-in-time statement lines."""

import argparse
import json
from pathlib import Path
import tempfile

from factorlab.data.asof import asof_records
from .market_version import checksum, open_snapshot
from .shortlist_dcf import annotate

DEFAULT = Path(__file__).resolve().parents[1] / "configs/market-data/pilot-dcf.json"


def build_case(snapshot, run_dir, output, config=DEFAULT):
    cfg = json.loads(config.read_bytes())
    code, day = cfg["symbol"], cfg["signal_date"]
    receipt = json.loads((run_dir / "run.json").read_bytes())
    cutoff = day + "T" + receipt["config"]["signal_time"] + ":00+08:00"
    with open_snapshot(snapshot) as (db, _, digest):
        if digest != receipt["manifest_sha256"]:
            raise ValueError("Financial facts and candidates must use the same snapshot")
        frame = db.execute(
            "SELECT * FROM statement_facts WHERE symbol=? AND period_end=?", [code, cfg["period_end"]]
        ).df()
        selected = asof_records(frame, cutoff, mode="public").set_index("field")
        cap, close = db.execute(
            "SELECT market_cap,close FROM research_daily JOIN valuation USING(date,stock_code) WHERE date=? AND stock_code=?",
            [day, code],
        ).fetchone()

    def fact(name, basis="instant"):
        return float(selected.at[f"{name}.{basis}", "value"])

    shares = fact("shares")
    if abs(shares * close - cap) > 1:
        raise ValueError("Disclosed shares do not reconcile to same-day stock capitalization")
    revenue = fact("revenue", "ytd")
    margin = 1 - sum(fact(k, "ytd") for k in cfg["core_expenses"]) / revenue
    entry = {
        "symbol": code,
        "signal_date": day,
        "shares": shares,
        "cash": fact("cash_equivalents"),
        "debt": sum(fact(k) for k in cfg["debt_fields"]),
        "minority_interests": fact("minority_interests"),
        "nonoperating_assets": sum(fact(k) for k in cfg["nonoperating_fields"]),
        "source_id": selected.source_id.unique().tolist(),
        "available_at": selected.available_at.max().isoformat(),
        "assumptions": cfg,
        "assumptions_sha256": checksum(config),
        "historical_sensitivity": True,
        "source_lines": json.loads(selected.reset_index().to_json(orient="records", date_format="iso")),
        "model": {
            **{
                k: cfg[k]
                for k in (
                    "tax_rate",
                    "sales_to_capital",
                    "discount_rate",
                    "terminal_growth",
                    "terminal_roic",
                )
            },
            "base_revenue": revenue * 4 / 3,
            "operating_margins": [margin] * cfg["years"],
            "terminal_margin": margin,
        },
        "attainable_growth": cfg["attainable_growth"],
        "stress_growth": cfg["stress_growth"],
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "inputs.json"
        path.write_text(json.dumps([entry], sort_keys=True))
        return annotate(run_dir, path, output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("snapshot", "run_dir", "output"):
        parser.add_argument(arg, type=Path)
    args = parser.parse_args()
    print(build_case(**vars(args))["coverage"])
