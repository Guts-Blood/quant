"""Daily-close signals executed at the next session's raw opening price.

Capacity is an estimate from the PREVIOUS session's volume, not actual auction
liquidity. Orders are sized and locked before any of the next day's fills.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal

import numpy as np
import pandas as pd

from ..data.inputs import _booleans, signal_cutoffs
from ..data.panel import sessions
from ..evaluation.metrics import _table
from ..factors.price_volume import _window
from .data import Day, Order
from .engine import Book, match, money

D = Decimal
DEFAULT_COSTS = {"commission_bps": "3", "min_commission": "5",
                 "transfer_bps": "0.1", "sell_tax_bps": "5",
                 "slippage_bps": "5", "participation": "0.01"}


def _decimal(value, name, *, positive=False):
    try:
        result = D(str(value))
    except Exception as exc:
        raise ValueError(f"invalid {name}: {value}") from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(f"invalid {name}: {value}")
    return result


def _price(value, name):
    result = _decimal(value, name, positive=True)
    if result != money(result):
        raise ValueError(f"{name} must be an unadjusted price in cents")
    return result


def _costs(overrides):
    overrides = overrides or {}
    if set(overrides) - set(DEFAULT_COSTS):
        raise ValueError("unknown execution cost parameter")
    result = {key: _decimal(value, key) for key, value in (DEFAULT_COSTS | overrides).items()}
    if not 0 < result["participation"] <= 1:
        raise ValueError("participation must be in (0, 1]")
    return result


def _market(panel, calendar):
    required = ("date", "symbol", "open", "close", "volume", "tradable", "action",
                "limit_down", "limit_up")
    frame = _table(panel, required, ["date", "symbol"], "execution panel")
    if not frame.date.isin(calendar).all():
        raise ValueError("execution date outside calendar")
    frame["tradable"] = _booleans(frame.tradable, "tradable")
    if "limit_exempt" in frame:
        frame["limit_exempt"] = _booleans(frame.limit_exempt, "limit_exempt")
    else:
        frame["limit_exempt"] = False
    result = {}
    for row in frame.to_dict("records"):
        day, code = row["date"], row["symbol"]
        close = _price(row["close"], "close")
        opening = None if pd.isna(row["open"]) else _price(row["open"], "open")
        volume = _decimal(row["volume"], "volume")
        if volume != volume.to_integral_value():
            raise ValueError("volume must be integral shares")
        if row["limit_exempt"]:
            if not pd.isna(row["limit_down"]) or not pd.isna(row["limit_up"]):
                raise ValueError("limit-exempt rows must have empty price bands")
            lower = upper = None
        else:
            lower, upper = (_price(row[key], key) for key in ("limit_down", "limit_up"))
            if not lower < upper or not lower <= close <= upper or (opening is not None and not lower <= opening <= upper):
                raise ValueError("raw prices outside declared price bands")
        action = row["action"]
        if action not in {"none", "cash", "ex", "unknown"}:
            raise ValueError("action must be none, cash, ex or unknown")
        dividend, pay_date = D(0), ""
        if action == "cash":
            dividend = _decimal(row.get("cash_per_share"), "cash_per_share", positive=True)
            pay = pd.Timestamp(row.get("pay_date"))
            if pd.isna(pay) or pay.tzinfo is not None or pay != pay.normalize() or pay < day:
                raise ValueError("invalid dividend pay_date")
            pay_date = pay.date().isoformat()
        elif pd.notna(row.get("cash_per_share")) and D(str(row["cash_per_share"])) != 0:
            raise ValueError("cash_per_share requires cash action")
        info = Day(close, lower, upper, "trading" if row["tradable"] else "suspended",
                   action, dividend, pay_date)
        result[day, code] = (info, opening, int(volume))
    return result


def _targets(targets, calendar):
    frame = _table(targets, ("date", "symbol", "weight", "signal_valid"),
                   ["date", "symbol"], "target weights")
    if not frame.date.isin(calendar).all():
        raise ValueError("signal date outside calendar")
    frame["signal_valid"] = _booleans(frame.signal_valid, "signal_valid")
    frame["weight"] = pd.to_numeric(frame.weight, errors="raise").astype(float)
    if not np.isfinite(frame.weight).all() or not frame.weight.between(0, 1).all():
        raise ValueError("target weights must be finite and in [0, 1]")
    result = {}
    for day, part in frame.groupby("date", sort=True):
        if part.signal_valid.nunique() != 1 or part.weight.sum() > 1 + 1e-10:
            raise ValueError("inconsistent signal validity or leveraged target weights")
        valid = bool(part.signal_valid.iloc[0])
        if not valid and part.weight.sum() > 0:
            raise ValueError("invalid signal cannot carry positive target weights")
        result[day] = (valid, dict(zip(part.symbol, part.weight)))
    return result


def simulate(panel, targets, *, calendar, start=None, end=None, initial_cash=100000,
             costs=None, lot_size=100, rebalance_every=1, max_positions=10,
             missing_signal="hold", signal_time="16:00"):
    """Return daily NAV, holdings, locked orders, events, and declared assumptions.

    A missing held-security close or unsupported corporate action aborts the run.
    No guessed mark, adjustment, or automatic liquidation is substituted.
    """
    calendar = sessions(calendar)
    lot_size = _window(lot_size, "lot_size")
    rebalance_every = _window(rebalance_every, "rebalance_every")
    max_positions = _window(max_positions, "max_positions")
    if missing_signal not in {"hold", "cash", "error"}:
        raise ValueError("missing_signal must be hold, cash or error")
    capital = _decimal(initial_cash, "initial_cash", positive=True)
    if capital != money(capital):
        raise ValueError("initial cash must be in cents")
    fees = _costs(costs)
    market, signals = _market(panel, calendar), _targets(targets, calendar)
    # Validate even if the first signal happens to be absent.
    signal_cutoffs(calendar, signal_time)
    begin = calendar[0] if start is None else pd.Timestamp(start)
    finish = calendar[-1] if end is None else pd.Timestamp(end)
    if begin not in calendar or finish not in calendar or begin > finish:
        raise ValueError("backtest bounds must be ordered calendar sessions")
    run_dates = calendar[(calendar >= begin) & (calendar <= finish)]
    book, nav_previous = Book(capital), capital
    nav_rows, holding_rows, orders, events = [], [], [], []
    for run_index, day in enumerate(run_dates):
        day_index = calendar.get_loc(day)
        previous = calendar[day_index - 1] if day_index else None
        planned = []
        if run_index % rebalance_every == 0:
            valid, weights = signals.get(previous, (False, {}))
            if not valid:
                events.append({"date": day, "event": "missing_signal", "policy": missing_signal})
                if missing_signal == "error":
                    raise ValueError(f"missing prior-session signal for {day.date()}")
                weights = {} if missing_signal == "cash" else None
            if weights is not None:
                if sum(weight > 0 for weight in weights.values()) > max_positions:
                    raise ValueError("targets exceed max_positions")
                known = signal_cutoffs([previous], signal_time).iloc[0].isoformat() if previous is not None else ""
                for code in sorted(set(weights) | set(book.holdings)):
                    desired = 0
                    if weights.get(code, 0) > 0:
                        prior = market.get((previous, code))
                        if prior is None:
                            raise ValueError(f"missing sizing price: {previous} {code}")
                        desired = int(nav_previous * D(str(weights[code])) / prior[0].close / lot_size) * lot_size
                    delta = desired - book.holdings[code]
                    if delta:
                        planned.append(Order(code, "buy" if delta > 0 else "sell", abs(delta), known))
        # Corporate actions apply to holdings carried into this session.
        book.sellable = Counter(book.holdings)
        for code, shares in list(book.holdings.items()):
            current = market.get((day, code))
            if current is None:
                raise ValueError(f"missing held-security observation: {day.date()} {code}")
            info = current[0]
            if info.action in {"unknown", "ex"}:
                raise ValueError(f"unsupported held-security corporate action: {day.date()} {code}")
            if info.action == "cash":
                book.receivables[info.pay_date] += money(shares * info.cash_per_share)
        for pay_date in list(book.receivables):
            if pay_date <= day.date().isoformat():
                book.cash += book.receivables.pop(pay_date)
        traded, day_fees = D(0), D(0)
        # No order is resized after seeing these fills or current opening prices.
        for order in sorted(planned, key=lambda value: (value.side != "sell", value.code)):
            current, prior = market.get((day, order.code)), market.get((previous, order.code))
            info = current[0] if current else None
            if order.side == "buy" and order.code not in book.holdings and len(book.holdings) >= max_positions:
                fill = {"reason": "position_cap"}
            elif current and current[1] is not None and prior is None:
                fill = {"reason": "missing_prior_volume"}
            else:
                bar = (current[1], prior[2]) if current and current[1] is not None and prior else None
                fill = match(book, order, info, bar, fees, day_index)
            row = {"date": day, "signal_date": previous, "known_at": order.known_at,
                   "symbol": order.code, "side": order.side, "requested_shares": order.shares,
                   "filled_shares": order.shares if fill["reason"] == "filled" else 0,
                   "cash_after": str(book.cash), **fill}
            orders.append(row)
            if fill["reason"] == "filled":
                traded += D(fill["gross"])
                day_fees += D(fill["fees"])
        market_value = D(0)
        for code, shares in sorted(book.holdings.items()):
            current = market.get((day, code))
            if current is None:
                raise ValueError(f"missing closing mark: {day.date()} {code}")
            value = shares * current[0].close
            market_value += value
            holding_rows.append({"date": day, "symbol": code, "shares": shares,
                                 "close": str(current[0].close), "market_value": str(value)})
        receivable = sum(book.receivables.values(), D(0))
        nav = book.cash + receivable + market_value
        nav_rows.append({"date": day, "cash": float(book.cash), "receivables": float(receivable),
                         "market_value": float(market_value), "nav": float(nav),
                         "return": float(nav / nav_previous - 1), "fees": float(day_fees),
                         "traded_value": float(traded), "half_gross_turnover": float(traded / nav_previous / 2),
                         "positions": len(book.holdings)})
        nav_previous = nav
    daily = pd.DataFrame(nav_rows)
    peaks = np.maximum.accumulate(np.r_[float(capital), daily.nav.to_numpy()])[1:]
    daily["drawdown"] = daily.nav / peaks - 1
    ledger = pd.DataFrame(orders, columns=["date", "signal_date", "known_at", "symbol", "side",
                "requested_shares", "filled_shares", "reason", "bar_open", "fill_price", "gross",
                "commission", "transfer", "sell_tax", "fees", "slippage_cost", "cash_after"])
    summary = {"initial_cash": float(capital), "ending_nav": float(nav_previous),
               "net_return": float(nav_previous / capital - 1),
               "max_drawdown": float(daily.drawdown.min()), "fees": float(daily.fees.sum()),
               "orders": len(ledger), "fills": int((ledger.reason == "filled").sum()),
               "rejections": ledger.loc[ledger.reason != "filled", "reason"].value_counts().to_dict()}
    assumptions = {"execution_profile": "next_open_prior_volume_estimate",
        "signal_time": signal_time, "sizing": "prior_session_nav_and_raw_close_locked_batch",
        "capacity": "prior_session_volume_times_participation_not_auction_liquidity",
        "costs": {key: str(value) for key, value in fees.items()},
        "cost_schedule": "constant_research_assumption_not_historical_fee_schedule",
        "lot_size": lot_size, "rebalance_every": rebalance_every, "max_positions": max_positions,
        "missing_signal": missing_signal, "cash_dividend_tax": "not_modelled",
        "unsupported_actions": ["splits", "mergers", "unknown_actions"],
        "fill_policy": "all_or_none", "same_day_sale": "T_plus_one"}
    return {"daily": daily, "orders": ledger,
            "holdings": pd.DataFrame(holding_rows, columns=["date", "symbol", "shares", "close", "market_value"]),
            "events": pd.DataFrame(events, columns=["date", "event", "policy"]),
            "summary": summary, "assumptions": assumptions}
