"""Check exchange delistings and replay invariance using the frozen 2026 inputs."""
import hashlib
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from . import study
from .collect_2026 import DIR, END
from .panel_2026 import EXACT_FROM, HALTS, MERGERS, ST_START, bars, prepare, window_quotes

OUT = DIR / "asof_reviewed"


def exchange_audit():
    inputs = DIR / "audit_inputs"
    sh = json.loads((inputs / "sse_delisted.json").read_bytes())
    assert len(sh["result"]) == sh["pageHelp"]["total"]
    delisted = {"SH"+row["COMPANY_CODE"]:pd.Timestamp(row["DELIST_DATE"]).date().isoformat()
                for row in sh["result"] if row["DELIST_DATE"].isdigit()}
    sz = pd.read_excel(inputs / "szse_delisted.xlsx", dtype=str)
    delisted.update({"SZ"+code:date for code,date in zip(sz["证券代码"],sz["终止上市日期"])})
    assert delisted["SH601198"] == "2026-10-19"
    names = set()
    for f in (DIR/"members").glob("*.csv.gz"):
        names.update(pd.read_csv(f).code.str.replace(".","",regex=False).str.upper())
    ending = pd.read_excel(DIR/"members_20261123.xls",dtype=str).iloc[:,4].str.zfill(6)
    names.update(("SH" if x.startswith("6") else "SZ")+x for x in ending)
    candidates = {c for c in names if re.fullmatch(r"SH60[0135]\d{3}|SZ(?:00[0123]|30[01])\d{3}",c)
                  and int(hashlib.sha256(c.encode()).hexdigest(),16)%3==0}
    sampled = set(json.loads((DIR/"sample_codes.json").read_bytes()))
    assert sampled <= candidates
    registry_hits = {c:delisted[c] for c in names & delisted.keys() if "2026-01-01" <= delisted[c] <= END}
    checks = {}
    for name in ("asof_results","asof_reviewed"):
        count, expired, suspended, locked, st_buys = 0,0,0,0,0
        for path in (DIR/name).rglob("orders.csv"):
            log = pd.read_csv(path)
            for r in log[log.reason.eq("filled")].itertuples():
                c = r.symbol[-2:]+r.symbol[:6]
                first,last = HALTS.get(c,("9999","9999"))
                count += 1
                expired += r.date >= delisted.get(c,"9999")
                suspended += first <= r.date <= last
                locked += c == "SH601995" and r.side=="sell" and r.date < "2026-10-29"
                st_buys += r.side=="buy" and r.date >= ST_START.get(c,"9999")
        assert count and (expired,suspended,locked,st_buys)==(0,0,0,0)
        checks[name] = dict(fills=count,on_or_after_delisting=expired,known_halt=suspended,
                            sale_before_conversion_release=locked,known_ST_buys=st_buys)
    return dict(candidate_hash_count=len(candidates),sample_count=len(sampled),excluded=sorted(candidates-sampled),
                index_constituent_delistings=registry_hits,ledger_checks=checks,
                exchange_snapshot_sha256={p.name:study.filehash(p) for p in sorted(inputs.iterdir()) if p.is_file()})


def replay_checks():
    with TemporaryDirectory() as tmp:
        out=Path(tmp)
        dates,market,tapes,panel,terms=prepare(out,True)
        ranks=study.rankings(panel,out)
        masked=panel.copy()
        masked["forward"]=float("nan")
        assert ranks==study.rankings(masked,out)
        code="SH600038"
        for kind,day,end in (("30","2026-06-16","14:30"),("5","2026-10-19","14:05")):
            df=bars(code,kind)
            partial=df.loc[(df.date==day)&(df.end<=end)].copy()
            if kind=="30":
                partial.loc[partial.end==end,["high","low","close","amount","volume"]]=0
            # Remove the entire rest of the session. For a proxy fill, also
            # erase everything in its current half-hour bar except the opening print.
            got=window_quotes(partial,kind,market,code,{end:"14:00"})
            key=(day,"14:00",code)
            assert got[key]==tapes[kind][key]
        checkpoints=["2026-06-15","2026-10-19"]
        for cut in checkpoints:
            _,marks,quotes,subset,_=prepare(out,True,cut)
            assert study.rankings(subset,out)=={d:r for d,r in ranks.items() if d<=cut}
            allowable=set(subset.code) | {"SH601995"}
            assert marks=={key:v for key,v in market.items() if key[0]<=cut and key[1] in allowable}
            for kind,q in quotes.items():
                assert q=={key:v for key,v in tapes[kind].items() if key[0]<=cut and key[2] in allowable}
        # Same submitted 14:00 list and quantities even when every buy is rejected.
        # Start from actual archived portfolios on three dates, including an initial build.
        cutoffs=[dates[1],"2026-06-16","2026-10-19"]
        raw=panel.pivot(index="date",columns="code",values="raw")
        bench={day:(0.,0.) for day in dates[1:]}
        tape={k:(p,v//6) for k,(p,v) in tapes["30"].items() if k[0]<EXACT_FROM}
        tape.update({k:q for k,q in tapes["5"].items() if k[0]>=EXACT_FROM})
        first=dates[:3]
        kwargs=dict(capital=study.D(100000),out=out,mergers=MERGERS)
        study.run(2026,"value",5,first,market,tape,ranks,raw,bench,terms,**kwargs)
        first_nav=pd.read_csv(out/"capital-100000"/"2026-value-5bp"/"nav.csv")
        symbol=next(iter(json.loads(first_nav.positions.iloc[0])))
        code=symbol[-2:]+symbol[:6]
        broken=dict(market)
        del broken[(first[-1],code)]
        try:
            study.run(2026,"value",5,first,broken,tape,ranks,raw,bench,terms,**kwargs)
        except ValueError as err:
            assert "unresolved held security" in str(err) and code in str(err)
        else:
            raise AssertionError("held security without a mark did not stop replay")
        tested=0
        for cut in cutoffs:
            span=dates[:dates.index(cut)+1]
            for strategy in ("value","blend","reversal"):
                kwargs=dict(capital=study.INITIAL,out=out,mergers=MERGERS)
                study.run(2026,strategy,5,span,market,tape,ranks,raw,bench,terms,**kwargs)
                path=out/f"2026-{strategy}-5bp"/"orders.csv"
                before=pd.read_csv(path)
                blocked={key:q for key,q in tape.items() if key[:2]!=(cut,"14:00")}
                study.run(2026,strategy,5,span,market,blocked,ranks,raw,bench,terms,**kwargs)
                after=pd.read_csv(path)
                cols=["symbol","shares","side","known_at"]
                a=before.loc[(before.date==cut)&(before.window=="14:00"),cols].reset_index(drop=True)
                b=after.loc[(after.date==cut)&(after.window=="14:00"),cols].reset_index(drop=True)
                pd.testing.assert_frame_equal(a,b)
                tested+=len(a)
        assert tested>=5
    return dict(future_return_label_removed="all daily order rankings identical",
                cutoff_replays=checkpoints,intraday_tail_removal="proxy and five-minute fills unchanged",
                unresolved_position="replay stopped when a held security lost its mark",
                afternoon_fill_outcome_perturbations=cutoffs,
                buy_orders_compared=tested)


if __name__=="__main__":
    result={**exchange_audit(),**replay_checks()}
    (OUT/"timing_audit.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k not in ("excluded","exchange_snapshot_sha256")}))
