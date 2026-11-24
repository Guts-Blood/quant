"""Uniform date/symbol factor API backed by small, pure formulas."""
from __future__ import annotations

from collections.abc import Iterable, Mapping

import numpy as np
import pandas as pd

from ..data.panel import prepare_panel
from .price_volume import low_volatility, momentum, reversal
from .value import earnings_yield

# New factors add a formula and metadata; execution and account state stay outside.
CATALOG = {
    "reversal": {"column": "adj_close", "function": reversal,
                 "hypothesis": "Recent price changes partially reverse."},
    "momentum": {"column": "adj_close", "function": momentum,
                 "hypothesis": "Intermediate trends persist beyond recent noise."},
    "lowvol": {"column": "return", "function": low_volatility,
               "hypothesis": "Lower realized volatility earns a relative premium."},
    "value": {"column": "pe_ttm", "function": earnings_yield,
              "hypothesis": "Higher positive trailing earnings yield predicts returns."},
}


def compute(panel: pd.DataFrame, name: str, *, calendar: Iterable,
            parameters: Mapping | None = None) -> pd.DataFrame:
    """Return date, symbol, factor, value and missing_reason on the given calendar.

    Scores use information at each session's close; execution must occur later.
    Numeric inputs must already reflect the appropriate historical data vintage.
    No future-return columns are passed to a factor formula.
    """
    if name not in CATALOG:
        raise ValueError(f"unknown factor {name!r}; choose from {sorted(CATALOG)}")
    spec = CATALOG[name]
    column = spec["column"]
    aligned = prepare_panel(panel, calendar=calendar, required=(column,))
    outputs = []
    for symbol, group in aligned.groupby("symbol", sort=True):
        history = group.set_index("date")
        values = spec["function"](history[column], **dict(parameters or {}))
        values = values.where(history["observed"] & history[column].notna())
        if np.isinf(values.to_numpy()).any():
            raise ValueError(f"nonfinite output from {name}")
        reason = pd.Series("", index=history.index, dtype="object")
        reason.loc[values.isna()] = "insufficient_history_or_missing_window"
        reason.loc[history[column].isna()] = "missing_input"
        if name == "value":
            reason.loc[history[column].notna() & (history[column] <= 0)] = "nonpositive_pe"
        reason.loc[~history["observed"]] = "missing_observation"
        outputs.append(pd.DataFrame({
            "date": history.index, "symbol": symbol, "factor": name,
            "value": values.to_numpy(), "missing_reason": reason.to_numpy(),
        }))
    return pd.concat(outputs, ignore_index=True).sort_values(
        ["date", "symbol"], kind="stable"
    ).reset_index(drop=True)


__all__ = ["CATALOG", "compute", "reversal", "momentum", "low_volatility", "earnings_yield"]
