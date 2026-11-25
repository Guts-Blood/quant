"""Archive a 510500 close observation, or import an archived probe without re-dating it."""

import argparse
from collections import Counter
from datetime import datetime, time, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from .legacy_pilot.risk_expectations import local_iv30
from .legacy_pilot.risk_source_probe import CST, SnapshotClient, fetch_options

OBSERVATION = time(15, 1)
AGE_SECONDS = 300
UNDERLYING = "510500"


def timestamp(value):
    t = datetime.fromisoformat(value)
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError("Option timestamps need a timezone")
    return t


def encode(obj):
    return (
        json.dumps(obj, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    ).encode()


def write_archive(folder, path):
    with ZipFile(path, "w") as z:
        for f in sorted(folder.rglob("*")):
            if f.is_file():
                info = ZipInfo(f.relative_to(folder).as_posix())
                payload = f.read_bytes()
                if f.parent.name == "raw" and f.suffix == ".json":
                    meta = json.loads(payload)
                    if "raw_path" in meta:
                        meta["raw_path"] = "raw/" + Path(meta["raw_path"]).name
                        payload = encode(meta)
                z.writestr(info, payload, compress_type=ZIP_DEFLATED)


def observation(path, kind):
    """Read ONE acquisition batch. Its wire archive remains an independent snapshot input."""
    with ZipFile(path) as z:
        for name in z.namelist():
            if name.startswith("raw/") and name.endswith(".json"):
                meta = json.loads(z.read(name))
                body = z.read(name[:-5] + ".bin")
                if len(body) != meta["bytes"] or hashlib.sha256(body).hexdigest() != meta["sha256"]:
                    raise ValueError(f"Changed vendor response: {name}")
        if "observation.json" in z.namelist():
            row = json.loads(z.read("observation.json"))
        else:
            lines = z.read("iv-quotes.jsonl").splitlines()
            if len(lines) != 1:
                raise ValueError("Expected one historical observation per input partition")
            row = json.loads(lines[0])
            row.setdefault("underlying", UNDERLYING)
            if "round-1-options.json" in z.namelist():
                parsed = json.loads(z.read("round-1-options.json"))
                if parsed["quotes"] != row["quotes"]:
                    raise ValueError("The archived probe and quote export disagree")
                for key in ("source_quality_flags", "expected_contracts", "parse_errors"):
                    row.setdefault(key, parsed.get(key))
    if kind not in ("intraday", "daily") or row["underlying"] != UNDERLYING or not row["source_id"]:
        raise ValueError("Unexpected observation kind, underlying or source")
    asof, available, ingested = (timestamp(row[k]) for k in ("asof", "available_at", "ingested_at"))
    local = asof.astimezone(CST)
    if row["date"] != local.date().isoformat() or not asof <= available <= ingested:
        raise ValueError("Invalid option observation date or availability")
    if kind == "daily" and local.time() != OBSERVATION:
        raise ValueError("Daily observations require the fixed 15:01:00 Asia/Shanghai cutoff")
    quotes = row["quotes"]
    ids = [q["contract_id"] for q in quotes]
    if any(q.get("underlying") != UNDERLYING for q in quotes) or len(set(ids)) != len(ids):
        raise ValueError("Mixed underlyings or duplicate contract in one observation")
    calc = local_iv30(quotes, asof=asof, rate=row["rate"], max_quote_age_seconds=AGE_SECONDS)
    reasons = Counter(q["reason"] for q in calc["excluded_quotes"])
    quality = {
        "received": len(quotes),
        "expected": row.get("expected_contracts"),
        "with_bid_and_ask": sum(q.get("bid") is not None and q.get("ask") is not None for q in quotes),
        "quoted_times": sorted({str(q["quoted_at"]) for q in quotes if q.get("quoted_at")}),
        "excluded_by_reason": dict(reasons),
    }
    return {
        **row,
        "kind": kind,
        "max_age_seconds": AGE_SECONDS,
        "calculation": calc,
        "rate_basis": row.get("rate_basis", "flat annual continuously compounded research assumption"),
        "quality": quality,
    }


def collect(folder, *, rate, now=None, fetch=fetch_options, retry_unavailable=False):
    """Cache one usable daily cut. Failed attempts keep raw responses and can be retried."""
    now = now or (lambda: datetime.now(timezone.utc))
    today = now().astimezone(CST)
    day = today.date()
    asof = datetime.combine(day, OBSERVATION, CST)
    if today < asof or day.weekday() > 4:
        raise ValueError("Collect after 15:01 Shanghai on the trading day")
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"510500-{day}-daily.zip"
    if dest.exists():
        cached = observation(dest, "daily")
        if cached["rate"] != rate:
            raise ValueError("The existing observation used a different rate; keep a separate version")
        return dest
    failed = list(folder.glob(f"510500-{day}-unavailable-*.zip"))
    if failed and not retry_unavailable:
        cached = []
        for path in failed:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if path.stem.rsplit("-", 1)[-1] != digest:
                raise ValueError(f"Changed failed capture: {path.name}")
            row = observation(path, "daily")
            if row["rate"] != rate:
                raise ValueError("The existing observation used a different rate; keep a separate version")
            cached.append((timestamp(row["available_at"]), path))
        return min(cached)[1]
    with tempfile.TemporaryDirectory(dir=folder) as stage:
        wire = Path(stage) / "wire"
        wire.mkdir()
        client = SnapshotClient(wire / "raw")
        try:
            result = fetch(client, underlying=UNDERLYING, rate=rate)
        except (ValueError, OSError, RuntimeError) as exc:
            result = {"quotes": [], "error": str(exc)}
        available = now()
        if available.astimezone(CST).date() != day:
            raise ValueError("Collection crossed a date boundary")
        row = {
            "source_id": "sina_510500_options",
            "underlying": UNDERLYING,
            "date": str(day),
            "asof": asof.isoformat(),
            "available_at": available.isoformat(),
            "ingested_at": available.isoformat(),
            "rate": rate,
            "quotes": result["quotes"],
            "source_quality_flags": result.get("source_quality_flags", []),
            "acquisition_error": result.get("error"),
            "parse_errors": result.get("parse_errors", []),
            "expected_contracts": result.get("expected_contracts"),
        }
        (wire / "observation.json").write_bytes(encode(row))
        tmp = Path(stage) / "capture.zip"
        write_archive(wire, tmp)
        checked = observation(tmp, "daily")
        if checked["calculation"]["status"] != "ok":
            digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
            dest = folder / f"510500-{day}-unavailable-{digest}.zip"
        try:
            os.link(tmp, dest)
        except FileExistsError:
            if observation(dest, "daily")["rate"] != rate:
                raise ValueError("Conflicting concurrent capture")
        return dest


def quote_audit(row):
    """Diagnose each expiry, including the first insufficient wing of an unusable chain."""
    expiries = []
    result = local_iv30(row["quotes"], asof=timestamp(row["asof"]), rate=row["rate"],
                        max_quote_age_seconds=AGE_SECONDS, expiry_audit=expiries)
    excluded = {q["contract_id"] for q in result["excluded_quotes"]}
    expected = row.get("expected_contracts")
    audited = {entry["expiry"] for entry in expiries}
    for label in sorted({q["expiry"] for q in row["quotes"] if isinstance(q.get("expiry"), str)}):
        try:
            expiry = timestamp(label)
        except (ValueError, TypeError):
            continue
        if not any(expiry == timestamp(t) for t in audited):
            expiries.append({"expiry": label, "accepted_quotes": 0, "positive_bids": 0,
                             "wing_counts": None, "reason": "all quotes excluded"})
    return {
        "expected": expected, "received": len(row["quotes"]),
        "missing_contracts": expected - len(row["quotes"]) if expected is not None else None,
        "fresh_positive_two_sided": sum(q["contract_id"] not in excluded and float(q["bid"]) > 0 for q in row["quotes"]),
        "excluded_by_reason": dict(Counter(q["reason"] for q in result["excluded_quotes"])),
        "max_accepted_age_seconds": result["max_quote_age_seconds"], "expiries": expiries,
        "acquisition_error": row.get("acquisition_error"), "status": result["status"], "reason": result.get("reason"),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("folder", type=Path)
    p.add_argument("--rate", type=float, required=True, help="Annual continuously compounded decimal rate")
    p.add_argument(
        "--retry-unavailable", action="store_true", help="Requery an unavailable day; retain every attempt"
    )
    args = p.parse_args()
    path = collect(args.folder, rate=args.rate, retry_unavailable=args.retry_unavailable)
    iv = observation(path, "daily")["calculation"]
    print(
        json.dumps(
            {"path": str(path), "status": iv["status"], "iv30": iv.get("iv30"), "reason": iv.get("reason")}
        )
    )


if __name__ == "__main__":
    main()
