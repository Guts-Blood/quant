"""Rebuild effective CSI500 intervals from an anchored parent and dated, archived changes."""

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from zipfile import ZipFile

from .market_snapshot import manifest_bytes, read_manifest, verified, write_once
from .market_version import checksum, file_spec, open_snapshot
from .option_capture import timestamp


def roster(codes):
    codes = list(codes)
    if len(set(codes)) != len(codes) or any(not re.fullmatch(r"(SH|SZ)[0-9]{6}", c) for c in codes):
        raise ValueError("Invalid or duplicate security codes in membership evidence")
    return set(codes)


def rebuild(base, calendar_batch, bundle, output, through):
    with ZipFile(bundle) as z:
        plan = json.loads(z.read("timeline.json"))
        for source, meta in plan["sources"].items():
            if not meta["source_id"] or hashlib.sha256(z.read(source)).hexdigest() != meta["sha256"]:
                raise ValueError(f"Changed membership evidence: {source}")
            timestamp(meta["collected_at"])
        for item in [*plan["events"], *plan["anchors"]]:
            if not item["source_files"] or not set(item["source_files"]) <= plan["sources"].keys():
                raise ValueError("All membership changes and checks require archived evidence")
    batch = read_manifest(calendar_batch)
    specs = {s["name"]: s for s in batch["files"]}
    for name, spec in specs.items():
        if not verified(calendar_batch.parent / name, spec):
            raise ValueError(f"Calendar input changed: {name}")
    dates = [
        date.fromisoformat(d) for d in (calendar_batch.parent / "calendar.txt").read_text().splitlines()
    ]
    anchor = date.fromisoformat(plan["seed_date"])
    if through not in dates or anchor not in dates or anchor >= through:
        raise ValueError("Membership bounds must be observed sessions")
    anchors = {date.fromisoformat(a["date"]): roster(a["codes"]) for a in plan["anchors"]}
    if not anchors or max(anchors) < through or anchor not in anchors:
        raise ValueError("Require a full seed check and a closing roster check")
    if any(len(s) != 500 for s in anchors.values()):
        raise ValueError("A checkpoint needs exactly 500 distinct constituents")
    with open_snapshot(base) as (db, parent, digest):
        if specs["calendar.txt"].get("parent_manifest_sha256") != digest:
            raise ValueError("Calendar and membership must extend the same parent")
        six = {
            r[0]
            for r in db.execute("SELECT stock_code FROM csi500_members WHERE date=?", [anchor]).fetchall()
        }
    initial = anchors[anchor]
    if {c[2:] for c in initial} != six:
        raise ValueError("Seed disagrees with parent")
    events = {}
    for e in plan["events"]:
        day = date.fromisoformat(e["effective_date"])
        if (
            not anchor < day <= through
            or day not in dates
            or day in events
            or date.fromisoformat(e["published_on"]) >= day
        ):
            raise ValueError("Invalid or ambiguous event timing")
        removed, added = roster(e["removed"]), roster(e["added"])
        if not removed or len(removed) != len(added) or removed & added:
            raise ValueError("Unbalanced membership change")
        events[day] = (removed, added)
    active = set(initial)
    intervals = []
    for line in (base / "csi500.tsv").read_text().splitlines():
        code, start, end = line.split()
        if start <= str(anchor):
            intervals.append((code, start, min(end, str(anchor))))
    first = dates[dates.index(anchor) + 1]
    starts = {c: first for c in active}
    previous = anchor
    for day in dates[dates.index(anchor) + 1 : dates.index(through) + 1]:
        if day in events:
            removed, added = events[day]
            if not removed <= active or added & active:
                raise ValueError(f"Change is inconsistent with the previous roster at {day}")
            for code in removed:
                start = starts.pop(code)
                if start <= previous:
                    intervals.append((code, str(start), str(previous)))
            starts.update({c: day for c in added})
            active = (active - removed) | added
        if day in anchors and active != anchors[day]:
            raise ValueError(f"Roster mismatch on {day}")
        previous = day
    if any(active != codes for d, codes in anchors.items() if d >= through):
        raise ValueError("Final reconstructed roster disagrees with the independent closing roster")
    intervals.extend((c, str(start), str(through)) for c, start in starts.items())
    output.mkdir(parents=True, exist_ok=True)
    raw_name = "reference-" + checksum(bundle) + "-membership.zip"
    write_once(output / raw_name, bundle.read_bytes())
    write_once(
        output / "csi500.tsv", ("".join("\t".join(row) + "\n" for row in sorted(intervals))).encode()
    )
    acquired = plan["collected_at"]
    timestamp(acquired)
    src = plan["source_id"]
    raw = file_spec(
        output / raw_name,
        raw_name,
        "reference_raw",
        src,
        acquired,
        coverage=[str(anchor), str(max(anchors))],
    )
    member = file_spec(
        output / "csi500.tsv",
        "csi500.tsv",
        "csi500_intervals",
        src,
        acquired,
        raw_file=raw_name,
        parent_manifest_sha256=digest,
        parent_snapshot=parent["snapshot"],
        reconstruction={
            "seed_date": str(anchor),
            "through": str(through),
            "official_events": len(events),
            "checkpoints": [str(d) for d in sorted(anchors)],
            "basis": "published changes, independently checked rosters",
        },
    )
    for name in specs:
        write_once(output / name, (calendar_batch.parent / name).read_bytes())
    path = output / "inputs-manifest.json"
    write_once(
        path, manifest_bytes({"snapshot": "reference-update", "files": [*batch["files"], member, raw]})
    )
    return path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("base", type=Path)
    p.add_argument("calendar_manifest", type=Path)
    p.add_argument("evidence_zip", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--through", type=date.fromisoformat, required=True)
    a = p.parse_args()
    print(rebuild(a.base, a.calendar_manifest, a.evidence_zip, a.output, a.through))


if __name__ == "__main__":
    main()
