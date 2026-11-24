"""Reproduce the frozen pilot with pandas/numpy, using FactorLab's offline lot matcher."""

import hashlib
import json
import csv
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import numpy as np
import pandas as pd

from factorlab.backtest.engine import Book, match
from factorlab.backtest.data import Day, Order
from .collect import CACHE, LOCAL, REV, membership, universe, write

D = Decimal
NAMES = ("reversal", "momentum", "lowvol", "value", "blend", "control")
OUT = CACHE / "results"
INITIAL = D("1000000")
BASE_COSTS = dict(commission_bps=D("2.5"), min_commission=D(5), participation=D(".01"))
CENT = D(".01")


def dec(value):
    return D(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def filehash(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def exchange_code(code):
    return code[2:] + "." + code[:2]


def dataset():
    market, tape, panels, audit, files, repairs = {}, {}, [], [], {}, []
    members = membership()
    codes = universe()
    cal_source = LOCAL / "sh000001.csv"
    dates = pd.read_csv(cal_source, usecols=["date"]).date
    dates = list(dates[dates.between("2021-08-01", "2024-12-31")])
    files[str(cal_source)] = filehash(cal_source)
    files[str(CACHE / "members.tsv")] = filehash(CACHE / "members.tsv")
    supplement = CACHE / "action_crosscheck.json"
    crosscheck = json.loads(supplement.read_bytes())
    files[str(supplement)] = filehash(supplement)
    share_terms = {}
    listing_path = Path(__file__).with_name("share_listings.json")
    listings = json.loads(listing_path.read_bytes())
    files[str(listing_path)] = filehash(listing_path)
    for code in codes:
        source = LOCAL / (code.lower() + ".csv")
        files[str(source)] = filehash(source)
        d = pd.read_csv(source, low_memory=False).set_index("date")
        if d.index.has_duplicates:
            raise ValueError(f"duplicate daily rows {code}")
        d = d.reindex(dates)
        minutes = {}
        effective = d.factor.copy()
        for year in (2022, 2023, 2024):
            path = CACHE / "raw" / str(year) / (code + ".csv")
            if not path.exists():
                continue
            files[str(path)] = filehash(path)
            m = pd.read_csv(path).rename(
                columns={
                    "日期": "day",
                    "时间": "time",
                    "开盘": "open",
                    "最高": "high",
                    "最低": "low",
                    "收盘": "close",
                    "成交量": "volume",
                }
            )
            if m.duplicated(["day", "time"]).any() or not m.time.isin(["09:35"]).any():
                raise ValueError(f"invalid timestamp convention {path}")
            m = m.sort_values(["day", "time"])
            minutes[year] = m
            close = m[m.time == "15:00"].set_index("day").close
            implied = d.close / close
            agg = m.groupby("day").agg(
                open=("open", "first"), high=("high", "max"), low=("low", "min"), volume=("volume", "sum")
            )
            wrong = ((d.close / d.factor - close).abs() > 0.011) & d.tradestatus.eq(1)
            verified = (agg.volume * 100 / d.volume - 1).abs().le(0.02)
            for col in ("open", "high", "low"):
                verified &= (d[col] / implied - agg[col]).abs().le(0.011)
            for day in wrong[wrong & verified].index:
                repairs.append((code, day, effective[day], implied[day], close[day]))
                effective[day] = implied[day]
        for year in range(2021, 2025):
            path = CACHE / "daily_crosscheck" / str(year) / (code + ".json")
            if not path.exists():
                continue
            files[str(path)] = filehash(path)
            records = [r[:5] for r in json.loads(path.read_bytes())]
            q = (
                pd.DataFrame(records, columns=["date", "open", "close", "high", "low"])
                .set_index("date")
                .astype(float)
            )
            implied = d.close / q.close
            different = ((d.close / effective - q.close).abs() > 0.011) & d.tradestatus.eq(1)
            valid = different.copy()
            for col in ("open", "high", "low"):
                valid &= (d[col] / implied - q[col]).abs().le(0.011)
            for day in valid[valid].index:
                repairs.append((code, day, effective[day], implied[day], q.close[day]))
                effective[day] = implied[day]
        # Daily local file was produced with backward-adjusted prices and a separate multiplicative factor.
        raw = (d.close / effective).round(2)
        ref = (d.preclose / effective).round(2)
        adjusted = d.close.where(d.tradestatus == 1)
        ret = (d.pctChg / 100).mask(d.tradestatus.eq(0) & d.close.eq(d.preclose), 0.0)
        member = pd.Series(False, index=d.index)
        for row in members[members.code == code].itertuples():
            member.loc[row.start : row.end] = True
        # Only the final implementation announcement, from the following session, may trigger avoidance.
        blocked = pd.Series(False, index=d.index)
        ex_dates = set()
        cash_actions = {}
        source = CACHE / "actions" / (code + ".json")
        acts_known = source.exists()
        if acts_known:
            files[str(source)] = filehash(source)
            for row in json.loads(source.read_bytes()) + crosscheck[code]["additions"]:
                ex = str(row.get("EX_DIVIDEND_DATE") or "")[:10]
                notice = str(row.get("NOTICE_DATE") or "")[:10]
                if ex and notice and notice <= ex:
                    blocked.loc[notice:ex] = True
                    ex_dates.add(ex)
                    if row.get("PRETAX_BONUS_RMB") and not row.get("BONUS_IT_RATIO"):
                        cash_actions[ex] = D(str(row["PRETAX_BONUS_RMB"])) * D(".08")
                    if row.get("BONUS_IT_RATIO"):
                        listing = listings.get(code + "/" + ex, {})
                        share_terms[ex, code] = (
                            D(str(row["BONUS_IT_RATIO"])) / 10,
                            D(str(row.get("PRETAX_BONUS_RMB") or 0)) * D(".08"),
                            listing.get("tradable", "9999"),
                        )
        enough = adjusted.rolling(61).count().eq(61)
        factor_event = effective.pct_change(fill_method=None).abs().gt(0.00001)
        permitted = (
            member
            & enough
            & d.isST.eq(0)
            & d.tradestatus.eq(1)
            & acts_known
            & (d.amount.rolling(20).mean() >= 30_000_000)
            & ~blocked
        )
        forward = d.close.shift(-5) / d.close - 1
        same_year = pd.Series(d.index.str[:4], index=d.index)
        forward = forward.where(same_year.eq(same_year.shift(-5)))
        frame = pd.DataFrame(
            dict(
                code=code,
                raw=raw,
                eligible=permitted,
                member=member,
                forward=forward,
                ret=ret,
                reversal=-adjusted.pct_change(5, fill_method=None),
                momentum=adjusted.shift(5) / adjusted.shift(60) - 1,
                lowvol=-ret.rolling(20).std(),
                value=1 / d.peTTM.where(d.peTTM > 0),
            )
        )
        panels.append(frame.rename_axis("date").reset_index())
        for day, row in d.loc["2022":].dropna(subset=["close", "factor", "preclose"]).iterrows():
            if raw[day] <= 0 or ref[day] <= 0:
                continue
            band = D(".2") if code.startswith("SZ30") else D(".05" if row.isST == 1 else ".10")
            base = dec(ref[day])
            info = Day(
                dec(raw[day]),
                dec(base * (1 - band)),
                dec(base * (1 + band)),
                "trading" if row.tradestatus == 1 else "suspended",
                "cash" if day in cash_actions else "ex" if factor_event[day] or day in ex_dates else "none",
                cash_actions.get(day, D(0)),
                {"2022": "2023-01-03", "2023": "2024-01-02", "2024": "2025-01-02"}[day[:4]],
            )
            if info.lower <= info.close <= info.upper:
                market[day, code] = info
        for year, m in minutes.items():
            grouped = m.groupby("day")
            closing = m[m.time == "15:00"].set_index("day").close
            valid_close = (closing - raw).abs().le(0.011)
            valid_vol = (grouped.volume.sum() * 100 / d.volume - 1).abs().le(0.02)
            good = valid_close & valid_vol
            count = 0
            for row in m[m.time.isin(["11:05", "14:05"])].itertuples():
                info = market.get((row.day, code))
                if not good.get(row.day, False) or info is None or info.status != "trading":
                    continue
                px = dec(row.open)
                if not info.lower <= px <= info.upper:
                    continue
                window = "11:00" if row.time == "11:05" else "14:00"
                tape[row.day, window, code] = px, int(round(row.volume * 100))
                count += 1
            audit.append(
                dict(
                    code=code,
                    year=year,
                    source_days=grouped.ngroups,
                    close_volume_pass=int(good.sum()),
                    windows=count,
                )
            )
    index = {}
    for year in (2021, 2022, 2023, 2024):
        p = CACHE / "index" / f"{year}.json"
        files[str(p)] = filehash(p)
        for r in json.loads(p.read_bytes()):
            day = pd.Timestamp(r["tradeDate"]).date().isoformat()
            index[day] = float(r["close"])
    pd.DataFrame(audit).to_csv(OUT / "coverage.csv", index=False)
    pd.DataFrame(repairs, columns=["code", "date", "original_factor", "verified_factor", "raw_close"]).to_csv(
        OUT / "price_repairs.csv", index=False
    )
    with (OUT / "daily.csv").open("w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(
            [
                "date",
                "symbol",
                "close",
                "limit_down",
                "limit_up",
                "status",
                "action",
                "cash_per_share",
                "pay_date",
            ]
        )
        for (day, code), v in sorted(market.items()):
            # This ledger deliberately reserves dividends through the tested year.
            writer.writerow(
                [
                    day,
                    exchange_code(code),
                    v.close,
                    v.lower,
                    v.upper,
                    v.status,
                    v.action,
                    v.cash_per_share if v.action == "cash" else "",
                    v.pay_date if v.action == "cash" else "",
                ]
            )
    with (OUT / "bars.csv").open("w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(["date", "end", "symbol", "open", "volume"])
        for (day, window, code), (px, vol) in sorted(tape.items()):
            writer.writerow([day, window[:3] + "05", exchange_code(code), px, vol])
    write(OUT / "sources.json", json.dumps(dict(dataset_revision=REV, sha256=files), indent=2).encode())
    panel = pd.concat(panels, ignore_index=True)
    return dates, market, tape, panel, index, share_terms


def rankings(panel, out=OUT):
    orders = {}
    statistics = []
    diagnostic_rows = []
    for day, group in panel.groupby("date", sort=True):
        g = group[group.eligible].set_index("code").copy()
        outcome = g.pop("forward")
        statistics.append((day, int(group.member.sum()), len(g), int(g.value.isna().sum())))
        # Missing/negative earnings share the lowest rank.
        g["value"] = g.value.fillna(0)
        g["blend"] = (g.value.rank(pct=True) + g.lowvol.rank(pct=True)) / 2
        g["control"] = [int(hashlib.sha256(("control-" + c).encode()).hexdigest()[:12], 16) for c in g.index]
        orders[day] = {
            k: list(g.sort_index().sort_values(k, ascending=False, kind="stable").index) for k in NAMES
        }
        if day >= "2022" and outcome.count() >= 20:
            observed = g.join(outcome.dropna(), how="inner")
            for k in NAMES[:-1]:
                x = observed[k].rank()
                y = observed.forward
                count = max(1, len(y) // 5)
                sorted_y = observed.sort_values(k, kind="stable").forward
                diagnostic_rows.append(
                    (
                        day,
                        k,
                        len(y),
                        x.corr(y.rank()),
                        sorted_y.iloc[-count:].mean() - sorted_y.iloc[:count].mean(),
                    )
                )
    pd.DataFrame(statistics, columns=["date", "sample_members", "eligible", "no_positive_pe"]).to_csv(
        out / "universe.csv", index=False
    )
    pd.DataFrame(
        diagnostic_rows, columns=["date", "strategy", "breadth", "rank_ic", "quintile_spread"]
    ).to_csv(out / "factor_daily.csv", index=False)
    return orders


def run(year, name, bp, dates, market, tape, ranks, raw, bench, share_terms, capital=INITIAL, out=OUT, mergers=None, vwap_from=None):
    book = Book(capital)
    prev_equity = capital
    reserve = D(0)
    locked = []
    daily, orders = [], []
    for i, day in enumerate(dates):
        if i == 0 or not day.startswith(str(year)):
            continue
        prev = dates[i - 1]
        ranking = ranks[prev][name]
        rank = {c: j for j, c in enumerate(ranking)}
        conversions = {}
        for old, new, ratio, release in (mergers or {}).get(day, ()):
            if old in book.holdings:
                old_qty = book.holdings.pop(old)
                qty = int(old_qty * ratio)
                opened = book.opened.pop(old)
                book.opened[new] = min(book.opened.get(new, opened), opened)
                book.holdings[new] += qty
                locked.append((new, qty, release))
                conversions[exchange_code(old)] = [old_qty, exchange_code(new), qty]
        planned = [c for c in book.holdings if rank.get(c, 100000) >= 20]
        planned.sort(key=lambda c: (-rank.get(c, 100000), c))
        credits = {}
        used = set()
        costs = dict(
            BASE_COSTS,
            slippage_bps=D(bp),
            transfer_bps=D(".2" if day < "2022-04-29" else ".1"),
            sell_tax_bps=D(10 if day < "2023-08-28" else 5),
        )
        for c in book.holdings:
            info = market.get((day, c))
            term = share_terms.get((day, c))
            if info is None or (info.action == "ex" and term is None):
                raise ValueError(f"unresolved held security {name} {day} {c} {info}")
            if term:
                ratio, cash, release = term
                qty = int(book.holdings[c] * ratio)
                reserve += dec(book.holdings[c] * cash)
                credits[exchange_code(c)] = qty
                book.holdings[c] += qty
                locked.append((c, qty, release))
            elif info.action == "cash":
                reserve += dec(book.holdings[c] * info.cash_per_share)
        locked = [item for item in locked if item[2] > day]
        book.sellable = book.holdings.copy()
        for c, qty, release in locked:
            book.sellable[c] -= qty

        def submit(code, side, qty, window):
            assert code not in used and len(used) < 10 and qty > 0
            used.add(code)
            order = Order(
                code, side, qty, prev + "T23:59:59+08:00" if side == "sell" else day + "T11:05:00+08:00"
            )
            result = match(book, order, market.get((day, code)), tape.get((day, window, code)), costs, i)
            if vwap_from is not None:
                result["price_basis"] = "five_minute_vwap" if day >= vwap_from else "opening_print"
                if "bar_open" in result:
                    result["reference_price"] = result.pop("bar_open")
            orders.append(
                dict(
                    date=day,
                    window=window,
                    known_at=order.known_at,
                    symbol=exchange_code(code),
                    side=side,
                    shares=qty,
                    **result,
                    cash_after=str(book.cash),
                )
            )

        for c in planned[:5]:
            if book.sellable[c] > 0:
                submit(c, "sell", book.sellable[c], "11:00")
        slots = 10 - len(book.holdings)
        batch = []
        # Submit a single batch. Same-window cash, slots and rejects cannot
        # determine another order at the same timestamp.
        budget = min(prev_equity / 10, book.cash / slots) * D(".97") if slots else D(0)
        for c in ranking:
            if len(batch) >= min(slots, 5):
                break
            if c in book.holdings or c in used:
                continue
            px = raw.at[prev, c]
            qty = int(budget / (dec(px) * 100)) * 100
            if qty <= 0:
                continue
            batch.append((c, qty))
        for c, qty in batch:
            submit(c, "buy", qty, "14:00")
        equity = book.cash + reserve + sum(qty * market[day, c].close for c, qty in book.holdings.items())
        daily.append(
            dict(
                date=day,
                equity=float(equity),
                cash=float(book.cash),
                dividend_reserve=float(reserve),
                ret=float(equity / prev_equity - 1),
                stock_count=len(book.holdings),
                attempted=len(used),
                positions=json.dumps({exchange_code(c): q for c, q in book.holdings.items()}),
                share_credits=json.dumps(credits),
                **({"conversions":json.dumps(conversions)} if mergers else {}),
                pool_return=bench[day][0],
                index_return=bench[day][1],
            )
        )
        prev_equity = equity
    log = pd.DataFrame(orders)
    nav = pd.DataFrame(daily)
    folder = out if capital == INITIAL else out / f"capital-{int(capital)}"
    path = folder / f"{year}-{name}-{bp}bp"
    path.mkdir(parents=True, exist_ok=True)
    log.to_csv(path / "orders.csv", index=False)
    nav.to_csv(path / "nav.csv", index=False)
    levels = np.r_[1.0, (1 + nav.ret).cumprod()]
    pool = (1 + nav.pool_return).prod() - 1
    index = (1 + nav.index_return).prod() - 1
    net = levels[-1] - 1
    prior_stock_weight = ((nav.equity - nav.cash - nav.dividend_reserve) / nav.equity).shift(fill_value=0)
    matched_pool = (1 + nav.pool_return * prior_stock_weight).prod() - 1
    value = pd.to_numeric(log.gross, errors="coerce").sum() if "gross" in log else 0
    return dict(
        year=year,
        strategy=name,
        slippage_bp=bp,
        net=net,
        pool=pool,
        index=index,
        excess_pool=net - pool,
        excess_index=net - index,
        drawdown=max(1 - levels / np.maximum.accumulate(levels)),
        turnover=value / (2 * nav.equity.mean()),
        exposure=((nav.equity - nav.cash - nav.dividend_reserve) / nav.equity).mean(),
        prior_exposure_matched_pool=matched_pool,
        volume_rejects=int((log.reason == "volume_cap").sum()),
        fills=int((log.reason == "filled").sum()),
        attempts=len(log),
        max_codes=int(nav.attempted.max()),
    )


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    dates, market, tape, panel, idx, share_terms = dataset()
    ranks = rankings(panel)
    raw = panel.pivot(index="date", columns="code", values="raw")
    ret = panel.pivot(index="date", columns="code", values="ret")
    bench = {}
    eligible_windows = available_windows = return_gaps = 0
    gaps = []
    for i, day in enumerate(dates):
        if day < "2022":
            continue
        prev = dates[i - 1]
        codes = ranks[prev]["control"]
        r = ret.loc[day, codes]
        return_gaps += int(r.isna().sum())
        if not codes or r.isna().any():
            raise ValueError(f"incomplete same-pool outcome {day} {r[r.isna()].index.tolist()}")
        bench[day] = (r.mean(), idx[day] / idx[prev] - 1)
        for c in codes:
            info = market.get((day, c))
            if info and info.status == "trading":
                eligible_windows += 2
                for w in ("11:00", "14:00"):
                    if (day, w, c) in tape:
                        available_windows += 1
                    else:
                        gaps.append((day, w, c))
    pd.DataFrame(gaps, columns=["date", "window", "code"]).to_csv(
        OUT / "eligible_window_gaps.csv", index=False
    )
    write(
        OUT / "quality.json",
        json.dumps(
            dict(
                sample_codes=len(universe()),
                sessions=len(bench),
                expected_eligible_windows=eligible_windows,
                matched_eligible_windows=available_windows,
                missing_benchmark_returns=return_gaps,
            ),
            indent=2,
        ).encode(),
    )
    for capital in (INITIAL, D(100000)):
        results = []
        for year in (2022, 2023, 2024):
            for bp in (5, 10):
                for name in NAMES:
                    results.append(
                        run(year, name, bp, dates, market, tape, ranks, raw, bench, share_terms, capital)
                    )
        folder = OUT if capital == INITIAL else OUT / f"capital-{int(capital)}"
        pd.DataFrame(results).to_csv(folder / "comparison.csv", index=False)
        print(capital, len(results), "portfolios verified")


if __name__ == "__main__":
    main()
