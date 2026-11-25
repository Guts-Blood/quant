"""Publish and restore versioned market data through an authenticated rclone remote."""
import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
import subprocess
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from .market_snapshot import DEFAULT_MANIFEST, finish_restore, pin_target, read_manifest, restore_file, verified


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
        payload = pinned.read_bytes()
        pin_target(target, hashlib.sha256(payload).hexdigest())
        for spec in manifest["files"]:
            if verified(target / spec["name"], spec):
                continue
            for part in spec["drive_parts"]:
                transfer("copyto", remote + "/" + part, archive / part)
            restore_file(spec, target, archive)
            for part in spec["drive_parts"]:
                (archive / part).unlink()
        finish_restore(target, payload, catalog)
    print(f"Verified {len(manifest['files'])} files in {target.resolve()}")


def publish_increment(source, parent_path, store):
    """Publish immutable additions into an existing split archive, then its exact Git-pinnable manifest."""
    manifest_path = source / "manifest.json"
    release, parent = read_manifest(manifest_path), read_manifest(parent_path)
    if release.get("parent_manifest_sha256") != hashlib.sha256(parent_path.read_bytes()).hexdigest():
        raise ValueError("Incorrect parent manifest")
    parent_files = {s["name"]:s for s in parent["files"]}
    remote = store.rstrip("/")
    present = set(subprocess.check_output(["rclone","lsf",remote,"--files-only","--max-depth","1"],
                                          text=True).splitlines())
    with tempfile.TemporaryDirectory() as tmp:
        checked = Path(tmp)
        for s in release["files"]:
            old = parent_files.get(s["name"])
            if old and (s["sha256"],s["drive_parts"]) == (old["sha256"],old["drive_parts"]):
                if not set(s["drive_parts"]) <= present:
                    raise ValueError(f"Parent data absent on remote: {s['name']}")
                continue
            part = s["sha256"] + "-" + s["name"]
            if s["drive_parts"] != [part] or not verified(source/s["name"],s):
                raise ValueError(f"Invalid increment: {s['name']}")
            transfer("copyto",source/s["name"],remote+"/"+part,"--immutable","--checksum")
            transfer("copyto",remote+"/"+part,checked/part)
            if not verified(checked/part,s):
                raise ValueError(f"Remote verification failed: {part}")
        marker = remote + "/" + release["snapshot"] + "-manifest.json"
        transfer("copyto",manifest_path,marker,"--immutable","--checksum")
    return marker


def pack_run(run_dir, folder):
    """Archive the run receipt and exactly the output bytes to which it commits."""
    payload = (run_dir / "run.json").read_bytes()
    receipt = json.loads(payload)
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", receipt["snapshot"])
            or not re.fullmatch(r"[a-f0-9]{64}", receipt["manifest_sha256"])):
        raise ValueError("Run is missing its snapshot pin")
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder) as tmp:
        path = Path(tmp) / "research.zip"
        with ZipFile(path, "w") as archive:
            archive.writestr(ZipInfo("run.json"), payload, compress_type=ZIP_DEFLATED)
            for name, expected in sorted(receipt["outputs"].items()):
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.csv", name):
                    raise ValueError("Unexpected run filename")
                info = ZipInfo(name)
                info.compress_type = ZIP_DEFLATED
                digest = hashlib.sha256()
                with (run_dir/name).open("rb") as src, archive.open(info,"w",force_zip64=True) as dest:
                    while chunk := src.read(1024*1024):
                        dest.write(chunk)
                        digest.update(chunk)
                if digest.hexdigest() != expected:
                    raise ValueError(f"Changed research output: {name}")
        with path.open("rb") as src:
            sha = hashlib.file_digest(src, "sha256").hexdigest()
        final = folder / f"research-{receipt['snapshot']}-{sha}.zip"
        try:
            os.link(path,final)
        except FileExistsError:
            if not verified(final, {"bytes":path.stat().st_size,"sha256":sha}):
                raise ValueError(f"Changed existing research archive: {final}")
        return final


def publish_run(run_dir, store):
    with tempfile.TemporaryDirectory() as tmp:
        local = pack_run(run_dir,Path(tmp))
        remote = store.rstrip("/") + "/" + local.name
        transfer("copyto",local,remote,"--immutable","--checksum")
        copy = Path(tmp) / "readback.zip"
        transfer("copyto",remote,copy)
        if not verified(copy, {"bytes":local.stat().st_size,"sha256":local.stem.rsplit("-",1)[1]}):
            raise ValueError("Research archive failed remote read-back")
    return remote


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    put = commands.add_parser("publish", help="Publish checked files and write the release manifest last")
    put.add_argument("source", type=Path)
    put.add_argument("store", help="rclone directory, e.g. research:quant")
    put.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    delta = commands.add_parser("publish-increment", help="Add checked partitions to the existing Drive archive")
    delta.add_argument("source", type=Path)
    delta.add_argument("store")
    delta.add_argument("--parent-manifest", type=Path, required=True)
    get = commands.add_parser("restore", help="Restore a release or a pinned, split archive")
    get.add_argument("remote", help="Release directory printed by publish, or existing archive folder")
    get.add_argument("target", type=Path)
    get.add_argument("--manifest", type=Path, help="Pinned local manifest for an existing archive")
    get.add_argument("--catalog", action="store_true")
    pack = commands.add_parser("pack-run", help="Pack a verified research run for file or Drive upload")
    pack.add_argument("run_dir", type=Path)
    pack.add_argument("folder", type=Path)
    send = commands.add_parser("publish-run", help="Archive a run receipt and its checked outputs on the remote")
    send.add_argument("run_dir", type=Path)
    send.add_argument("store")
    args = parser.parse_args()
    if args.command == "publish":
        print(publish(args.source, args.manifest, args.store))
    elif args.command == "pack-run":
        print(pack_run(args.run_dir,args.folder))
    elif args.command == "publish-run":
        print(publish_run(args.run_dir,args.store))
    elif args.command == "publish-increment":
        print(publish_increment(args.source,args.parent_manifest,args.store))
    else:
        restore(args.remote, args.target, args.manifest, args.catalog)


if __name__ == "__main__":
    main()
