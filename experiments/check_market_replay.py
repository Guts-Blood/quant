"""Check a real two-fill replay against the restored daily market catalog."""
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import argparse

import duckdb
import pandas as pd

from factorlab.backtest.daily import simulate


def check(path):
    with duckdb.connect(str(path), read_only=True) as db:
        bars = db.sql("""SELECT date, stock_code AS symbol, open, close, volume, is_st
                         FROM research_daily WHERE stock_code = '600160'
                         AND date BETWEEN '2023-12-28' AND '2024-01-05' ORDER BY date""").df()
        actions = db.execute("""SELECT count(*) FROM dividends WHERE stock_code = '600160'
                            AND ex_date BETWEEN '2023-12-29' AND '2024-01-05'""").fetchone()[0]
    if actions or bars.is_st.isna().any() or bars.is_st.any() or len(bars) != 6 or (bars.volume <= 0).any():
        raise ValueError("Historical fixture has changed; review corporate actions, dates and trading status")
    previous = bars.close.shift(1)
    bars = bars.iloc[1:].copy()
    for field, multiple in (("limit_down", "0.9"), ("limit_up", "1.1")):
        bars[field] = [(Decimal(str(px)) * Decimal(multiple)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
                       for px in previous.iloc[1:]]
    bars = bars.assign(action="none", tradable=True)
    targets = pd.DataFrame({"date": pd.to_datetime(["2023-12-29", "2024-01-04"]),
                            "symbol": "600160", "weight": [0.02, 0.0], "signal_valid": True})
    result = simulate(bars, targets, calendar=bars.date, start="2024-01-02", end="2024-01-05",
                      initial_cash=100000, missing_signal="hold")
    orders = result["orders"]
    if orders.reason.tolist() != ["filled", "filled"] or orders.side.tolist() != ["buy", "sell"]:
        raise ValueError("Expected the fixed entry and exit to fill")
    if orders.filled_shares.tolist() != [100, 100] or result["daily"].iloc[-1].market_value != 0:
        raise ValueError("Unexpected lot size or ending position")
    return orders[["date", "side", "filled_shares", "fill_price", "fees"]], result["summary"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    ledger, summary = check(parser.parse_args().catalog)
    print(ledger.to_string(index=False))
    print(summary)
