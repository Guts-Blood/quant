"""Daily cross-sectional replication on disjoint names; no assumption about intraday fills."""

import json
import re
import argparse

import pandas as pd

from .collect import LOCAL, membership, universe
from .study import OUT, filehash


def main(forward_check=False):
    members = membership()
    end = "2025-12-31" if forward_check else "2024-12-31"
    start = "2025" if forward_check else "2023"
    m = members[(members.start <= end) & (members.end >= ("2025-01-01" if forward_check else "2021-12-30"))]
    pilot = set() if forward_check else set(universe())
    records = []
    hashes = {}
    dates = pd.read_csv(LOCAL / "sh000001.csv", usecols=["date"]).date
    dates = dates[dates.between("2024-08-01" if forward_check else "2021-08-01", end)]
    for code, intervals in m.groupby("code"):
        p = LOCAL / (code.lower() + ".csv")
        if (
            code in pilot
            or not p.exists()
            or not re.fullmatch(r"SH60[0135]\d{3}|SZ(?:00[0123]|30[01])\d{3}", code)
        ):
            continue
        hashes[code] = filehash(p)
        d = pd.read_csv(p, low_memory=False).set_index("date").reindex(dates)
        member = pd.Series(False, index=d.index)
        for row in intervals.itertuples():
            member.loc[row.start : row.end] = True
        px = d.close.where(d.tradestatus == 1)
        ok = member & px.rolling(61).count().eq(61) & d.isST.eq(0) & d.amount.rolling(20).mean().ge(30e6)
        y = d.close.shift(-5) / d.close - 1
        years = pd.Series(d.index.str[:4], index=d.index)
        r = pd.DataFrame(
            dict(
                code=code,
                forward=y.where(years == years.shift(-5)),
                value=(1 / d.peTTM.where(d.peTTM > 0)).fillna(0),
                lowvol=-(d.pctChg / 100).rolling(20).std(),
                momentum=px.shift(5) / px.shift(60) - 1,
                reversal=1 - px / px.shift(5),
            )
        )
        records.append(r[ok & (years >= start)].reset_index())
    frame = pd.concat(records)
    out = []
    for date, g in frame.groupby("date"):
        g = g.copy()
        g["blend"] = (g.value.rank(pct=True) + g.lowvol.rank(pct=True)) / 2
        n = len(g)
        g = g.dropna(subset=["forward"])
        if len(g) < 20:
            continue
        for name in ("blend",) if forward_check else ("value", "lowvol", "momentum", "reversal", "blend"):
            sort = g.sort_values([name, "code"])
            k = len(sort) // 5
            out.append(
                (
                    date,
                    name,
                    n,
                    len(sort),
                    sort[name].rank().corr(sort.forward.rank()),
                    sort.forward.iloc[-k:].mean() - sort.forward.iloc[:k].mean(),
                    sort.forward.iloc[-10:].mean() - sort.forward.mean(),
                )
            )
    tab = pd.DataFrame(
        out, columns=["date", "strategy", "eligible", "observed", "ic", "spread", "top10_minus_pool"]
    )
    OUT.mkdir(parents=True, exist_ok=True)
    prefix = "forward_2025" if forward_check else "disjoint"
    tab.to_csv(OUT / f"{prefix}_daily.csv", index=False)
    (OUT / f"{prefix}_sources.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")
    print("independent codes", len(hashes), "unique eligible", frame.code.nunique())
    print(tab.groupby([tab.date.str[:4], "strategy"])[["ic", "spread", "observed"]].mean().round(4))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--forward-2025", action="store_true")
    main(parser.parse_args().forward_2025)
