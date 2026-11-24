"""Valuation factors using only inputs supplied by the point-in-time data layer."""
from __future__ import annotations

import pandas as pd

from .price_volume import _values


def earnings_yield(pe_ttm: pd.Series) -> pd.Series:
    """Inverse positive trailing PE; nonpositive or absent PE remains missing.

    This preserves the pilot's raw value factor. Its historical portfolio policy
    of assigning missing values the lowest rank belongs to portfolio configuration.
    Data availability and the historical vintage of PE must be checked upstream.
    """
    values = _values(pe_ttm)
    return (1.0 / values.where(values > 0.0)).rename("value")
