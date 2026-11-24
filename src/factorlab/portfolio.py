"""Convert one factor's scores into deterministic, unlevered target weights."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .evaluation.metrics import _table
from .factors.price_volume import _window


def target_weights(scores, *, top_n=10, cash_buffer=0.02):
    """Equal allocation to the top names; unavailable slots remain in cash.

    All rows are retained, with zero weights outside the selected set. A day with
    no valid score is marked separately so the simulator can apply its declared
    missing-signal policy rather than silently treating it as a sell signal.
    """
    top_n = _window(top_n, "top_n")
    cash_buffer = float(cash_buffer)
    if not np.isfinite(cash_buffer) or not 0 <= cash_buffer < 1:
        raise ValueError("cash_buffer must be in [0, 1)")
    frame = _table(scores, ("date", "symbol", "factor", "value"),
                   ["date", "symbol"], "portfolio scores")
    if frame.factor.nunique() != 1:
        raise ValueError("one portfolio must have exactly one factor or blend")
    frame["value"] = pd.to_numeric(frame.value, errors="raise").astype(float)
    if np.isinf(frame.value.to_numpy()).any():
        raise ValueError("infinite portfolio scores")
    parts = []
    for day, group in frame.groupby("date", sort=True):
        ranked = group.dropna(subset=["value"]).sort_values(
            ["value", "symbol"], ascending=[False, True], kind="stable"
        )
        selected = set(ranked.head(top_n).symbol)
        parts.append(pd.DataFrame({"date": day, "symbol": group.symbol,
            "factor": group.factor,
            "weight": group.symbol.map(lambda symbol: (1 - cash_buffer) / top_n if symbol in selected else 0.0),
            "signal_valid": not ranked.empty, "valid_names": len(ranked)}))
    return pd.concat(parts, ignore_index=True).sort_values(["date", "symbol"]).reset_index(drop=True)
