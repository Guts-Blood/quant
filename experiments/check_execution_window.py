"""Replay sourced dividend, removal, suspension and limit-up cases from the historical pilot."""

import argparse
from decimal import Decimal
import json
from pathlib import Path

import pandas as pd

from factorlab.backtest.daily import simulate
from .market_reconciliation import reconcile
from .market_research import execution_bars
from .market_version import open_snapshot


def check(folder):
    cases = [
        ("688289", "2023-12-07", "2023-12-08", "2023-12-11", "filled"),
        ("603816", "2023-11-03", "2023-11-06", "2023-11-06", "suspended"),
        ("002195", "2023-11-20", "2023-11-21", "2023-11-21", "price_limit"),
    ]
    receipts = []
    with open_snapshot(folder) as (db, manifest, digest):
        for code, signal, trade, end, expected in cases:
            calendar = pd.DatetimeIndex(
                [
                    r[0]
                    for r in db.execute(
                        "SELECT date FROM trading_calendar WHERE date BETWEEN ? AND ? ORDER BY date",
                        [signal, end],
                    ).fetchall()
                ]
            )
            bars = execution_bars(db, {code}, signal, end)
            targets = pd.DataFrame(
                [dict(date=pd.Timestamp(signal), symbol=code, weight=0.04, signal_valid=True)]
            )
            result = simulate(bars, targets, calendar=calendar, start=trade, end=end, initial_cash=100000)
            ledger = reconcile(result, bars, 100000)
            order = result["orders"].iloc[0]
            if len(result["orders"]) != 1 or order.reason != expected:
                raise ValueError(f"Unexpected {code} order: {order.reason}")
            case = dict(
                symbol=code,
                trade_date=trade,
                reason=order.reason,
                shares=int(order.filled_shares),
                ending_nav=str(ledger.iloc[-1].nav),
            )
            if code == "688289":
                members = db.execute(
                    "SELECT date FROM csi500_members WHERE stock_code=? AND date BETWEEN ? AND ? ORDER BY date",
                    [code, trade, end],
                ).fetchall()
                paid = ledger.iloc[-1].cash - ledger.iloc[0].cash
                if (
                    members != [(pd.Timestamp(trade).date(),)]
                    or case["shares"] != 200
                    or paid != Decimal("52.44")
                    or ledger.iloc[-1].market_value != Decimal("3730.00")
                ):
                    raise ValueError("Removed holding, raw mark or payment differs from issuer notice")
                case["dividend_paid"] = str(paid)
            receipts.append(case)
    return {"snapshot": manifest["snapshot"], "manifest_sha256": digest, "cases": receipts}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    print(json.dumps(check(parser.parse_args().snapshot), indent=2))
