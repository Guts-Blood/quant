"""Deterministic fictional market inputs for offline installation checks."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .artifacts import atomic_write, write_json


def create_demo(destination):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    dates = pd.bdate_range("2023-01-02", periods=100)
    rng = np.random.default_rng(1729)
    rows = []
    for index in range(6):
        previous = 10.0 + index * 4
        for offset, day in enumerate(dates):
            opening = round(previous * (1 + rng.normal(0, 0.003)), 2)
            close = round(opening * (1 + 0.002 * np.sin(offset / (index + 3)) + rng.normal(0, 0.007)), 2)
            rows.append({"date": day.date().isoformat(), "symbol": f"SYN{index + 1:03d}",
                "open": opening, "close": close, "adj_open": opening, "adj_close": close,
                "volume": 2_000_000, "amount": close * 2_000_000,
                "pe_ttm": 10 + index * 3 + offset / 100, "is_st": False, "tradable": True,
                "action": "none", "limit_down": round(previous * 0.9, 2),
                "limit_up": round(previous * 1.1, 2),
                "available_at": day.date().isoformat() + "T15:05:00+08:00"})
            previous = close
    panel = pd.DataFrame(rows).sort_values(["date", "symbol"])
    atomic_write(destination / "panel.csv", panel.to_csv(index=False, lineterminator="\n"))
    atomic_write(destination / "calendar.csv", pd.DataFrame({"date": dates.strftime("%Y-%m-%d")}).to_csv(index=False, lineterminator="\n"))
    config = {"schema_version": 1, "name": "fictional-daily-demo", "output_dir": "runs",
        "data": {"panel": "panel.csv", "calendar": "calendar.csv",
                 "source_id": "synthetic_seed_1729_business_day_calendar_not_exchange_history",
                 "availability_mode": "public", "signal_time": "16:00"},
        "universe": {"mode": "supplied_panel", "min_history": 10, "liquidity_window": 5, "min_amount": 0},
        "factors": [{"name": "reversal", "id": "rev", "parameters": {"window": 5}},
                    {"name": "momentum", "id": "mom", "parameters": {"lookback": 20, "skip": 5}},
                    {"name": "lowvol", "id": "vol", "parameters": {"window": 10}}],
        "transforms": [{"name": "winsorize", "lower": 0.01, "upper": 0.99}, {"name": "standardize"}],
        "labels": {"horizons": [1, 5], "price_column": "adj_open", "entry_lag": 1},
        "evaluation": {"quantiles": 2, "min_observations": 4},
        "portfolio": {"factor": "rev", "top_n": 3, "cash_buffer": 0.05},
        "backtest": {"initial_cash": 100000, "rebalance_every": 5, "missing_signal": "hold",
                     "costs": {"commission_bps": 3, "min_commission": 5, "transfer_bps": 0.1,
                               "sell_tax_bps": 5, "slippage_bps": 5, "participation": 0.01}},
        "splits": [{"name": name, "role": name, "start": dates[first].date().isoformat(),
                    "end": dates[last].date().isoformat()}
                   for name, first, last in [("train", 25, 54), ("validation", 55, 79), ("test", 80, 99)]]}
    write_json(destination / "demo.json", config)
    scan = dict(config, name="fictional-reversal-grid", scan={"max_trials": 6,
                          "grid": {"factors.rev.parameters.window": [2, 5, 10], "portfolio.top_n": [2, 3]}})
    write_json(destination / "scan.json", scan)
    atomic_write(destination / "README.txt", "Fictional offline smoke-test data. Not real securities, investment evidence, or an exchange calendar.\n")
    return destination / "demo.json"
