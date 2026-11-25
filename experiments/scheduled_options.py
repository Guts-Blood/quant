"""Acquire and audit an independent 510500 batch at the Shanghai close on exchange sessions."""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time

from .market_snapshot import write_once
from .market_version import checksum
from .option_capture import CST, OBSERVATION, collect, observation, quote_audit


def capture_session(folder, *, now=lambda: datetime.now(CST), sleep=time.sleep, capture=collect):
    import exchange_calendars as xc

    folder.mkdir(parents=True, exist_ok=True)
    start = now().astimezone(CST)
    day = start.date()
    calendar = xc.get_calendar("XSHG")
    # An out-of-range calendar raises. A weekday alone does not establish an exchange session.
    if not calendar.is_session(day.isoformat()):
        return {"date": str(day), "status": "closed"}
    cutoff = datetime.combine(day, OBSERVATION, CST)
    delay = (cutoff - start).total_seconds()
    if delay > 0:
        sleep(delay)
    started = now().astimezone(CST)
    if started.date() != day:
        raise ValueError("Missed scheduled session")
    if (started - cutoff).total_seconds() > 60:
        return {
            "date": str(day),
            "asof": cutoff.isoformat(),
            "started_at": started.isoformat(),
            "status": "missed_capture_window",
            "attempts": [],
        }
    attempts = []
    for attempt in range(2):
        path = capture(folder, rate=0.02, now=now, retry_unavailable=attempt > 0)
        row = observation(path, "daily")
        if row["date"] != str(day):
            raise ValueError("Capture has the wrong session date")
        attempts.append(
            {
                "file": path.name,
                "sha256": checksum(path),
                "available_at": row["available_at"],
                "audit": quote_audit(row),
            }
        )
        if row["calculation"]["status"] == "ok":
            break
        if attempt == 0:
            sleep(10)
    return {
        "date": str(day),
        "asof": cutoff.isoformat(),
        "status": attempts[-1]["audit"]["status"],
        "selected": attempts[-1]["file"],
        "attempts": attempts,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    receipt = capture_session(args.folder)
    payload = json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    # Each runner has its own directory. Keep separate retry receipts on operator reruns.
    digest = hashlib.sha256(payload.encode()).hexdigest()
    write_once(args.folder / f"{receipt['date']}-{digest}.json", payload.encode())
    print(payload)
