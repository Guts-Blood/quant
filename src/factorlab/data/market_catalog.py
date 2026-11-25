"""DuckDB tables and date checks for a pinned on-disk market snapshot."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

BASE_TABLES = ("daily", "valuation", "fundamentals", "dividends")


def literal(path):
    return "'" + str(Path(path).resolve()).replace("'", "''") + "'"


def install(db, root, files):
    for name in BASE_TABLES:
        db.execute(
            f"CREATE VIEW {name} AS SELECT * FROM read_parquet({literal(root / (name + '.parquet'))})"
        )
    db.execute("""CREATE VIEW research_daily AS
        SELECT d.*, v.turn, v.pe_ttm, v.pb_mrq, v.ps_ttm, v.is_st
        FROM daily d LEFT JOIN valuation v USING (stock_code, date)""")
    db.execute("CREATE TABLE trading_calendar (date DATE PRIMARY KEY)")
    db.execute("""CREATE TABLE csi500_intervals (stock_code VARCHAR, valid_from DATE, valid_to DATE,
               PRIMARY KEY(stock_code, valid_from), CHECK(valid_to >= valid_from))""")
    names = {s["name"] for s in files}
    if "calendar.txt" in names:
        db.execute(f"""INSERT INTO trading_calendar SELECT date FROM read_csv({literal(root / "calendar.txt")},
                     header=false, columns={{'date':'DATE'}})""")
    if "csi500.tsv" in names:
        db.execute(f"""INSERT INTO csi500_intervals
            SELECT CASE WHEN regexp_full_match(code, '(SH|SZ)[0-9]{{6}}') THEN right(code,6)
                        ELSE error('invalid membership code') END, start_date, end_date
            FROM read_csv({literal(root / "csi500.tsv")}, header=false, delim='\t',
                columns={{'code':'VARCHAR','start_date':'DATE','end_date':'DATE'}})""")
        overlap = db.execute("""SELECT stock_code FROM (
            SELECT *,lag(valid_to) OVER (PARTITION BY stock_code ORDER BY valid_from) AS previous_end
            FROM csi500_intervals) WHERE valid_from<=previous_end LIMIT 1""").fetchone()
        if overlap:
            raise ValueError(f"Overlapping membership intervals for {overlap[0]}")
    db.execute("""CREATE VIEW csi500_members AS
        SELECT c.date, i.stock_code FROM trading_calendar c JOIN csi500_intervals i
        ON c.date BETWEEN i.valid_from AND i.valid_to""")
    db.execute("""CREATE TABLE option_observations (
        date DATE, kind VARCHAR, underlying VARCHAR, asof_time TIMESTAMPTZ, available_at TIMESTAMPTZ,
        ingested_at TIMESTAMPTZ, source_id VARCHAR, rate DOUBLE, status VARCHAR, iv30 DOUBLE,
        reason VARCHAR, method_version VARCHAR, partition_sha256 VARCHAR,
        PRIMARY KEY(date,kind,underlying))""")
    db.execute("""CREATE TABLE option_quotes (
        date DATE, kind VARCHAR, underlying VARCHAR, contract_id VARCHAR, quoted_at TIMESTAMPTZ,
        strike DOUBLE, expiry TIMESTAMPTZ, side VARCHAR, bid DOUBLE, ask DOUBLE, adjusted BOOLEAN,
        PRIMARY KEY(date,kind,underlying,contract_id))""")
    for entry in files:
        if entry.get("dataset") != "option_chain":
            continue
        p = json.loads((root / entry["name"]).read_bytes())
        iv = p["calculation"]
        db.execute(
            "INSERT INTO option_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                p["date"],
                p["kind"],
                p["underlying"],
                p["asof"],
                p["available_at"],
                p["ingested_at"],
                p["source_id"],
                p["rate"],
                iv["status"],
                iv.get("iv30"),
                iv.get("reason"),
                iv["method_version"],
                entry["sha256"],
            ],
        )
        rows = [
            (
                p["date"],
                p["kind"],
                p["underlying"],
                q["contract_id"],
                q.get("quoted_at"),
                q.get("strike"),
                q.get("expiry"),
                q.get("kind"),
                q.get("bid"),
                q.get("ask"),
                q.get("adjusted"),
            )
            for q in p["quotes"]
        ]
        if rows:
            db.executemany(
                """INSERT INTO option_quotes VALUES (?,?,?,?,try_cast(? AS TIMESTAMPTZ),
                          try_cast(? AS DOUBLE),try_cast(? AS TIMESTAMPTZ),?,try_cast(? AS DOUBLE),
                          try_cast(? AS DOUBLE),try_cast(? AS BOOLEAN))""",
                rows,
            )
    if "execution.parquet" in names:
        db.execute(
            f"CREATE VIEW execution AS SELECT * FROM read_parquet({literal(root / 'execution.parquet')})"
        )
        db.execute("""SELECT date,stock_code,adj_return,tradable,action,limit_up,limit_down,
                             limit_exempt,cash_per_share,pay_date FROM execution LIMIT 0""")
    db.execute("""CREATE VIEW session_coverage AS
        WITH prices AS (SELECT date, count(*) n FROM daily GROUP BY date),
             valuations AS (SELECT date, count(*) n FROM valuation GROUP BY date),
             members AS (SELECT date, count(*) n, count(DISTINCT stock_code) codes FROM csi500_members GROUP BY date)
        SELECT c.date, prices.n price_rows, valuations.n valuation_rows, members.codes member_count,
               coalesce(prices.n>0 AND valuations.n>0 AND members.n=500 AND members.codes=500,false) usable
        FROM trading_calendar c LEFT JOIN prices USING(date) LEFT JOIN valuations USING(date)
        LEFT JOIN members USING(date)""")


def inventory(db):
    clocks = {
        "daily": ("date",),
        "valuation": ("date",),
        "fundamentals": ("stat_date", "pub_date"),
        "dividends": ("ann_date", "record_date", "ex_date"),
        "trading_calendar": ("date",),
        "csi500_intervals": ("valid_from", "valid_to"),
        "option_quotes": ("date",),
        "option_observations": ("date",),
    }
    if ("execution",) in db.execute("SHOW TABLES").fetchall():
        clocks["execution"] = ("date",)
    result = {}
    for table, fields in clocks.items():
        periods = {}
        for field in fields:
            count, lo, hi = db.execute(f"""SELECT count(*), min(CAST({field} AS DATE)),
                                              max(CAST({field} AS DATE)) FROM {table}""").fetchone()
            periods[field] = [str(lo) if lo else None, str(hi) if hi else None]
        result[table] = {"rows": count, "coverage": periods}
    return result


def preflight(db, start=None, end=None):
    """No extrapolation; enforce both endpoints and each intervening exchange session."""
    lo, hi, sessions = db.execute("""SELECT min(date),max(date),count(*) FROM session_coverage
                                   WHERE usable""").fetchone()
    result = {
        "common_start": str(lo) if lo else None,
        "common_end": str(hi) if hi else None,
        "common_sessions": sessions,
    }
    if start is None and end is None:
        return result
    if start is None or end is None:
        raise ValueError("Supply both start and end")
    start, end = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    if not lo or not lo <= start <= end <= hi:
        raise ValueError(
            f"Requested {start}..{end} outside joint price/valuation/calendar/membership coverage {lo}..{hi}"
        )
    days = db.execute(
        "SELECT date, usable FROM session_coverage WHERE date BETWEEN ? AND ? ORDER BY date", [start, end]
    ).fetchall()
    if not days or days[0][0] != start or days[-1][0] != end or not all(ok for _, ok in days):
        raise ValueError(
            "Requested bounds must be trading sessions with complete session coverage throughout"
        )
    # Detect trading days present in the market feed but missing inside the pinned calendar.
    stray = db.execute(
        """SELECT DISTINCT CAST(date AS DATE) FROM daily WHERE date BETWEEN ? AND ?
                          EXCEPT SELECT date FROM trading_calendar""",
        [start, end],
    ).fetchall()
    if stray:
        raise ValueError(
            f"Daily market dates missing from calendar: {sorted(str(d[0]) for d in stray)[:5]}"
        )
    return result
