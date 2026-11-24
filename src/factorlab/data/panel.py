"""Validation and calendar alignment for daily research panels."""
from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd


def sessions(values: Iterable) -> pd.DatetimeIndex:
    """Validate an explicit exchange calendar; never infer missing market days."""
    index = pd.DatetimeIndex(pd.to_datetime(list(values), errors="raise"), name="date")
    if index.empty or index.hasnans or index.tz is not None:
        raise ValueError("calendar must contain timezone-naive session dates")
    if not index.is_unique or not index.is_monotonic_increasing:
        raise ValueError("calendar must be unique and sorted")
    if not index.equals(index.normalize()):
        raise ValueError("calendar entries must be dates, not intraday timestamps")
    return index


def prepare_panel(frame: pd.DataFrame, *, calendar: Iterable,
                  required: Iterable[str] = ()) -> pd.DataFrame:
    """Return a dense date/symbol panel without filling any input values.

    Missing observations are explicit rows with observed=False. The supplied
    exchange calendar controls every rolling window and forward-return horizon.
    Extra columns, such as industry or tradability flags, are preserved.
    """
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError("panel must be a nonempty DataFrame")
    required = tuple(required)
    needed = {"date", "symbol", *required}
    if not frame.columns.is_unique or not needed.issubset(frame.columns):
        raise ValueError(f"panel requires unique columns including {sorted(needed)}")
    if "observed" in frame.columns:
        raise ValueError("observed is reserved for calendar alignment")
    result = frame.copy(deep=True)
    result["date"] = pd.to_datetime(result["date"], errors="raise")
    dates = pd.DatetimeIndex(result["date"])
    if dates.hasnans or dates.tz is not None or not dates.equals(dates.normalize()):
        raise ValueError("panel dates must be timezone-naive session dates")
    valid_symbols = result["symbol"].map(
        lambda value: isinstance(value, str) and bool(value) and value == value.strip()
    )
    if not valid_symbols.all():
        raise ValueError("symbols must be nonempty strings without surrounding whitespace")
    if result.duplicated(["date", "symbol"]).any():
        raise ValueError("duplicate date/symbol observations")
    calendar_index = sessions(calendar)
    if not dates.isin(calendar_index).all():
        raise ValueError("panel contains a date outside the exchange calendar")
    for column in required:
        result[column] = pd.to_numeric(result[column], errors="raise").astype(float)
        if np.isinf(result[column].to_numpy()).any():
            raise ValueError(f"infinite values in {column}")
    indexed = result.set_index(["date", "symbol"])
    grid = pd.MultiIndex.from_product(
        [calendar_index, sorted(result["symbol"].unique())], names=["date", "symbol"]
    )
    aligned = indexed.reindex(grid)
    aligned["observed"] = grid.isin(indexed.index)
    return aligned.reset_index()
