"""Restore the versioned A-share market snapshot and optionally build DuckDB views."""
import argparse
from contextlib import closing
import hashlib
import json
import re
from pathlib import Path
import shutil
import tempfile
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "configs/market-data/2026-11-19.json"
PUBLIC_REPO = "CryUshio/ashare-market-base"
BASE_TABLES = ("daily", "valuation", "fundamentals", "dividends")


def current_public_manifest():
    with urlopen(f"https://huggingface.co/api/datasets/{PUBLIC_REPO}", timeout=30) as source:
        revision = json.load(source)["sha"]
    if not re.fullmatch("[0-9a-f]{40}", revision):
        raise ValueError("Invalid upstream revision")
    base = f"https://huggingface.co/datasets/{PUBLIC_REPO}/resolve/{revision}/"
    with urlopen(base + "manifest.json", timeout=30) as source:
        index = {entry["path"]: entry for entry in json.load(source)["files"]}
    files = []
    for table in BASE_TABLES:
        name = table + ".parquet"
        entry = index[name]
        if not re.fullmatch("[0-9a-f]{64}", entry["sha256"]) or entry["bytes"] <= 0:
            raise ValueError(f"Invalid upstream checksum or size for {name}")
        files.append({"name": name, "bytes": entry["bytes"], "sha256": entry["sha256"],
                      "source": base + "base/" + name, "drive_parts": [name]})
    return {"snapshot": revision, "provider": PUBLIC_REPO, "files": files}


def read_manifest(path):
    manifest = json.loads(path.read_bytes())

    def safe_name(value):
        return isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", value)

    if not safe_name(manifest.get("snapshot")) or not manifest.get("files"):
        raise ValueError("Expected a named snapshot with files")
    names, parts = set(), set()
    for spec in manifest["files"]:
        name = spec["name"]
        chunks = spec["drive_parts"]
        if (not safe_name(name) or name in {"manifest.json", "market.duckdb"} or name in names
                or not isinstance(chunks, list) or not chunks
                or not all(safe_name(part) and part != "manifest.json" for part in chunks)
                or len(set(chunks)) != len(chunks) or parts.intersection(chunks)):
            raise ValueError("Invalid or duplicate snapshot filename")
        if (type(spec["bytes"]) is not int or spec["bytes"] < 0
                or not re.fullmatch("[0-9a-f]{64}", spec["sha256"])):
            raise ValueError(f"Invalid file size or checksum for {name}")
        names.add(name)
        parts.update(chunks)
    return manifest


def verified(path, spec):
    if not path.is_file() or path.stat().st_size != spec["bytes"]:
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == spec["sha256"]


def restore_file(spec, target, archive):
    destination = target / spec["name"]
    if verified(destination, spec):
        return
    with tempfile.NamedTemporaryFile(dir=target, delete=False) as output:
        temp = Path(output.name)
    try:
        with temp.open("wb") as output:
            if archive:
                for part in spec["drive_parts"]:
                    with (archive / part).open("rb") as stream:
                        shutil.copyfileobj(stream, output, 1024 * 1024)
            elif spec["source"]:
                with urlopen(spec["source"], timeout=120) as stream:
                    shutil.copyfileobj(stream, output, 1024 * 1024)
            else:
                raise ValueError("Supply the downloaded Drive files with --archive for " + spec["name"])
        if not verified(temp, spec):
            raise ValueError(f"Size or SHA-256 mismatch for {destination.name}")
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)


def build_catalog(target):
    import duckdb

    with closing(duckdb.connect(str(target / "market.duckdb"))) as db:
        with db:
            for table in BASE_TABLES:
                # Store local absolute paths so callers can query from any directory.
                file = str((target / (table + ".parquet")).resolve()).replace("'", "''")
                db.execute(f"CREATE OR REPLACE VIEW {table} AS SELECT * FROM read_parquet('{file}')")
            db.execute("""CREATE OR REPLACE VIEW research_daily AS
                          SELECT d.*, v.turn, v.pe_ttm, v.pb_mrq, v.ps_ttm, v.is_st
                          FROM daily d LEFT JOIN valuation v USING (stock_code, date)""")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--out", type=Path, help="Local snapshot directory")
    parser.add_argument("--archive", type=Path, help="Directory containing the downloaded Drive files")
    parser.add_argument("--public-only", action="store_true", help="Restore the pinned public sources only")
    parser.add_argument("--refresh-public", action="store_true",
                        help="Discover and cache the latest four public tables in a revision-specific directory")
    parser.add_argument("--catalog", action="store_true", help="Build market.duckdb (requires duckdb)")
    args = parser.parse_args()
    if args.refresh_public and (args.archive or args.out or args.manifest != DEFAULT_MANIFEST):
        parser.error("--refresh-public writes a separate snapshot directory for the resolved upstream revision")
    manifest = current_public_manifest() if args.refresh_public else read_manifest(args.manifest)
    target = args.out or ROOT / "datasets/market" / manifest["snapshot"]
    target.mkdir(parents=True, exist_ok=True)
    files = [f for f in manifest["files"] if f["source"] or not args.public_only]
    for spec in files:
        restore_file(spec, target, args.archive)
    if args.catalog:
        build_catalog(target)
    if args.refresh_public:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target, delete=False) as stream:
            json.dump(manifest, stream, indent=2)
            stream.write("\n")
        Path(stream.name).replace(target / "manifest.json")
    print(f"Verified {len(files)} files in {target.resolve()}")


if __name__ == "__main__":
    main()
