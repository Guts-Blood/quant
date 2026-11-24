"""Deterministic lot matching copied from the existing two-window pilot.

No factor calculation, downloads, research store or account integration.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from .data import Day

D = Decimal
CENT = D(".01")
BPS = D(10000)


@dataclass
class Book:
    cash: Decimal
    holdings: Counter = field(default_factory=Counter)
    sellable: Counter = field(default_factory=Counter)
    opened: dict = field(default_factory=dict)
    receivables: Counter = field(default_factory=Counter)


def money(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def match(book, order, info: Day | None, bar, costs, day_index):
    """All-or-none simulated fills. Only status/bands and this execution bar enter matching."""
    code, qty = order.code, order.shares
    buy = order.side == "buy"
    if info is None or info.status == "suspended":
        return {"reason": "no_daily_row" if info is None else "suspended"}
    if info.action in ("unknown", "ex"):
        return {"reason": "corporate_action"}
    if bar is None:
        return {"reason": "missing_bar"}
    raw, volume = bar
    if volume == 0:
        return {"reason": "zero_volume"}
    if (buy and raw == info.upper) or (not buy and raw == info.lower):
        return {"reason": "price_limit"}
    if not buy and book.sellable[code] < qty:
        return {"reason": "t_plus_one" if book.holdings[code] >= qty else "insufficient_shares"}
    if qty > volume * costs["participation"]:
        return {"reason": "volume_cap"}
    slip = costs["slippage_bps"] / BPS
    px = (raw * (1 + slip if buy else 1 - slip)).quantize(CENT, rounding=ROUND_CEILING if buy else ROUND_FLOOR)
    if px <= 0 or (info.lower is not None and not info.lower <= px <= info.upper):
        return {"reason": "price_limit"}
    gross = px * qty
    commission = money(max(costs["min_commission"], gross * costs["commission_bps"] / BPS))
    transfer = money(gross * costs["transfer_bps"] / BPS)
    tax = D(0) if buy else money(gross * costs["sell_tax_bps"] / BPS)
    fees = commission + transfer + tax
    next_cash = book.cash + (-gross if buy else gross) - fees
    if next_cash < 0:
        return {"reason": "insufficient_cash"}
    book.cash = next_cash
    if buy:
        book.holdings[code] += qty
        book.opened.setdefault(code, day_index)
    else:
        book.holdings[code] -= qty
        book.sellable[code] -= qty
        if book.holdings[code] == 0:
            del book.holdings[code], book.opened[code]
    return {"reason": "filled", "bar_open": str(raw), "fill_price": str(px),
            "gross": str(gross), "commission": str(commission), "transfer": str(transfer),
            "sell_tax": str(tax), "fees": str(fees), "slippage_cost": str(abs(raw - px) * qty)}
