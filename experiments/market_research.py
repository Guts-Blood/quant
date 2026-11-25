"""Versioned value/low-volatility selection and matched next-open exposure experiments."""

import argparse
from datetime import date
from importlib.metadata import version
import json
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd

from factorlab.artifacts import write_json
from factorlab.backtest.daily import simulate
from factorlab.data.market_catalog import preflight
from factorlab.experiments.runner import code_identity, environment, save_table
from factorlab.factors import earnings_yield, low_volatility
from .market_version import checksum, open_snapshot

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/market-data/value-lowvol-iv30.json"
CANDIDATE_COLUMNS = [
    "signal_date",
    "trade_date",
    "symbol",
    "rank",
    "score",
    "close",
    "base_weight",
    "member_on_trade_date",
]
METRICS = ("net_return", "max_drawdown", "mean_actual_exposure", "half_gross_turnover", "fees")


def overlay(db, calendar, cfg):
    rows = db.execute(
        """SELECT date, iv30 FROM option_observations WHERE underlying='510500'
        AND kind='daily' AND status='ok' AND isfinite(iv30) AND iv30>0
        AND timezone('Asia/Shanghai',available_at) <= date + CAST(? AS TIME)
        ORDER BY date""",
        [cfg["signal_time"]],
    ).df()
    series = rows.assign(date=pd.to_datetime(rows.date)).set_index("date").iv30.reindex(calendar)
    prior = series.shift(1).rolling(cfg["iv_window"], min_periods=cfg["iv_min_valid"])
    result = pd.DataFrame(
        {
            "iv30": series,
            "threshold": prior.quantile(cfg["iv_quantile"]),
            "prior_valid": series.shift(1).rolling(cfg["iv_window"], min_periods=0).count(),
        }
    )
    result["valid"] = result.iv30.notna() & result.threshold.notna()
    result["high"] = result.valid & result.iv30.ge(result.threshold)
    return result


def shortlist(db, calendar, trade_days, cfg, common_start):
    # Align every return, turnover and tradability observation to explicit exchange sessions.
    first = calendar.get_loc(trade_days[0])
    history = calendar[max(0, first - cfg["min_history"]) : calendar.get_loc(trade_days[-1]) + 1]
    history = history[history >= pd.Timestamp(common_start)]
    members = db.execute(
        """SELECT date,stock_code FROM csi500_members
        WHERE date BETWEEN ? AND ? ORDER BY date,stock_code""",
        [history[0], history[-1]],
    ).df()
    by_day = members.groupby(pd.to_datetime(members.date)).stock_code.agg(set)
    lo, hi = history[0], history[-1]
    panel = db.execute(
        """WITH codes AS (SELECT DISTINCT stock_code FROM csi500_members
          WHERE date BETWEEN ? AND ?) SELECT d.date,d.stock_code,d.close,d.amount,v.is_st,v.pe_ttm,
          e.tradable,e.adj_return, e.stock_code IS NOT NULL AS has_execution
          FROM daily d JOIN codes USING(stock_code)
          LEFT JOIN valuation v USING(date,stock_code) LEFT JOIN execution e USING(date,stock_code)
          WHERE d.date BETWEEN ? AND ? ORDER BY d.date,d.stock_code""",
        [lo, hi, lo, hi],
    ).df()
    index = pd.MultiIndex.from_product(
        [history, sorted(set(members.stock_code))], names=["date", "stock_code"]
    )
    panel["date"] = pd.to_datetime(panel.date)
    grid = panel.set_index(["date", "stock_code"]).reindex(index)
    # Distinguish legitimate observation gaps in older records from truncated supplier panels.
    for day, codes in by_day.items():
        today = grid.loc[day].reindex(sorted(codes))
        if today.has_execution.isna().any() or not today.has_execution.all() or today.is_st.isna().any():
            raise ValueError(f"Incomplete member price, valuation or execution status on {day.date()}")
    eligible_close = grid.close.where(grid.tradable.eq(True))
    grid["history"] = eligible_close.groupby(level=1).transform(
        lambda s: s.rolling(cfg["min_history"], min_periods=1).count()
    )
    grid["liquidity"] = grid.amount.groupby(level=1).transform(
        lambda s: s.rolling(cfg["liquidity_window"]).mean()
    )
    grid["lowvol"] = grid.adj_return.groupby(level=1).transform(
        lambda s: low_volatility(s.droplevel(1), cfg["volatility_window"]).to_numpy()
    )
    grid["value"] = grid.pe_ttm.groupby(level=1).transform(
        lambda s: earnings_yield(s.droplevel(1)).to_numpy()
    )
    previous_targets = []
    candidates = []
    for trade in trade_days:
        pos = calendar.get_loc(trade)
        if not pos:
            continue
        signal = calendar[pos - 1]
        if signal not in history or signal not in by_day or trade not in by_day:
            continue
        # Freeze the prior-close ranking. At the next open, block removed names without reranking.
        s = grid.loc[signal].reindex(sorted(by_day[signal]))
        s = s.loc[
            s.tradable.eq(True)
            & s.is_st.eq(False)
            & s.history.ge(cfg["min_history"])
            & s.liquidity.ge(cfg["min_mean_amount"])
            & s.lowvol.notna()
        ].copy()
        if s.empty:
            previous_targets = []
            continue
        s["score"] = (s.value.rank(pct=True, na_option="top") + s.lowvol.rank(pct=True)) / 2
        ordered = s.sort_values(["score", "stock_code"], ascending=[False, True]).index.tolist()
        ranks = {code: i for i, code in enumerate(ordered, 1)}
        keep = [code for code in previous_targets if ranks.get(code, np.inf) <= cfg["retain_rank"]]
        selected = (keep + [code for code in ordered if code not in keep])[: cfg["max_positions"]]
        previous_targets = selected
        for code in sorted(selected, key=ranks.get):
            candidates.append(
                [
                    signal,
                    trade,
                    code,
                    ranks[code],
                    s.at[code, "score"],
                    s.at[code, "close"],
                    1 / cfg["max_positions"],
                    code in by_day[trade],
                ]
            )
    return pd.DataFrame(candidates, columns=CANDIDATE_COLUMNS)


