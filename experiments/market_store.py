"""Publish and restore versioned market data through an authenticated rclone remote."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path
import subprocess
import tempfile

from .market_snapshot import DEFAULT_MANIFEST, build_catalog, read_manifest, restore_file, verified


def transfer(*args):
    subprocess.run(["rclone", *map(str, args)], check=True)


def publish(source, manifest_path, store):
    manifest = read_manifest(manifest_path)
    with tempfile.TemporaryDirectory() as temp:
        staging = Path(temp) / "data"
        staging.mkdir()
        files = []
        for spec in manifest["files"]:
            name = spec["name"]
            shutil.copyfile(source / name, staging / name)
            if not verified(staging / name, spec):
                raise ValueError(f"Invalid local snapshot file: {name}")
            files.append(spec | {"drive_parts": [name]})
        payload = (json.dumps(manifest | {"files": files}, sort_keys=True, indent=2) + "\n").encode()
        digest = hashlib.sha256(payload).hexdigest()
        remote = f"{store.rstrip('/')}/snapshots/{manifest['snapshot']}/{digest}"
        transfer("copy", staging, remote, "--immutable", "--checksum")
        transfer("check", staging, remote, "--download", "--one-way")
        marker = Path(temp) / "manifest.json"
        marker.write_bytes(payload)
        transfer("copyto", marker, remote + "/manifest.json", "--immutable", "--checksum")
    return remote


def restore(remote, target, pinned, catalog):
    remote = remote.rstrip("/")
    with tempfile.TemporaryDirectory() as temp:
        archive = Path(temp)
        if pinned is None:
            pinned = archive / "manifest.json"
            transfer("copyto", remote + "/manifest.json", pinned)
            digest = hashlib.sha256(pinned.read_bytes()).hexdigest()
            if remote.rsplit("/", 1)[-1] != digest:
                raise ValueError("Remote path must end in the manifest SHA-256 or use --manifest for a pinned archive")
        manifest = read_manifest(pinned)
        target.mkdir(parents=True, exist_ok=True)
        for spec in manifest["files"]:
            if verified(target / spec["name"], spec):
                continue
            for part in spec["drive_parts"]:
                transfer("copyto", remote + "/" + part, archive / part)
            restore_file(spec, target, archive)
            for part in spec["drive_parts"]:
                (archive / part).unlink()
        if catalog:
            build_catalog(target)
    print(f"Verified {len(manifest['files'])} files in {target.resolve()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    put = commands.add_parser("publish", help="Publish checked files and write the release manifest last")
    put.add_argument("source", type=Path)
    put.add_argument("store", help="rclone directory, e.g. research:quant")
    put.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    get = commands.add_parser("restore", help="Restore a release or a pinned, split archive")
    get.add_argument("remote", help="Release directory printed by publish, or existing archive folder")
    get.add_argument("target", type=Path)
    get.add_argument("--manifest", type=Path, help="Pinned local manifest for an existing archive")
    get.add_argument("--catalog", action="store_true")
    args = parser.parse_args()
    if args.command == "publish":
        print(publish(args.source, args.manifest, args.store))
    else:
        restore(args.remote, args.target, args.manifest, args.catalog)


if __name__ == "__main__":
    main()
