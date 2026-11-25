"""Compose immutable snapshots from an existing complete release and checked increments."""

import argparse
from contextlib import contextmanager
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

import duckdb

from factorlab.data.market_catalog import BASE_TABLES, install, inventory, preflight
from .market_snapshot import build_catalog, manifest_bytes, pin_target, read_manifest, verified, write_once
from .option_capture import encode, observation, quote_audit, timestamp

TABLE_FILES = {
    **{k + ".parquet": k for k in BASE_TABLES},
    "csi500.tsv": "csi500_intervals",
    "calendar.txt": "trading_calendar",
    "execution.parquet": "execution",
    "benchmark.parquet": "benchmark",
    "statement_facts.parquet": "statement_facts",
}


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_files(folder, manifest):
    for item in manifest["files"]:
        if not verified(folder / item["name"], item):
            raise ValueError(f"Snapshot checksum/size mismatch: {item['name']}")


@contextmanager
def open_snapshot(folder):
    path = folder / "manifest.json"
    manifest = read_manifest(path)
    digest = checksum(path)
    verify_files(folder, manifest)
    with duckdb.connect(str(folder / "market.duckdb"), read_only=True) as db:
        identity = db.execute("SELECT version, sha256, root FROM snapshot_identity").fetchone()
        if identity != (manifest["snapshot"], digest, str(folder.resolve())):
            raise ValueError(
                "Catalog does not match the pinned manifest or directory; rebuild with --catalog"
            )
        yield db, manifest, digest


def link_or_copy(source, dest):
    try:
        os.link(source, dest)
    except OSError:
        shutil.copyfile(source, dest)


def file_spec(path, name, dataset, source, collected_at, **extra):
    sha = checksum(path)
    return {
        "name": name,
        "dataset": dataset,
        "bytes": path.stat().st_size,
        "sha256": sha,
        "drive_parts": [f"{sha}-{name}"],
        "source": None,
        "source_id": source,
        "collected_at": collected_at,
        **extra,
    }


def prepare(folder, names, source, acquired):
    """Inventory complete replacement files, including the acquisition clock, before composition."""
    timestamp(acquired)
    if not source.strip() or len(set(names)) != len(names) or not names:
        raise ValueError("Supply unique table files and an identifiable source")
    files = []
    for name in sorted(names):
        if name not in TABLE_FILES:
            raise ValueError(f"Unknown replacement table: {name}")
        files.append(file_spec(folder / name, name, TABLE_FILES[name], source, acquired))
    payload = manifest_bytes({"snapshot": "replacement-inputs", "files": files})
    out = folder / "inputs-manifest.json"
    write_once(out, payload)
    return out


