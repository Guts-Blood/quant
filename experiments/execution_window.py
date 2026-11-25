"""Build the dated CSI500 execution pilot from two immutable Git sources and the parent archive."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import duckdb
import numpy as np
import pandas as pd

from factorlab.data.market_catalog import literal
from .market_statements import prepare as prepare_statements
from .market_snapshot import manifest_bytes, read_manifest, verified, write_once
from .market_version import checksum, file_spec, compose

CONFIG = Path(__file__).resolve().parents[1] / "configs/market-data/execution-pilot.json"
FIELDS = {
    "交易日期": "date",
    "股票代码": "exchange_code",
    "股票名称": "name",
    "中证500成分股": "member",
    "开盘价": "open",
    "最高价": "high",
    "最低价": "low",
    "收盘价": "close",
    "前收盘价": "preclose",
    "成交量": "volume",
    "成交额": "amount",
    "总市值": "market_cap",
    "净利润TTM": "profit_ttm",
    "净资产": "equity",
    "新版申万一级行业名称": "industry",
}


def git_file(root, spec, name, hashes):
    body = subprocess.check_output(["git", "-C", root, "show", f"{spec['commit']}:{name}"])
    hashes[f"{spec['repo']}@{spec['commit']}/{name}"] = hashlib.sha256(body).hexdigest()
    return body


def write_parquet(frame, path):
    with duckdb.connect(config={"threads": 1}) as db:
        db.register("rows", frame)
        db.execute(
            f"COPY (SELECT * FROM rows ORDER BY date,stock_code) TO {literal(path)} (FORMAT PARQUET)"
        )


def source_panel(cfg, days, codes, quotes_repo, stock_repo, hashes):
    quotes = []
    with duckdb.connect() as db, tempfile.TemporaryDirectory() as folder:
        for day in days:
            stem = str(day.date())
            body = git_file(quotes_repo, cfg["quotes"], f"data/v5/daily/{day.year}/{stem}.parquet", hashes)
            audit = json.loads(git_file(quotes_repo, cfg["quotes"], f"data/v5/quality/{stem}.json", hashes))
            if audit["sha256"] != hashlib.sha256(body).hexdigest() or audit["date"] != stem:
                raise ValueError(f"Upstream daily partition failed audit on {stem}")
            path = Path(folder) / "source.parquet"
            path.write_bytes(body)
            rows = db.read_parquet(str(path)).df()
            if not rows.date.eq(day).all() or not rows.adjustflag.eq(3).all():
                raise ValueError(f"Expected unadjusted prices on {stem}")
            quotes.append(rows[rows.symbol.isin(codes)].rename(columns={"symbol": "stock_code"}))
    quotes = pd.concat(quotes, ignore_index=True)
    facts = []
    for code in sorted(codes):
        exchange_code = ("sh" if code.startswith("6") else "sz") + code
        path = f"刑不刑股票数据/{exchange_code}.csv"
        data = git_file(stock_repo, cfg["stock_files"], path, hashes)
        df = pd.read_csv(
            io.BytesIO(data), encoding="gb18030", skiprows=1, usecols=FIELDS.keys(), low_memory=False
        ).rename(columns=FIELDS)
        if not df.exchange_code.eq(exchange_code).all():
            raise ValueError(f"Incorrect security code in {path}")
        df["date"] = pd.to_datetime(df.date)
        facts.append(df.loc[df.date.isin(days)].assign(stock_code=code))
    facts = pd.concat(facts, ignore_index=True)
    keys = ["date", "stock_code"]
    # The BaoStock feed omits this STAR-market CDR. Use observed, positive-volume raw bars.
    cdr = facts[facts.stock_code.eq("689009")].copy()
    if not (cdr.volume > 0).all() or cdr.name.isna().any():
        raise ValueError("Unverified CDR trading status")
    cdr["trade_status"] = 1
    cdr["is_st"] = cdr.name.str.contains("ST").astype(int)
    quotes = pd.concat([quotes, cdr[quotes.columns.intersection(cdr.columns)]], ignore_index=True)
    result = quotes.merge(facts, on=keys, validate="one_to_one", how="outer", suffixes=("", "_check"))
    if not result.trade_status.isin([0, 1]).all() or not result.is_st.isin([0, 1]).all():
        raise ValueError("Missing trading or ST status")
    return result.sort_values(["stock_code", "date"]).reset_index(drop=True)


def execution_tables(panel, cfg, dividends, days, before, after):
    p = panel.copy()
    expected = pd.MultiIndex.from_product([days, sorted(before | after)], names=["date", "stock_code"])
    actual = pd.MultiIndex.from_frame(p[["date", "stock_code"]])
    if not expected.equals(actual.sort_values()):
        raise ValueError(f"Window key mismatch: missing={list(expected.difference(actual)[:10])}, "
                         f"unexpected={list(actual.difference(expected)[:10])}, "
                         f"duplicates={list(actual[actual.duplicated()][:10])}")
    p["tradable"] = p.trade_status.eq(1)
    for key in ("open", "high", "low", "close", "preclose"):
        if not np.isfinite(p[key]).all() or not p[key].gt(0).all():
            raise ValueError(f"Invalid raw {key}")
    # Validate the reference quote against independently sourced raw bars.
    p["raw_close_difference"] = p.close.sub(p.close_check).abs()
    if p.loc[p.tradable, "close_check"].isna().any():
        raise ValueError("Missing independent traded close")
    p.loc[~p.tradable, ["amount", "volume"]] = 0
    if p[["amount", "volume"]].isna().any().any() or not (p.loc[p.tradable, "volume"] > 0).all():
        raise ValueError("Missing turnover or inconsistent trade status")
    p["prior_close"] = p.groupby("stock_code").close.shift()
    p = p.loc[p.date >= cfg["start"]].copy()
    # The pilot consists entirely of established listings. Verify a full prior week of bars;
    # the pilot rule is restricted to the post-registration-reform 2023/24 window.
    p["is_st"] = p.is_st.astype(bool)
    growth = p.stock_code.str.startswith(("30", "68"))
    if not (growth | p.stock_code.str.startswith(("00", "60"))).all():
        raise ValueError("Pilot contains an unsupported board")
    bp = np.where(growth, 20, np.where(p.is_st, 5, 10))
    cents = (p.preclose * 100).round().astype("int64")
    p["limit_up"] = ((cents * (100 + bp) + 50) // 100) / 100
    p["limit_down"] = ((cents * (100 - bp) + 50) // 100) / 100
    p["limit_exempt"] = False
    if (p.low.lt(p.limit_down) | p.high.gt(p.limit_up)).any():
        raise ValueError("Unexplained price outside the pilot's regular-session band")
    p["adj_return"] = p.close / p.preclose - 1  # exchange ex-rights reference, decimal
    p["action"] = "none"
    p["missing_reason"] = ""
    p["cash_per_share"] = np.nan
    p["pay_date"] = pd.Series(pd.NaT, index=p.index, dtype="datetime64[ns]")
    dividends = dividends.loc[dividends.progress.eq("实施")].copy()
    dividends["date"] = pd.to_datetime(dividends.ex_date)
    relevant = dividends[dividends.stock_code.isin(before | after) & dividends.date.isin(p.date)]
    p = p.merge(
        relevant[["date", "stock_code", "cash_dividend", "bonus_shares", "transfer_shares"]],
        how="left",
        on=["date", "stock_code"],
        validate="one_to_one",
        indicator="event_join",
    )
    event = p.event_join.eq("both")
    changed_reference = p.preclose.sub(p.prior_close).abs().gt(0.005)
    p.loc[changed_reference & ~event, ["action", "missing_reason"]] = [
        "unknown",
        "unexplained_reference_change",
    ]
    p.loc[event, ["action", "missing_reason"]] = ["unknown", "cash_payment_notice_missing"]
    shares = event & (p.bonus_shares.gt(0) | p.transfer_shares.gt(0))
    p.loc[shares, ["action", "missing_reason"]] = ["ex", "share_distribution_unsupported"]
    for notice in cfg["cash_notices"]:
        row = p.date.eq(notice["ex_date"]) & p.stock_code.eq(notice["stock_code"])
        if (
            row.sum() != 1
            or not event[row].all()
            or shares[row].any()
            or not np.isclose(p.loc[row, "cash_dividend"].item() / 10, notice["cash_per_share"])
        ):
            raise ValueError(f"Notice disagrees with dividend register: {notice}")
        p.loc[row, ["action", "missing_reason", "cash_per_share"]] = ["cash", "", notice["cash_per_share"]]
        p.loc[row, "pay_date"] = pd.Timestamp(notice["pay_date"])
    p["is_member"] = np.where(
        p.date.lt(cfg["membership"]["effective"]), p.stock_code.isin(before), p.stock_code.isin(after)
    )
    p["vendor_member"] = p.member.eq("Y")
    p["pe_ttm"] = p.market_cap.div(p.profit_ttm.where(p.profit_ttm.ne(0)))
    p["pb_mrq"] = p.market_cap.div(p.equity.where(p.equity.ne(0)))
    p["ps_ttm"] = np.nan
    p["turn"] = p.turnover
    return p.sort_values(["date", "stock_code"])


def build(parent, dest, quote_repo, stock_repo, benchmark_repo, acquired):
    cfg = json.loads(CONFIG.read_bytes())
    pin = CONFIG.with_name(cfg["parent"] + ".json")
    if checksum(parent / "manifest.json") != checksum(pin):
        raise ValueError("Wrong pilot parent manifest")
    specs = {s["name"]: s for s in read_manifest(pin)["files"]}
    for name in ("csi500.tsv", "calendar.txt", "dividends.parquet"):
        if not verified(parent / name, specs[name]):
            raise ValueError(f"Invalid parent input: {name}")
    cal = pd.DatetimeIndex(pd.read_csv(parent / "calendar.txt", header=None, parse_dates=[0])[0])
    days = cal[cal.get_loc(cfg["start"]) - 6 : cal.get_loc(cfg["end"]) + 1]
    m = pd.read_csv(parent / "csi500.tsv", sep="\t", header=None, names=["code", "lo", "hi"])

    def roster(day):
        return set(m.loc[m.lo.le(day) & m.hi.ge(day)].code.str[2:])

    event = cfg["membership"]
    before, after = roster(event["before"]), roster(event["after"])
    if len(before) != 500 or len(after) != 500 or len(before - after) != event["replacements"]:
        raise ValueError("Unexpected CSI500 rosters")
    hashes = {}
    panel = source_panel(cfg, days, before | after, quote_repo, stock_repo, hashes)
    with duckdb.connect() as db:
        dividends = db.read_parquet(str(parent / "dividends.parquet")).df()
    p = execution_tables(panel, cfg, dividends, days, before, after)
    keys = ["date", "stock_code"]
    tables = {
        "daily": keys + ["open", "high", "low", "close", "volume", "amount"],
        "valuation": keys + ["turn", "pe_ttm", "pb_mrq", "ps_ttm", "is_st", "industry", "market_cap"],
        "execution": keys
        + [
            "adj_return",
            "tradable",
            "action",
            "limit_down",
            "limit_up",
            "limit_exempt",
            "cash_per_share",
            "pay_date",
            "missing_reason",
            "preclose",
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        source = "; ".join(
            f"https://github.com/{cfg[k]['repo']}/tree/{cfg[k]['commit']}"
            for k in ("quotes", "stock_files")
        )
        files = []
        for name, columns in tables.items():
            file = stage / (name + ".parquet")
            write_parquet(p[columns], file)
            files.append(file_spec(file, file.name, name, source, acquired))
        disclosure_file = CONFIG.with_name("pilot-disclosures.json")
        file = stage / "statement_facts.parquet"
        prepare_statements(disclosure_file, file)
        files.append(
            file_spec(
                file,
                file.name,
                "statement_facts",
                "Issuer filings in pilot-disclosures.json",
                acquired,
                input_sha256=checksum(disclosure_file),
            )
        )
        bench = cfg["benchmark"]
        raw = git_file(benchmark_repo, bench, bench["path"], hashes)
        data = json.loads(raw)
        if data["meta"]["id"] != "csi500-total-return" or bench["code"] not in data["meta"]["description"]:
            raise ValueError("Wrong total-return benchmark")
        levels = pd.DataFrame(data["series"])
        levels["date"] = pd.to_datetime(levels.date)
        levels = levels.set_index("date").value.reindex(days)
        if not np.isfinite(levels).all() or not levels.gt(0).all():
            raise ValueError("Incomplete benchmark levels")
        index = (
            pd.DataFrame(
                {
                    "close": levels,
                    "return": levels.pct_change(fill_method=None),
                    "index_code": bench["code"],
                }
            )
            .loc[cfg["start"] :]
            .rename_axis("date")
            .reset_index()
        )
        file = stage / "benchmark.parquet"
        with duckdb.connect() as db:
            db.register("levels", index)
            db.execute(f"COPY (SELECT * FROM levels ORDER BY date) TO {literal(file)} (FORMAT PARQUET)")
        files.append(
            file_spec(
                file,
                file.name,
                "benchmark",
                f"https://github.com/{bench['repo']}/blob/{bench['commit']}/{bench['path']}",
                acquired,
            )
        )
        intervals = []
        for code in sorted(before | after):
            lo = cfg["start"] if code in before else event["effective"]
            hi = cfg["end"] if code in after else event["before"]
            intervals.append((("SH" if code.startswith("6") else "SZ") + code, lo, hi))
        file = stage / "csi500.tsv"
        pd.DataFrame(intervals).to_csv(file, sep="\t", index=False, header=False)
        files.append(file_spec(file, file.name, "csi500_intervals", event["notice"], acquired))
        daily = (
            p.assign(
                missing_pe=p.pe_ttm.isna(),
                unknown=p.action.isin(["unknown", "ex"]),
                membership_disagreement=p.exchange_code.notna() & p.vendor_member.ne(p.is_member),
                raw_close_disagreement=p.raw_close_difference.gt(0.005),
            )
            .groupby("date")
            .agg(
                received=("stock_code", "size"),
                unique_codes=("stock_code", "nunique"),
                members=("is_member", "sum"),
                suspended=("tradable", lambda x: (~x).sum()),
                unresolved_actions=("unknown", "sum"),
                missing_pe=("missing_pe", "sum"),
                membership_flags_disagree=("membership_disagreement", "sum"),
                raw_close_disagreements=("raw_close_disagreement", "sum"),
            )
        )
        daily["expected"] = len(before | after)
        daily["duplicate_keys"] = daily.received - daily.unique_codes
        if daily.membership_flags_disagree.any():
            raise ValueError(
                "Independent dated membership flags disagree with the effective-date reconstruction"
            )
        audit = stage / "audit.zip"
        p.loc[p.pe_ttm.isna() & p.missing_reason.eq(""), "missing_reason"] = (
            "missing_trailing_earnings_or_cap"
        )
        issues = p[p.action.isin(["unknown", "ex"]) | p.pe_ttm.isna() | p.raw_close_difference.gt(0.005)]
        with ZipFile(audit, "w") as out:
            for name, payload in {
                "config.json": CONFIG.read_bytes(),
                "source-sha256.json": manifest_bytes(hashes),
                "coverage.csv": daily.to_csv().encode(),
                "gaps.csv": issues[keys + ["missing_reason", "pe_ttm", "raw_close_difference"]]
                .to_csv(index=False)
                .encode(),
            }.items():
                out.writestr(ZipInfo(name), payload, compress_type=ZIP_DEFLATED)
        name = f"reference-{checksum(audit)}-execution-audit.zip"
        audit.rename(stage / name)
        files.append(file_spec(stage / name, name, "reference_raw", source, acquired))
        changes = stage / "inputs-manifest.json"
        write_once(changes, manifest_bytes({"snapshot": "execution-pilot-inputs", "files": files}))
        return compose(parent, dest, replacements=changes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("parent", "dest", "quotes_repo", "stock_repo", "benchmark_repo"):
        parser.add_argument(arg, type=Path)
    parser.add_argument("--collected-at", required=True)
    a = parser.parse_args()
    print(build(a.parent, a.dest, a.quotes_repo, a.stock_repo, a.benchmark_repo, a.collected_at))


if __name__ == "__main__":
    main()
