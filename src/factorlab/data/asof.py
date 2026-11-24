"""Point-in-time selection of versioned accounting observations.

Public-time reconstruction and actually observed history are separate modes.
The caller must choose; a download made today is never silently backdated.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

REQUIRED = {"symbol", "field", "period_end", "published_at", "available_at",
            "ingested_at", "revision", "value", "source_id"}


def _aware(value, name: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tzinfo is None:
        raise ValueError(f"{name} requires an explicit timezone")
    return timestamp.tz_convert("UTC")


def validate_fundamentals(records: pd.DataFrame) -> pd.DataFrame:
    """Validate long-form statement facts; retain source and revision metadata."""
    if not isinstance(records, pd.DataFrame) or records.empty:
        raise ValueError("fundamentals must be a nonempty DataFrame")
    if not records.columns.is_unique or not REQUIRED.issubset(records.columns):
        raise ValueError(f"fundamentals require {sorted(REQUIRED)}")
    result = records.copy(deep=True)
    for column in ("symbol", "field", "source_id"):
        if not result[column].map(
            lambda value: isinstance(value, str) and bool(value) and value == value.strip()
        ).all():
            raise ValueError(f"invalid {column}")
    period = pd.DatetimeIndex(pd.to_datetime(result["period_end"], errors="raise"))
    if period.hasnans or period.tz is not None or not period.equals(period.normalize()):
        raise ValueError("period_end must contain dates")
    result["period_end"] = period
    for column in ("published_at", "available_at", "ingested_at"):
        result[column] = pd.to_datetime(
            [_aware(value, column) for value in result[column]], utc=True
        )
    if (result.available_at < result.published_at).any():
        raise ValueError("available_at cannot precede public disclosure")
    if (result.ingested_at < result.published_at).any():
        raise ValueError("ingested_at cannot precede public disclosure")
    publication_date = result.published_at.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).dt.normalize()
    if (result.period_end > publication_date).any():
        raise ValueError("statement period cannot end after its publication")
    revision = pd.to_numeric(result.revision, errors="raise")
    if revision.isna().any() or not np.isfinite(revision).all() or (revision < 0).any() or (revision % 1 != 0).any():
        raise ValueError("revision must be a nonnegative integer")
    result["revision"] = revision.astype("int64")
    if result.duplicated(["symbol", "field", "period_end", "revision"]).any():
        raise ValueError("duplicate or conflicting fundamental revision")
    result["value"] = pd.to_numeric(result.value, errors="raise").astype(float)
    if np.isinf(result.value.to_numpy()).any():
        raise ValueError("fundamental values cannot be infinite")
    return result


def asof_records(records: pd.DataFrame, cutoff, *, mode: str) -> pd.DataFrame:
    """Select the last available revision of each symbol/field/reporting period.

    mode='public' reconstructs availability from disclosure plus processing delay.
    mode='observed' additionally requires the record to have been ingested by the
    cutoff. A later correction never overwrites an earlier historical snapshot.
    """
    if mode not in {"public", "observed"}:
        raise ValueError("mode must be explicitly 'public' or 'observed'")
    cutoff = _aware(cutoff, "cutoff")
    frame = validate_fundamentals(records)
    allowed = frame.available_at <= cutoff
    if mode == "observed":
        allowed &= frame.ingested_at <= cutoff
    selected = frame.loc[allowed].sort_values(
        ["symbol", "field", "period_end", "available_at", "revision"], kind="stable"
    ).groupby(["symbol", "field", "period_end"], sort=False).tail(1).copy()
    selected["availability_mode"] = mode
    selected["historical_reconstruction"] = selected.ingested_at > cutoff
    return selected.reset_index(drop=True)


def latest_fundamentals(records: pd.DataFrame, cutoff, *, mode: str) -> pd.DataFrame:
    """Latest disclosed period for each symbol/field, without filling missing facts."""
    selected = asof_records(records, cutoff, mode=mode)
    return selected.sort_values(["symbol", "field", "period_end"], kind="stable").groupby(
        ["symbol", "field"], sort=False
    ).tail(1).reset_index(drop=True)
