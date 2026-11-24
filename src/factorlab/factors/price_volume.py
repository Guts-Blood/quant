"""Pure daily price/volume factors; positive scores always rank first.

Inputs are one symbol's Series on an explicit trading-session calendar, in
ascending order. Missing sessions must be present as NaN. Prices are adjusted
consistently by the data layer; return inputs are decimal simple returns.
These functions perform no I/O and never fill missing observations.
"""
from __future__ import annotations

from numbers import Integral

import numpy as np
import pandas as pd


def _window(value: int, name: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return int(value)


def _values(series: pd.Series, *, prices: bool = False) -> pd.Series:
    if not isinstance(series, pd.Series):
        raise TypeError("factor input must be a pandas Series")
    if not series.index.is_unique or not series.index.is_monotonic_increasing:
        raise ValueError("factor input index must be unique and ascending")
    values = pd.to_numeric(series, errors="raise").astype(float).copy()
    if np.isinf(values.to_numpy()).any():
        raise ValueError("factor input contains infinite values")
    if prices and (values.dropna() <= 0).any():
        raise ValueError("observed prices must be positive")
    return values


def reversal(prices: pd.Series, window: int = 5) -> pd.Series:
    """Negative trailing simple return, matching the original pilot formula."""
    window = _window(window, "window")
    values = _values(prices, prices=True)
    return (1.0 - values / values.shift(window)).rename("value")


def momentum(prices: pd.Series, lookback: int = 60, skip: int = 5) -> pd.Series:
    """Return from t-lookback to t-skip, excluding the recent skip sessions."""
    lookback = _window(lookback, "lookback")
    skip = _window(skip, "skip", minimum=0)
    if skip >= lookback:
        raise ValueError("skip must be smaller than lookback")
    values = _values(prices, prices=True)
    return (values.shift(skip) / values.shift(lookback) - 1.0).rename("value")


def low_volatility(returns: pd.Series, window: int = 20) -> pd.Series:
    """Negative rolling sample standard deviation (ddof=1), in decimal units."""
    window = _window(window, "window", minimum=2)
    values = _values(returns)
    if (values.dropna() < -1.0).any():
        raise ValueError("simple returns cannot be smaller than -1")
    return (-values.rolling(window, min_periods=window).std(ddof=1)).rename("value")
