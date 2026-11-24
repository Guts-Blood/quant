"""Forward-return labels with explicit start and end sessions."""
from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from .data.panel import prepare_panel
from .factors.price_volume import _window


def forward_returns(panel: pd.DataFrame, *, calendar: Iterable, price_column: str,
                    horizon: int, entry_lag: int = 1) -> pd.DataFrame:
    """Calculate horizon-session returns starting entry_lag sessions after signal.

    entry_lag=0 is explicitly marked as a same-session diagnostic. These labels
    are statistical outcomes, not executable P&L; fees, halts, limits and fills
    are applied separately by the portfolio simulator. No price is forward-filled.
    """
    horizon = _window(horizon, "horizon")
    entry_lag = _window(entry_lag, "entry_lag", minimum=0)
    aligned = prepare_panel(panel, calendar=calendar, required=(price_column,))
    if (aligned[price_column].dropna() <= 0).any():
        raise ValueError("label prices must be positive")
    parts = []
    for symbol, group in aligned.groupby("symbol", sort=True):
        history = group.set_index("date")
        prices = history[price_column]
        dates = pd.Series(history.index, index=history.index)
        entry = prices.shift(-entry_lag)
        exit_price = prices.shift(-(entry_lag + horizon))
        result = exit_price / entry - 1.0
        parts.append(pd.DataFrame({
            "date": history.index, "symbol": symbol, "forward_return": result.to_numpy(),
            "label_start": dates.shift(-entry_lag).to_numpy(),
            "label_end": dates.shift(-(entry_lag + horizon)).to_numpy(),
            "horizon": horizon, "entry_lag": entry_lag, "price_column": price_column,
            "timing": "same_session_diagnostic" if entry_lag == 0 else "later_session",
        }))
    return pd.concat(parts, ignore_index=True).sort_values(
        ["date", "symbol"], kind="stable"
    ).reset_index(drop=True)


def within_split(labels: pd.DataFrame, start, end) -> pd.DataFrame:
    """Exclude outcomes extending beyond a split, including overlapping horizons."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if start.tzinfo is not None or end.tzinfo is not None or start > end:
        raise ValueError("split bounds must be ordered timezone-naive dates")
    if start != start.normalize() or end != end.normalize():
        raise ValueError("split bounds must be dates")
    required = {"date", "label_start", "label_end"}
    if not required.issubset(labels.columns):
        raise ValueError("labels are missing their timing columns")
    keep = (labels.date >= start) & (labels.date <= end)
    keep &= (labels.label_start >= start) & (labels.label_end <= end)
    return labels.loc[keep].copy().reset_index(drop=True)