def compose(base, dest, *, captures=(), replacements=None):
    """Stage, validate, then atomically expose a new release; retries use identical inputs."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", dest.name) or dest.resolve() == base.resolve():
        raise ValueError("Provide a distinct named output version")
    parent_path = base / "manifest.json"
    parent = read_manifest(parent_path)
    # Only inherited bytes are dependencies. Fully replaced tables may live in cold storage.
    changes = read_manifest(replacements)["files"] if replacements else []
    old_hashes = {item["name"]: item["sha256"] for item in parent["files"]}
    replaced = {item["name"] for item in changes if item["sha256"] != old_hashes.get(item["name"])}
    inherited = [item for item in parent["files"] if item["name"] not in replaced]
    verify_files(base, {"files": inherited})
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".market-update-", dir=dest.parent) as temp:
        stage = Path(temp) / "release"
        stage.mkdir()
        specs = {}
        for old in inherited:
            name = old["name"]
            # The original release preserves only its archival day, not historical ingestion clocks.
            acquired = old.get("collected_at") or parent.get("archived_on")
            if not acquired:
                raise ValueError(f"Missing acquisition date for {name}")
            spec = {
                **old,
                "source_id": old.get("source_id") or old.get("source") or f"Drive archived {name}",
                "collected_at": acquired,
            }
            if not old.get("collected_at"):
                spec["collection_time_basis"] = "archive date, day precision"
            specs[name] = spec
            link_or_copy(base / name, stage / name)

        if changes:
            for item in changes:
                name = item["name"]
                reference_raw = item.get("dataset") == "reference_raw" and name.startswith(
                    "reference-" + item["sha256"] + "-"
                )
                if (
                    not reference_raw
                    and name not in TABLE_FILES
                    and name not in {"source-README.md", "upstream-manifest.json"}
                ):
                    raise ValueError(f"Unsupported replacement: {name}")
                path = replacements.parent / name
                if not verified(path, item) or not (item.get("source_id") or item.get("source")):
                    raise ValueError(f"Missing source or invalid checksum: {name}")
                timestamp(item["collected_at"])
                if name in specs and item["sha256"] == specs[name]["sha256"]:
                    continue
                (stage / name).unlink(missing_ok=True)
                shutil.copyfile(path, stage / name)
                specs[name] = {
                    **item,
                    "source_id": item.get("source_id") or item["source"],
                    "drive_parts": [f"{item['sha256']}-{name}"],
                }

        seen = set()
        for capture, kind in captures:
            record = observation(capture, "daily" if kind == "attempt" else kind)
            key = f"{record['underlying']}-{record['date']}-{kind}"
            if kind != "attempt" and key in seen:
                raise ValueError(f"Duplicate option partition in update: {key}")
            seen.add(key)
            raw_sha = checksum(capture)
            existing = next((s for s in specs.values() if s["sha256"] == raw_sha), None)
            if existing is None:
                raw_name = f"wire-{key}-{raw_sha}.zip"
                shutil.copyfile(capture, stage / raw_name)
                existing = file_spec(
                    stage / raw_name,
                    raw_name,
                    "option_wire",
                    record["source_id"],
                    record["ingested_at"],
                    coverage=[record["date"], record["date"]],
                )
                specs[raw_name] = existing
            if kind == "attempt":
                existing.update(
                    dataset="option_attempt",
                    kind=kind,
                    status=record["calculation"]["status"],
                    reason=record["calculation"].get("reason"),
                )
                continue
            record["wire_sha256"] = raw_sha
            name = f"options-{key}.json"
            (stage / name).unlink(missing_ok=True)
            (stage / name).write_bytes(encode(record))
            specs[name] = file_spec(
                stage / name,
                name,
                "option_chain",
                record["source_id"],
                record["ingested_at"],
                coverage=[record["date"], record["date"]],
                kind=kind,
                wire_file=existing["name"],
                wire_sha256=raw_sha,
            )
        with duckdb.connect() as db:
            install(db, stage, specs.values())
            for name in ("daily", "valuation", "execution"):
                if name + ".parquet" in specs:
                    bad = db.execute(f"""SELECT stock_code,date FROM {name} GROUP BY stock_code,date
                         HAVING count(*)>1 OR stock_code IS NULL OR date IS NULL LIMIT 1""").fetchone()
                    if bad:
                        raise ValueError(f"Duplicate or missing market key in {name}: {bad}")
            tables = inventory(db)
            joint = preflight(db)
        for name, item in specs.items():
            if name in TABLE_FILES:
                table = TABLE_FILES[name]
                item.update(dataset=table, coverage=tables[table]["coverage"])
            elif "coverage" not in item:
                item["coverage"] = (
                    [r["coverage"] for r in specs.values() if r.get("wire_file") == name] or [None]
                )[0]
        child = {
            "schema_version": 2,
            "snapshot": dest.name,
            "parent_snapshot": parent["snapshot"],
            "parent_manifest_sha256": checksum(parent_path),
            "tables": tables,
            "common": joint,
            "files": [specs[name] for name in sorted(specs)],
        }
        payload = manifest_bytes(child)
        (stage / "manifest.json").write_bytes(payload)
        read_manifest(stage / "manifest.json")
        digest = hashlib.sha256(payload).hexdigest()
        pin_target(stage, digest)
        try:
            stage.rename(dest)
        except OSError:
            if not (dest / "manifest.json").exists() or (dest / "manifest.json").read_bytes() != payload:
                raise ValueError(f"Conflicting existing version: {dest}")
            verify_files(dest, child)
        build_catalog(dest, child, digest)
    return dest / "manifest.json"


def audit_options(folder, manifest):
    """Re-parse each independent wire archive and verify the exact derived partition."""
    rows = []
    for item in manifest["files"]:
        if item.get("dataset") != "option_chain":
            continue
        wire = folder / item["wire_file"]
        if checksum(wire) != item["wire_sha256"]:
            raise ValueError(f"Option wire checksum mismatch: {wire.name}")
        row = observation(wire, item["kind"])
        row["wire_sha256"] = item["wire_sha256"]
        if (folder / item["name"]).read_bytes() != encode(row):
            raise ValueError(f"Recalculated partition differs: {item['name']}")
        result = row["calculation"]
        excluded = {q["contract_id"] for q in result["excluded_quotes"]}
        fresh_two_sided = sum(
            q["contract_id"] not in excluded and float(q["bid"]) > 0 for q in row["quotes"]
        )
        rows.append(
            {
                "date": row["date"],
                "kind": row["kind"],
                "asof": row["asof"],
                "available_at": row["available_at"],
                "received": row["quality"]["received"],
                "expected": row["quality"]["expected"],
                "quote_audit": quote_audit(row),
                "two_sided_fields": row["quality"]["with_bid_and_ask"],
                "fresh_accepted_two_sided": fresh_two_sided,
                "excluded": len(result["excluded_quotes"]),
                "max_accepted_age_seconds": result["max_quote_age_seconds"],
                "status": result["status"],
                "reason": result.get("reason"),
                "rate": row["rate"],
                "iv30": result.get("iv30"),
            }
        )
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    c = commands.add_parser("update")
    c.add_argument("base", type=Path)
    c.add_argument("dest", type=Path)
    c.add_argument(
        "--replace-manifest", type=Path, help="Checked replacement files alongside this manifest"
    )
    c.add_argument("--intraday", type=Path, action="append", default=[])
    c.add_argument("--daily", type=Path, action="append", default=[])
    c.add_argument(
        "--attempt",
        type=Path,
        action="append",
        default=[],
        help="Additional raw daily retry, for audit only",
    )
    batch = commands.add_parser(
        "prepare", help="Record checked, complete input tables for --replace-manifest"
    )
    batch.add_argument("folder", type=Path)
    batch.add_argument("names", nargs="+")
    batch.add_argument("--source", required=True)
    batch.add_argument("--collected-at", required=True)
    report = commands.add_parser("coverage")
    report.add_argument("snapshot", type=Path)
    report.add_argument("--start", type=date.fromisoformat)
    report.add_argument("--end", type=date.fromisoformat)
    audit = commands.add_parser("audit-options", help="Recompute each IV30 from its archived raw chain")
    audit.add_argument("snapshot", type=Path)
    a = p.parse_args()
    if a.command == "update":
        captures = [(f, kind) for kind in ("intraday", "daily", "attempt") for f in getattr(a, kind)]
        print(compose(a.base, a.dest, captures=captures, replacements=a.replace_manifest))
    elif a.command == "audit-options":
        with open_snapshot(a.snapshot) as (_, manifest, _digest):
            print(json.dumps(audit_options(a.snapshot, manifest), indent=2))
    elif a.command == "prepare":
        print(prepare(a.folder, a.names, a.source, a.collected_at))
    else:
        with open_snapshot(a.snapshot) as (db, version, digest):
            print(
                json.dumps(
                    {
                        "version": version["snapshot"],
                        "manifest_sha256": digest,
                        "common": preflight(db, a.start, a.end),
                        "tables": inventory(db),
                    },
                    indent=2,
                )
            )


if __name__ == "__main__":
    main()
