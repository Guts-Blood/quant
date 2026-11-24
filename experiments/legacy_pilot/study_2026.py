"""Forward-year test of the frozen factor pilot under both original capital assumptions."""
import argparse
import json
from collections import Counter
from decimal import Decimal

import pandas as pd

from .panel_2026 import EXACT_FROM, MERGERS, prepare
from . import study
from .collect import write
from .collect_2026 import DIR, END, PREV

D = Decimal


def audit(folder, capital, name, bp, market, tape, terms):
    nav=pd.read_csv(folder/"nav.csv")
    ledger=pd.read_csv(folder/"orders.csv")
    orders={d:g for d,g in ledger.groupby("date")}
    cash=D(capital)
    reserve=D(0)
    holdings=Counter()
    for n in nav.itertuples():
        day=n.date
        credits=json.loads(n.share_credits)
        frozen={}
        conversions=json.loads(n.conversions)
        for old,new,ratio,release in MERGERS.get(day,()):
            s=study.exchange_code(old)
            if s in holdings:
                q=holdings.pop(s)
                dest=study.exchange_code(new)
                added=int(q*ratio)
                assert conversions[s]==[q,dest,added]
                holdings[dest]+=added
        for entries in MERGERS.values():
            for old,new,ratio,release in entries:
                if day<release:
                    frozen[study.exchange_code(new)]=holdings[study.exchange_code(new)]
        for s,qty in list(holdings.items()):
            c=s[-2:]+s[:6]
            info=market[day,c]
            reserve+=study.dec(qty*info.cash_per_share)
            if s in credits:assert int(qty*terms[day,c][0])==credits[s]
            holdings[s]+=credits.get(s,0)
        available=holdings.copy()
        available.subtract(frozen)
        g=orders.get(day)
        if g is not None:
            assert g.symbol.nunique()<=10 and not g.duplicated(["window","symbol"]).any()
            for o in g.itertuples():
                assert pd.Timestamp(o.known_at)<pd.Timestamp(day+"T"+o.window+"+08:00")
                assert (o.side,o.window) in (("buy","14:00"),("sell","11:00"))
                if o.reason=="filled":
                    c=o.symbol[-2:]+o.symbol[:6]
                    px,volume=tape[day,o.window,c]
                    assert o.shares<=volume*.01 and D(str(o.reference_price))==px
                    gross=D(str(o.fill_price))*o.shares
                    fee=max(D(5),study.dec(gross*D(".00025")))+study.dec(gross*D(".00001"))
                    if o.side=="sell":
                        assert available[o.symbol]>=o.shares
                        fee+=study.dec(gross*D(".0005"))
                        cash+=gross-fee
                        holdings[o.symbol]-=o.shares
                        available[o.symbol]-=o.shares
                    else:
                        cash-=gross+fee
                        holdings[o.symbol]+=o.shares
                    assert D(str(o.fees))==fee
                assert cash==D(str(o.cash_after)) and cash>=0
        holdings=+holdings
        assert dict(holdings)==json.loads(n.positions) and len(holdings)<=10
        assert cash==D(str(n.cash)) and reserve==D(str(n.dividend_reserve))
        val=sum(market[day,s[-2:]+s[:6]].close*q for s,q in holdings.items())
        assert cash+reserve+val==D(str(n.equity))
    return nav


def run_all(dynamic):
    OUT=DIR/("asof_reviewed" if dynamic else "fixed_reviewed")
    OUT.mkdir(parents=True,exist_ok=True)
    dates,market,tapes,panel,terms = prepare(OUT,dynamic)
    ranks = study.rankings(panel,out=OUT)
    raw = panel.pivot(index="date",columns="code",values="raw")
    ret = panel.pivot(index="date",columns="code",values="ret")
    ix = {pd.Timestamp(r["tradeDate"]).date().isoformat():r["close"] for y in (2025,2026)
          for r in json.loads((DIR/f"index_{y}.json").read_bytes())}
    bench,coverage = {},[]
    for prev,day in zip(dates,dates[1:]):
        codes=ranks[prev]["control"]
        r=ret.loc[day,codes]
        assert len(codes)>0 and not r.isna().any()
        bench[day]=r.mean(),ix[day]/ix[prev]-1
        trade=[c for c in codes if (day,c) in market and market[day,c].status=="trading"]
        counts={kind:sum((day,w,c) in tape for c in trade for w in ("11:00","14:00")) for kind,tape in tapes.items()}
        coverage.append(dict(date=day,eligible=len(codes),expected=2*len(trade),**counts))
    pd.DataFrame(coverage).to_csv(OUT/"eligible_windows.csv",index=False)
    results=[]
    short=dates[dates.index(EXACT_FROM)-1:]
    for label,span,divisor in (("ytd",dates,6),("ytd_thin",dates,12),
                              ("exact",short,0),("overlap_proxy",short,6)):
        # After the cutoff five-minute execution is modeled at VWAP. Earlier
        # timestamps use the opening print and preceding half-hour volume.
        tape={key:(p,v//divisor) for key,(p,v) in tapes["30"].items()
              if divisor and (key[0]<EXACT_FROM or label=="overlap_proxy")}
        if label!="overlap_proxy":
            tape.update({key:quote for key,quote in tapes["5"].items() if key[0]>=EXACT_FROM})
        for capital in (100000,1000000):
            for bp in ((5,10) if label in ("ytd","exact") else (5,)):
                for name in study.NAMES:
                    row=study.run(2026,name,bp,span,market,tape,ranks,raw,bench,terms,
                                  capital=D(capital),out=OUT/label,mergers=MERGERS,
                                  vwap_from="9999" if label=="overlap_proxy" else EXACT_FROM)
                    folder=OUT/label if capital==1000000 else OUT/label/f"capital-{capital}"
                    audit(folder/f"2026-{name}-{bp}bp",capital,name,bp,market,tape,terms)
                    results.append(dict(scenario=label,first=span[1],last=span[-1],sessions=len(span)-1,
                                        capital=capital,**row))
        print(label,len(results),"audited portfolios",flush=True)
    pd.DataFrame(results).to_csv(OUT/"comparison.csv",index=False)
    sources=[DIR/"sample_codes.json",DIR/"calendar.csv.gz",DIR/"members_20261123.xls",DIR/"index_2025.json",DIR/"index_2026.json"]
    sources.extend((DIR/"members").glob("*.csv.gz"))
    sources.extend((DIR/"audit_inputs").glob("*"))
    sources.extend((DIR/"source").rglob("*.json.gz"))
    sources.extend((DIR/"daily_tencent").glob("*.json"))
    write(OUT/"sources.json",json.dumps({str(p.relative_to(DIR)):study.filehash(p) for p in sorted(sources)},indent=2).encode())


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--fixed-universe",action="store_true")
    run_all(not parser.parse_args().fixed_universe)
