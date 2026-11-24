"""Cross-sectional diagnostics, kept separate from executable portfolio returns."""
from __future__ import annotations

from itertools import combinations
from numbers import Integral

import numpy as np
import pandas as pd


def _table(frame, required, key, label):
    if not isinstance(frame, pd.DataFrame) or not frame.columns.is_unique:
        raise ValueError(f"{label} must have unique columns")
    if not set(required).issubset(frame.columns):
        raise ValueError(f"{label} requires {required}")
    result = frame.copy()
    result["date"] = pd.to_datetime(result.date, errors="raise")
    if result.date.dt.tz is not None or not result.date.equals(result.date.dt.normalize()):
        raise ValueError(f"{label} requires timezone-naive session dates")
    for column in ("symbol", "factor"):
        if column in key and not result[column].map(
            lambda value: isinstance(value, str) and bool(value) and value == value.strip()
        ).all():
            raise ValueError(f"invalid {column} in {label}")
    if result.date.isna().any() or result.duplicated(key).any():
        raise ValueError(f"missing dates or duplicate keys in {label}")
    return result.reset_index(drop=True)


def evaluate(factors, labels, *, eligibility=None, quantiles=5, min_observations=20):
    """Return daily diagnostics, equal-weight groups and annual stability tables.

    Quintile spread is a diagnostic difference, not a feasible short portfolio.
    Ties use average ranks for IC and symbol order for reproducible group assignment.
    Coverage is measured before dropping missing future outcomes.
    """
    if isinstance(quantiles, bool) or not isinstance(quantiles, Integral) or quantiles < 2:
        raise ValueError("quantiles must be an integer >= 2")
    if isinstance(min_observations, bool) or not isinstance(min_observations, Integral) or min_observations < quantiles:
        raise ValueError("min_observations must be an integer >= quantiles")
    factor = _table(factors, ("date", "symbol", "factor", "value"),
                    ["date", "symbol", "factor"], "factors")
    outcome = _table(labels, ("date", "symbol", "forward_return"),
                     ["date", "symbol"], "labels")
    frame = factor.merge(outcome[["date", "symbol", "forward_return"]],
                         on=["date", "symbol"], how="left", validate="many_to_one")
    for column in ("value", "forward_return"):
        frame[column] = pd.to_numeric(frame[column], errors="raise").astype(float)
        if np.isinf(frame[column].to_numpy()).any():
            raise ValueError(f"infinite {column}")
    if eligibility is None:
        frame["eligible"] = True
    else:
        mask = _table(eligibility, ("date", "symbol", "eligible"),
                      ["date", "symbol"], "eligibility")
        if mask.eligible.isna().any() or not mask.eligible.map(lambda x: isinstance(x, (bool, np.bool_))).all():
            raise ValueError("eligibility must contain booleans")
        frame = frame.merge(mask[["date", "symbol", "eligible"]],
                            on=["date", "symbol"], how="left", validate="many_to_one")
        if frame.eligible.isna().any():
            raise ValueError("eligibility is missing factor observations")
    daily_rows, group_rows = [], []
    for (day, name), whole in frame.groupby(["date", "factor"], sort=True):
        sample = whole.loc[whole.eligible]
        valid = sample.loc[sample.value.notna()]
        observed = valid.loc[valid.forward_return.notna()]
        row = {"date": day, "factor": name, "eligible": len(sample),
               "factor_observations": len(valid), "outcome_observations": len(observed),
               "factor_coverage": len(valid) / len(sample) if len(sample) else np.nan,
               "outcome_coverage": len(observed) / len(valid) if len(valid) else np.nan,
               "rank_ic": np.nan, "group_spread": np.nan, "status": "ok"}
        if len(observed) < min_observations:
            row["status"] = "insufficient_sample"
        elif observed.value.nunique() < 2:
            row["status"] = "constant_factor"
        else:
            if observed.forward_return.nunique() >= 2:
                row["rank_ic"] = observed.value.rank().corr(observed.forward_return.rank())
            else:
                row["status"] = "constant_outcome"
            ordered = observed.sort_values(["value", "symbol"], kind="stable").copy()
            ordered["group"] = np.arange(len(ordered)) * quantiles // len(ordered) + 1
            means = ordered.groupby("group").forward_return.mean()
            row["group_spread"] = means.loc[quantiles] - means.loc[1]
            for number, part in ordered.groupby("group", sort=True):
                group_rows.append({"date": day, "factor": name, "group": int(number),
                                   "count": len(part), "mean_forward_return": part.forward_return.mean()})
        daily_rows.append(row)
    daily = pd.DataFrame(daily_rows, columns=["date", "factor", "eligible", "factor_observations",
                         "outcome_observations", "factor_coverage", "outcome_coverage", "rank_ic",
                         "group_spread", "status"])
    groups = pd.DataFrame(group_rows, columns=["date", "factor", "group", "count", "mean_forward_return"])
    yearly_rows = []
    if not daily.empty:
        for (name, year), part in daily.groupby(["factor", daily.date.dt.year], sort=True):
            ic = part.rank_ic.dropna()
            deviation = ic.std(ddof=1)
            yearly_rows.append({"factor": name, "year": int(year), "sessions": len(part),
                                "ic_observations": len(ic), "mean_rank_ic": ic.mean(),
                                "rank_ic_std": deviation,
                                "rank_ic_ir": ic.mean() / deviation if deviation > 0 else np.nan,
                                "mean_group_spread": part.group_spread.mean(),
                                "mean_factor_coverage": part.factor_coverage.mean()})
    yearly = pd.DataFrame(yearly_rows, columns=["factor", "year", "sessions", "ic_observations",
                          "mean_rank_ic", "rank_ic_std", "rank_ic_ir", "mean_group_spread",
                          "mean_factor_coverage"])
    return {"daily": daily, "groups": groups, "yearly": yearly}


def factor_correlations(factors, *, min_observations=20):
    """Pairwise Spearman correlations on jointly observed names each day."""
    if isinstance(min_observations, bool) or not isinstance(min_observations, Integral) or min_observations < 2:
        raise ValueError("min_observations must be an integer >= 2")
    frame = _table(factors, ("date", "symbol", "factor", "value"),
                   ["date", "symbol", "factor"], "factors")
    frame["value"] = pd.to_numeric(frame.value, errors="raise").astype(float)
    if np.isinf(frame.value.to_numpy()).any():
        raise ValueError("infinite factor values")
    rows = []
    for day, part in frame.groupby("date", sort=True):
        wide = part.pivot(index="symbol", columns="factor", values="value")
        for left, right in combinations(sorted(wide.columns), 2):
            joint = wide[[left, right]].dropna()
            if len(joint) >= min_observations and (joint.nunique() >= 2).all():
                rows.append({"date": day, "factor_a": left, "factor_b": right,
                             "observations": len(joint),
                             "rank_correlation": joint[left].rank().corr(joint[right].rank())})
    return pd.DataFrame(rows, columns=["date", "factor_a", "factor_b", "observations", "rank_correlation"])