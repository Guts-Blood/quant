"""Within-session transformations; no statistic is fitted across future dates."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .data.asof import _aware
from .data.inputs import signal_cutoffs
from .evaluation.metrics import _table


def _factors(frame):
    result = _table(frame, ("date", "symbol", "factor", "value"),
                    ["date", "symbol", "factor"], "factors")
    result["value"] = pd.to_numeric(result.value, errors="raise").astype(float)
    if np.isinf(result.value.to_numpy()).any():
        raise ValueError("infinite factor values")
    return result


def winsorize(factors, *, lower=0.01, upper=0.99):
    lower, upper = float(lower), float(upper)
    if not 0 <= lower < upper <= 1:
        raise ValueError("winsorization bounds must satisfy 0 <= lower < upper <= 1")
    result = _factors(factors)
    for _, group in result.groupby(["date", "factor"], sort=False):
        values = group.value.dropna()
        if not values.empty:
            limits = values.quantile([lower, upper])
            result.loc[group.index, "value"] = group.value.clip(limits.iloc[0], limits.iloc[1])
    return result


def standardize(factors):
    """Population z-scores; observed constant groups map to zero, missing stays missing."""
    result = _factors(factors)
    for _, group in result.groupby(["date", "factor"], sort=False):
        values = group.value
        scale = values.std(ddof=0)
        if pd.notna(scale):
            result.loc[group.index, "value"] = ((values - values.mean()) / scale if scale > 0
                                                else values.where(values.isna(), 0.0))
    return result


def neutralize(factors, exposures, *, by_industry=True, by_size=True,
               signal_time="16:00", min_observations=20):
    """OLS residuals against contemporaneous industry and log market cap.

    Exposures must carry their historical availability timestamp. Missing or
    late exposures are never silently replaced with current classifications.
    """
    if not by_industry and not by_size:
        raise ValueError("choose at least one exposure")
    if isinstance(min_observations, bool) or not isinstance(min_observations, int) or min_observations < 2:
        raise ValueError("min_observations must be an integer >= 2")
    result = _factors(factors)
    required = ["date", "symbol", "available_at"]
    if by_industry:
        required.append("industry")
    if by_size:
        required.append("market_cap")
    source = _table(exposures, required, ["date", "symbol"], "exposures")
    available = pd.to_datetime([_aware(x, "exposure available_at") for x in source.available_at], utc=True)
    if (available > signal_cutoffs(source.date, signal_time)).any():
        raise ValueError("exposures were unavailable at the signal cutoff")
    if by_size:
        source["market_cap"] = pd.to_numeric(source.market_cap, errors="raise").astype(float)
        if np.isinf(source.market_cap.to_numpy()).any():
            raise ValueError("infinite market cap")
    joined = result[["date", "symbol", "factor", "value"]].merge(
        source[required], on=["date", "symbol"], how="left", validate="many_to_one"
    )
    result["value"] = np.nan
    result["transform_status"] = "missing_factor_or_exposure"
    for _, group in joined.groupby(["date", "factor"], sort=False):
        valid = group.value.notna() & group.available_at.notna()
        if by_industry:
            valid &= group.industry.notna() & group.industry.astype(str).str.strip().ne("")
        if by_size:
            valid &= group.market_cap.notna() & (group.market_cap > 0)
        sample = group.loc[valid]
        if len(sample) < min_observations:
            result.loc[group.index, "transform_status"] = "insufficient_exposure_sample"
            continue
        columns = [np.ones((len(sample), 1))]
        if by_size:
            size = np.log(sample.market_cap.to_numpy())
            columns.append((size - size.mean()).reshape(-1, 1))
        if by_industry:
            columns.append(pd.get_dummies(sample.industry.astype(str), drop_first=True, dtype=float).to_numpy())
        design = np.column_stack(columns)
        coefficients, _, rank, _ = np.linalg.lstsq(design, sample.value.to_numpy(), rcond=None)
        if len(sample) - rank < 2:
            result.loc[group.index, "transform_status"] = "insufficient_residual_degrees_of_freedom"
            continue
        result.loc[sample.index, "value"] = sample.value.to_numpy() - design @ coefficients
        result.loc[sample.index, "transform_status"] = (
            "ok" if rank == design.shape[1] else "rank_deficient_exposures"
        )
    return result


def combine(factors, weights, *, name="blend", missing_policy="strict"):
    """Weighted percentile-rank blend on each session's supplied cross-section.

    lowest explicitly recreates a lowest-rank treatment of missing components;
    strict keeps the composite missing unless every component is observed.
    """
    frame = _factors(factors)
    if not weights or missing_policy not in {"strict", "lowest"}:
        raise ValueError("provide component weights and a valid missing_policy")
    weights = {key: float(value) for key, value in weights.items()}
    if any(not np.isfinite(value) or value <= 0 for value in weights.values()):
        raise ValueError("component weights must be positive and finite")
    if not set(weights).issubset(set(frame.factor)):
        raise ValueError("a requested component factor is absent")
    if name in set(weights) or not isinstance(name, str) or not name:
        raise ValueError("blend name must be distinct from its components")
    parts = []
    for day, group in frame.groupby("date", sort=True):
        wide = group.pivot(index="symbol", columns="factor", values="value").reindex(columns=list(weights))
        missing = wide.isna().any(axis=1)
        ranks = wide.rank(pct=True, na_option="keep" if missing_policy == "strict" else "top")
        scores = ranks.mul(pd.Series(weights)).sum(axis=1, min_count=len(weights)) / sum(weights.values())
        parts.append(pd.DataFrame({"date": day, "symbol": wide.index, "factor": name,
                                   "value": scores.to_numpy(),
                                   "missing_reason": np.where(missing,
                                       "missing_component" if missing_policy == "strict" else "lowest_rank_imputation", "")}))
    return pd.concat(parts, ignore_index=True).sort_values(["date", "symbol"]).reset_index(drop=True)
