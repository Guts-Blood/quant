"""Historical membership and signal-time tradability filters."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .data.asof import _aware
from .data.inputs import signal_cutoffs
from .data.panel import prepare_panel
from .factors.price_volume import _window


def membership_mask(panel: pd.DataFrame, events: pd.DataFrame, *, signal_time="16:00", availability_mode="public"):
    """Use immutable membership events, including separately dated removals.

    Each event has symbol, effective_date, available_at, is_member and source_id.
    An exit announced later cannot remove a security from an earlier snapshot.
    """
    required = {"symbol", "effective_date", "available_at", "is_member", "source_id"}
    if not isinstance(events, pd.DataFrame) or not events.columns.is_unique or not required.issubset(events):
        raise ValueError(f"membership events require {sorted(required)}")
    if availability_mode not in {"public", "observed"}:
        raise ValueError("unknown membership availability_mode")
    history = events.copy()
    if availability_mode == "observed":
        if "ingested_at" not in history:
            raise ValueError("observed membership requires ingested_at on every event")
        history["ingested_at"] = pd.to_datetime([_aware(x, "membership ingested_at")
                                                 for x in history.ingested_at], utc=True)
    for column in ("symbol", "source_id"):
        if not history[column].map(lambda x: isinstance(x, str) and bool(x.strip())).all():
            raise ValueError(f"invalid membership {column}")
    history["effective_date"] = pd.to_datetime(history.effective_date, errors="raise")
    dates = pd.DatetimeIndex(history.effective_date)
    if dates.hasnans or dates.tz is not None or not dates.equals(dates.normalize()):
        raise ValueError("membership effective_date must contain dates")
    history["available_at"] = pd.to_datetime([_aware(x, "membership available_at")
                                             for x in history.available_at], utc=True)
    mapping = {"true": True, "false": False, "1": True, "0": False}
    def flag(value):
        if isinstance(value, (bool, np.bool_)):
            return bool(value)
        if str(value).lower() not in mapping:
            raise ValueError("is_member must be an explicit boolean")
        return mapping[str(value).lower()]
    history["is_member"] = history.is_member.map(flag)
    if history.duplicated(["symbol", "effective_date", "available_at"]).any():
        raise ValueError("duplicate or conflicting membership event")
    result = pd.Series(False, index=panel.index)
    for day, group in panel.groupby("date", sort=True):
        cutoff = signal_cutoffs([day], signal_time).iloc[0]
        known = history[(history.effective_date <= day) & (history.available_at <= cutoff)]
        if availability_mode == "observed":
            known = known.loc[known.ingested_at <= cutoff]
        latest = known.sort_values(["symbol", "effective_date", "available_at"], kind="stable")
        latest = latest.groupby("symbol", sort=False).tail(1).set_index("symbol")
        result.loc[group.index] = group.symbol.map(latest.is_member).eq(True)
    return result.astype(bool)


def eligible_universe(panel, *, calendar, membership=None, signal_time="16:00",
                      min_history=61, liquidity_window=20, min_amount=30_000_000.0, availability_mode="public"):
    """Explain exclusions using only current and trailing observations.

    Without membership events the universe is explicitly the supplied panel;
    this makes no claim of representing all historical A-share listings.
    """
    min_history = _window(min_history, "min_history")
    liquidity_window = _window(liquidity_window, "liquidity_window")
    if isinstance(min_amount, bool) or not np.isfinite(float(min_amount)) or float(min_amount) < 0:
        raise ValueError("min_amount must be finite and nonnegative")
    for column in ("is_st", "tradable"):
        if column not in panel or not panel[column].map(lambda x: isinstance(x, (bool, np.bool_))).all():
            raise ValueError(f"{column} must contain explicit booleans")
    aligned = prepare_panel(panel, calendar=calendar, required=("adj_close", "amount"))
    if (aligned.adj_close.dropna() <= 0).any() or (aligned.amount.dropna() < 0).any():
        raise ValueError("invalid price or traded amount")
    aligned["history_price"] = aligned.adj_close.where(aligned.tradable.eq(True))
    history_count = aligned.groupby("symbol", sort=False).history_price.transform(
        lambda values: values.rolling(min_history, min_periods=1).count()
    )
    mean_amount = aligned.groupby("symbol", sort=False).amount.transform(
        lambda values: values.rolling(liquidity_window, min_periods=liquidity_window).mean()
    )
    member = (pd.Series(True, index=aligned.index) if membership is None else
              membership_mask(aligned, membership, signal_time=signal_time, availability_mode=availability_mode))
    checks = {
        "missing_observation": aligned.observed,
        "not_tradable": aligned.tradable.eq(True),
        "st_status": aligned.is_st.eq(False),
        "outside_historical_universe": member,
        "insufficient_history": history_count >= min_history,
        "insufficient_liquidity": mean_amount >= float(min_amount),
    }
    keep = pd.Series(True, index=aligned.index)
    reasons = pd.Series("", index=aligned.index, dtype="object")
    for reason, condition in checks.items():
        valid = condition.fillna(False).astype(bool)
        keep &= valid
        reasons.loc[~valid] += reason + ";"
    return pd.DataFrame({"date": aligned.date, "symbol": aligned.symbol,
                         "eligible": keep, "exclusion_reason": reasons.str.rstrip(";"),
                         "history_observations": history_count, "mean_amount": mean_amount,
                         "member": member})