def execution_bars(db, names, start, end):
    # Retain marks for positions even after they leave the index.
    return db.execute(
        """SELECT d.date, d.stock_code AS symbol, d.open,d.close,d.volume,e.tradable,
        e.action,e.limit_down,e.limit_up,e.limit_exempt,e.cash_per_share,e.pay_date
        FROM daily d JOIN execution e USING(date,stock_code)
        WHERE d.date BETWEEN ? AND ? AND d.stock_code IN (SELECT unnest(?))
        ORDER BY d.date,d.stock_code""",
        [start, end, sorted(names)],
    ).df()


def run(db, manifest, digest, *, start, end, out, mode):
    cfg = json.loads(CONFIG.read_text())
    common = preflight(db, start, end)
    calendar = pd.DatetimeIndex(
        pd.to_datetime(
            [
                r[0]
                for r in db.execute(
                    "SELECT date FROM trading_calendar WHERE date <= ? ORDER BY date", [end]
                ).fetchall()
            ]
        ),
        name="date",
    )
    trade_days = calendar[calendar >= pd.Timestamp(start)]
    risk = overlay(db, calendar, cfg)
    schedule = pd.DataFrame(
        {
            "trade_date": trade_days,
            "signal_date": pd.Series(calendar, index=calendar).shift().reindex(trade_days).to_numpy(),
        }
    )
    schedule = schedule.join(risk, on="signal_date")
    reasons = []
    has_execution = ("execution",) in db.execute("SHOW TABLES").fetchall()
    if not has_execution:
        reasons.append("missing_execution_and_adjusted_returns")
    if mode == "compare" and not schedule.valid.eq(True).any():
        reasons.append("no_prior_warmed_fixed_time_iv30_on_requested_signals")
    candidates = (
        shortlist(db, calendar, trade_days, cfg, common["common_start"])
        if has_execution
        else pd.DataFrame(columns=CANDIDATE_COLUMNS)
    )
    schedule["has_candidates"] = schedule.trade_date.isin(candidates.trade_date)
    schedule["effective"] = schedule.has_candidates
    if mode == "compare":
        schedule["effective"] &= schedule.valid.eq(True)
    schedule["triggered"] = schedule.effective & schedule.high.eq(True)
    effective = schedule.loc[schedule.effective]
    args = (
        {"original": 1.0}
        if mode == "baseline"
        else {"original": 1.0, "iv30_dynamic": None, "fixed_95": cfg["fixed_exposure"]}
    )
    metrics = {name: dict.fromkeys(METRICS) for name in args}
    if has_execution and candidates.empty:
        reasons.append("no_eligible_stock_shortlists")
    save_table(out, "candidates.csv", candidates)
    save_table(out, "schedule.csv", schedule)
    if not effective.empty:
        selected = candidates.loc[candidates.trade_date.isin(effective.trade_date)].copy()
        market = execution_bars(db, set(selected.symbol), selected.signal_date.min(), trade_days[-1])
        # Start the matched measurement at the first actionable open. Later unquoted dates hold in every arm.
        for name, constant in args.items():
            multiplier = (
                selected.signal_date.map(risk.high).map({True: cfg["high_vol_multiplier"], False: 1.0})
                if constant is None
                else constant
            )
            targets = selected.assign(
                date=selected.signal_date,
                weight=selected.base_weight.where(selected.member_on_trade_date, 0.0) * multiplier,
                signal_valid=True,
            )
            result = simulate(
                market,
                targets,
                calendar=calendar,
                start=effective.trade_date.iloc[0],
                end=end,
                initial_cash=cfg["initial_cash"],
                costs=cfg["costs"],
                max_positions=cfg["max_positions"],
                signal_time=cfg["signal_time"],
                missing_signal="hold",
            )
            daily, stats = result["daily"], result["summary"]
            metrics[name] = {
                **stats,
                "mean_actual_exposure": float((daily.market_value / daily.nav).mean()),
                "half_gross_turnover": float(daily.half_gross_turnover.sum()),
            }
            for table in ("orders", "daily", "holdings", "events"):
                save_table(out, f"{name}-{table}.csv", result[table])
    source = code_identity()
    source["experiment_files"] = {
        p: checksum(ROOT / p)
        for p in (
            "experiments/market_research.py",
            "experiments/market_version.py",
            "experiments/option_capture.py",
            "experiments/legacy_pilot/risk_expectations.py",
        )
    }
    report = {
        "status": "ok" if not effective.empty else "unavailable",
        "mode": mode,
        "reasons": reasons,
        "snapshot": manifest["snapshot"],
        "manifest_sha256": digest,
        "files": {f["name"]: f["sha256"] for f in manifest["files"]},
        "code": source,
        "environment": {**environment(), "duckdb": version("duckdb")},
        "config": cfg,
        "config_sha256": checksum(CONFIG),
        "evaluation_window": [str(start), str(end)],
        "common": common,
        "requested_sessions": len(schedule),
        "evaluation_class": (
            "prospective_after_freeze"
            if start > date.fromisoformat(cfg["protocol_frozen_on"])
            else "historical_replay"
        ),
        "effective_rebalances": len(effective),
        "high_vol_rebalances": int(schedule.triggered.sum()),
        "fixed_time_iv_valid_signal_dates": int(schedule.valid.eq(True).sum()),
        "measured_from": str(effective.trade_date.iloc[0].date()) if len(effective) else None,
        "metrics": metrics,
        "outputs": {p.name: checksum(p) for p in sorted(out.glob("*.csv"))},
    }
    write_json(out / "run.json", report)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["baseline", "compare"])
    p.add_argument("snapshot", type=Path)
    p.add_argument("--start", type=date.fromisoformat, required=True)
    p.add_argument("--end", type=date.fromisoformat, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with (
        open_snapshot(a.snapshot) as (db, manifest, digest),
        tempfile.TemporaryDirectory(dir=a.out.parent) as tmp,
    ):
        stage = Path(tmp) / "run"
        stage.mkdir()
        report = run(db, manifest, digest, start=a.start, end=a.end, out=stage, mode=a.mode)
        if a.out.exists():
            if (
                not (a.out / "run.json").is_file()
                or (a.out / "run.json").read_bytes() != (stage / "run.json").read_bytes()
            ):
                raise ValueError("Output directory belongs to another run")
            for name, sha in report["outputs"].items():
                if checksum(a.out / name) != sha:
                    raise ValueError(f"Changed run output: {name}")
        else:
            stage.rename(a.out)
    print(
        json.dumps(
            {
                k: report[k]
                for k in ("status", "reasons", "effective_rebalances", "high_vol_rebalances", "metrics")
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
