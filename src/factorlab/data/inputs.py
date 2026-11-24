"""CSV input loading with immutable file fingerprints and explicit availability."""
from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .asof import _aware
from .panel import prepare_panel, sessions


@dataclass
class ResearchData:
    panel: pd.DataFrame
    calendar: pd.DatetimeIndex
    membership: pd.DataFrame | None
    provenance: dict


def _read_csv(path: Path):
    path = path.resolve()
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    header = next(csv.reader(io.StringIO(text)), [])
    if not header or len(header) != len(set(header)) or any(not name for name in header):
        raise ValueError(f"invalid or duplicate CSV columns: {path}")
    frame = pd.read_csv(io.StringIO(text), dtype={"symbol": "str"})
    return frame, {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                   "bytes": len(raw), "rows": len(frame)}


def _booleans(values, name):
    mapping = {"true": True, "false": False, "1": True, "0": False}
    def parse(value):
        if isinstance(value, (bool, np.bool_)):
            return bool(value)
        token = str(value).strip().lower()
        if token not in mapping:
            raise ValueError(f"{name} requires explicit true/false values")
        return mapping[token]
    return values.map(parse)


def signal_cutoffs(dates, clock="16:00"):
    parsed = time.fromisoformat(clock)
    if parsed.tzinfo is not None or parsed < time(15, 0):
        raise ValueError("daily-close signal_time must be a local time at or after 15:00")
    values = pd.Series(pd.to_datetime(dates, errors="raise"))
    if values.dt.tz is not None or not values.equals(values.dt.normalize()):
        raise ValueError("signal dates must be timezone-naive session dates")
    offset = pd.Timedelta(hours=parsed.hour, minutes=parsed.minute, seconds=parsed.second)
    return (values.dt.tz_localize("Asia/Shanghai") + offset).dt.tz_convert("UTC")


def load_inputs(spec: dict, *, base_dir: str | Path) -> ResearchData:
    """Load declared historical inputs, never infer an earlier ingestion time.

    Paths are relative to the experiment configuration. Public mode uses declared
    source availability; observed mode also requires historical ingestion records.
    Source timestamps are validated for consistency, not independently attested.
    """
    required = {"panel", "calendar", "source_id", "availability_mode"}
    if not isinstance(spec, dict) or not required.issubset(spec):
        raise ValueError(f"data configuration requires {sorted(required)}")
    mode = spec["availability_mode"]
    if mode not in {"public", "observed"}:
        raise ValueError("availability_mode must be public or observed")
    source_id = spec["source_id"]
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("source_id must describe the input source")
    base = Path(base_dir).resolve()
    panel, panel_meta = _read_csv(base / spec["panel"])
    calendar_frame, calendar_meta = _read_csv(base / spec["calendar"])
    if "date" not in calendar_frame:
        raise ValueError("calendar CSV requires a date column")
    calendar = sessions(calendar_frame.date)
    needed = {"date", "symbol", "adj_close", "amount", "is_st", "tradable", "available_at"}
    if not needed.issubset(panel.columns):
        raise ValueError(f"daily panel requires {sorted(needed)}")
    panel["date"] = pd.to_datetime(panel.date, errors="raise")
    for flag in ("is_st", "tradable"):
        panel[flag] = _booleans(panel[flag], flag)
    numeric = [column for column in ("adj_close", "adj_open", "close", "open", "return",
               "pe_ttm", "amount", "volume", "market_cap", "limit_down", "limit_up",
               "cash_per_share") if column in panel]
    for column in numeric:
        panel[column] = pd.to_numeric(panel[column], errors="raise").astype(float)
    aligned = prepare_panel(panel, calendar=calendar, required=numeric)
    if (panel.adj_close.dropna() <= 0).any() or (panel.amount.dropna() < 0).any():
        raise ValueError("prices must be positive and amount nonnegative")
    if "return" not in panel:
        aligned["return"] = aligned.groupby("symbol", sort=False).adj_close.transform(
            lambda prices: prices / prices.shift(1) - 1.0
        )
        panel = panel.merge(aligned[["date", "symbol", "return"]], on=["date", "symbol"],
                            how="left", validate="one_to_one")
    clock = spec.get("signal_time", "16:00")
    cutoffs = signal_cutoffs(panel.date, clock)
    available = pd.to_datetime([_aware(value, "available_at") for value in panel.available_at], utc=True)
    if (available < signal_cutoffs(panel.date, "15:00")).any():
        raise ValueError("daily closing observations cannot be available before the close")
    if (available > cutoffs).any():
        raise ValueError("daily observations arrive after the configured signal cutoff")
    panel["available_at"] = available
    reconstruction = mode == "public"
    if "ingested_at" in panel:
        ingested = pd.to_datetime([_aware(value, "ingested_at") for value in panel.ingested_at], utc=True)
        panel["ingested_at"] = ingested
        reconstruction = bool((ingested > cutoffs).any())
        if mode == "observed" and reconstruction:
            raise ValueError("observed mode cannot use data ingested after a historical signal")
    elif mode == "observed":
        raise ValueError("observed mode requires ingested_at on every row")
    membership, membership_meta = None, None
    if spec.get("membership"):
        membership, membership_meta = _read_csv(base / spec["membership"])
        if mode == "public":
            # Membership is reconstructed from public event times even if daily bars were captured live.
            reconstruction = True
    provenance = {"source_id": source_id, "availability_mode": mode,
                  "historical_reconstruction": reconstruction, "signal_time": clock,
                  "loaded_at": datetime.now(timezone.utc).isoformat(),
                  "availability_evidence": "declared_source_timestamps",
                  "files": [panel_meta, calendar_meta] + ([membership_meta] if membership_meta else [])}
    return ResearchData(panel.sort_values(["date", "symbol"]).reset_index(drop=True),
                        calendar, membership, provenance)
