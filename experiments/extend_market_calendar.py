"""Extend an exchange calendar only where the source and both actual stock feeds agree."""

import argparse
from datetime import date
from pathlib import Path

from .market_snapshot import manifest_bytes, write_once
from .market_version import checksum, file_spec, open_snapshot
from .option_capture import timestamp


def extend(base, source_file, output, through, source_id, acquired):
    timestamp(acquired)
    if not source_id.strip():
        raise ValueError("Identify the upstream calendar and its revision")
    text = source_file.read_text(encoding="utf-8-sig")
    supplied = [date.fromisoformat(line.strip()) for line in text.splitlines()]
    if not supplied or supplied != sorted(set(supplied)):
        raise ValueError("Expected ordered unique exchange sessions")
    with open_snapshot(base) as (db, parent, digest):
        old = [r[0] for r in db.execute("SELECT date FROM trading_calendar ORDER BY date").fetchall()]
        if not old or not supplied[0] <= old[-1] < through <= supplied[-1]:
            raise ValueError("The new calendar must overlap the parent and reach the requested date")
        overlap = [d for d in old if supplied[0] <= d <= old[-1]]
        if overlap != [d for d in supplied if old[0] <= d <= old[-1]]:
            raise ValueError("Calendar disagrees with the overlapping parent dates")
        added = [d for d in supplied if old[-1] < d <= through]
        if not added or added[-1] != through:
            raise ValueError("Specify a last observed trading session")
        for name in ("daily", "valuation"):
            rows = db.execute(
                f"SELECT DISTINCT CAST(date AS DATE) AS session_date FROM {name} WHERE date>? AND date<=? ORDER BY session_date",
                [old[-1], through],
            ).fetchall()
            if added != [r[0] for r in rows]:
                raise ValueError(f"Calendar disagrees with {name} on extension dates")
    output.mkdir(parents=True, exist_ok=True)
    raw_name = "reference-" + checksum(source_file) + "-calendar.txt"
    write_once(output / raw_name, source_file.read_bytes())
    write_once(output / "calendar.txt", ("".join(f"{x}\n" for x in old + added)).encode())
    raw = file_spec(
        output / raw_name,
        raw_name,
        "reference_raw",
        source_id,
        acquired,
        coverage=[str(supplied[0]), str(supplied[-1])],
    )
    merged = file_spec(
        output / "calendar.txt",
        "calendar.txt",
        "trading_calendar",
        source_id,
        acquired,
        raw_file=raw_name,
        parent_manifest_sha256=digest,
        parent_snapshot=parent["snapshot"],
        added_sessions=len(added),
        checked_through=str(through),
    )
    path = output / "inputs-manifest.json"
    write_once(path, manifest_bytes({"snapshot": "calendar-extension", "files": [merged, raw]}))
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path)
    parser.add_argument("source_file", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--through", type=date.fromisoformat, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--collected-at", required=True)
    a = parser.parse_args()
    print(extend(a.base, a.source_file, a.output, a.through, a.source, a.collected_at))


if __name__ == "__main__":
    main()
