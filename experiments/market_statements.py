"""Prepare sourced statement versions for a snapshot and select facts at a decision cutoff."""

import argparse
import json
from pathlib import Path
import re
import tempfile

import duckdb
import pandas as pd

from factorlab.data.asof import asof_records, validate_fundamentals
from factorlab.data.market_catalog import literal
from .market_snapshot import write_once
from .market_version import open_snapshot


def read_disclosures(path):
    facts = []
    for report in json.loads(path.read_bytes()):
        published = report["published"]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", published):
            # For a date-only notice the first supported instant is the next local midnight.
            bound = pd.Timestamp(published, tz="Asia/Shanghai")
            precision, available = "date", bound + pd.Timedelta(days=1)
        else:
            bound = pd.Timestamp(published)
            if bound.tz is None:
                raise ValueError("Publication timestamp needs an explicit offset")
            precision, available = "timestamp", bound
        for field, basis, unit, value, locator in report["facts"]:
            if (
                not re.fullmatch(r"[0-9]{6}", report["symbol"])
                or basis not in {"instant", "quarter", "ytd", "ttm"}
                or unit not in {"CNY", "shares"}
                or not locator.strip()
            ):
                raise ValueError(f"Invalid statement fact: {field}")
            # Separate flow bases in the existing as-of primary key.
            facts.append(
                dict(
                    symbol=report["symbol"],
                    field=f"{field}.{basis}",
                    metric=field,
                    basis=basis,
                    unit=unit,
                    period_end=report["period_end"],
                    publication_label=published,
                    published_at=bound,
                    time_precision=precision,
                    available_at=available,
                    ingested_at=report["ingested_at"],
                    revision=report["revision"],
                    value=value,
                    source_id=report["source"],
                    statement_line=locator,
                )
            )
    return validate_fundamentals(pd.DataFrame(facts)).sort_values(
        ["symbol", "field", "period_end", "revision"]
    )


def prepare(path, dest):
    values = read_disclosures(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(config={"threads": 1}) as db, tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        db.register("facts", values)
        part = Path(tmp) / "facts.parquet"
        db.execute(
            f"COPY (SELECT * FROM facts ORDER BY symbol,field,period_end,revision) TO {literal(part)} (FORMAT PARQUET)"
        )
        write_once(dest, part.read_bytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    cmds = parser.add_subparsers(dest="command", required=True)
    add = cmds.add_parser("prepare")
    add.add_argument("disclosures", type=Path)
    add.add_argument("output", type=Path)
    view = cmds.add_parser("asof")
    view.add_argument("snapshot", type=Path)
    view.add_argument("symbol")
    view.add_argument("cutoff")
    view.add_argument("--mode", choices=("public", "observed"), required=True)
    a = parser.parse_args()
    if a.command == "prepare":
        prepare(a.disclosures, a.output)
    else:
        with open_snapshot(a.snapshot) as (db, _, _):
            frame = db.execute("SELECT * FROM statement_facts WHERE symbol=?", [a.symbol]).df()
            if not frame.empty:
                frame = asof_records(frame, a.cutoff, mode=a.mode)
            print(frame.to_json(orient="records", date_format="iso", force_ascii=False))


if __name__ == "__main__":
    main()
