"""Verify daily holdings and cash independently from fills, corporate actions and raw marks."""

from collections import Counter
from decimal import Decimal

import pandas as pd

from factorlab.backtest.engine import money


def reconcile(result, market, initial_cash):
    bars = market.set_index(["date", "symbol"])
    orders = dict(tuple(result["orders"].groupby("date")))
    holdings = dict(tuple(result["holdings"].groupby("date")))
    cash = Decimal(str(initial_cash))
    shares, receivables = Counter(), Counter()
    rows = []
    for day in result["daily"].itertuples(index=False):
        for code, quantity in shares.items():
            bar = bars.loc[(day.date, code)]
            if bar.action == "cash":
                receivables[pd.Timestamp(bar.pay_date)] += money(
                    quantity * Decimal(str(bar.cash_per_share))
                )
        cash += sum(receivables.pop(d) for d in list(receivables) if d <= day.date)
        fees = Decimal(0)
        if day.date in orders:
            for order in orders[day.date].itertuples(index=False):
                if order.reason != "filled":
                    continue
                sign = 1 if order.side == "buy" else -1
                gross = Decimal(str(order.fill_price)) * order.filled_shares
                cost = sum(Decimal(str(x)) for x in [order.commission, order.transfer, order.sell_tax])
                cash -= sign * gross + cost
                fees += cost
                shares[order.symbol] += sign * order.filled_shares
                if shares[order.symbol] == 0:
                    del shares[order.symbol]
        recorded = holdings[day.date].set_index("symbol").shares.to_dict() if day.date in holdings else {}
        if recorded != dict(shares):
            raise ValueError(f"Position reconciliation failed on {day.date}")
        marks = sum(q * Decimal(str(bars.at[(day.date, c), "close"])) for c, q in shares.items())
        pending = sum(receivables.values())
        ledger = dict(
            cash=cash, receivables=pending, market_value=marks, nav=cash + pending + marks, fees=fees
        )
        for name, amount in ledger.items():
            if Decimal(str(getattr(day, name))) != amount:
                raise ValueError(f"{name} reconciliation failed on {day.date}")
        rows.append({"date": day.date, **ledger, "positions": len(shares)})
    return pd.DataFrame(rows)
