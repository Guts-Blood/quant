"""Build as-of signals and validated 11:00 / 14:00 execution quotes from archived feeds."""
import json
from decimal import Decimal as D

import pandas as pd

from factorlab.backtest.data import Day
from . import study
from .collect_2026 import DIR, END, PREV
from .quotes_2026 import constituent_history, delisting_dates, rows

# Effective date in the issuer's September 29 ST announcement (1225584110).
ST_START = {"SH600363": "2026-09-30"}
# Tradable dates from the issuers' final distribution notices.
LISTINGS = {("SZ000034","2026-05-19"), ("SZ300458","2026-04-30")}
EXACT_FROM = "2026-09-18"
MERGERS = {"2026-10-19": [("SH601198","SH601995",D(".4376"),"2026-10-29")]}
HALTS = {"SH600363": ("2026-09-29","2026-09-29"), "SH601198": ("2026-09-15","2026-10-16")}


def window_quotes(df, kind, market, code, windows):
    """Use only observations within the permitted time horizon of each fill."""
    by_time = df.set_index(["date","end"])
    quotes = {}
    for bar in df.loc[df.end.isin(windows)].itertuples():
        day,window = bar.date,windows[bar.end]
        info = market.get((day,code))
        if info is None or info.status != "trading":
            continue
        if kind == "30":
            if (day,window) not in by_time.index:
                continue
            # Opening print is observed at the start. Capacity is only an estimate
            # from the already completed half hour, never from the rest of this bar.
            volume = int(by_time.at[(day,window),"volume"])
            price = study.dec(bar.open)
        else:
            if bar.volume <= 0 or bar.amount <= 0:
                continue
            volume = int(bar.volume)
            avg = bar.amount / bar.volume
            if not float(bar.low)-.011 <= avg <= float(bar.high)+.011:
                continue
            price = study.dec(avg)
        if info.lower <= price <= info.upper and volume >= 0:
            quotes[day,window,code] = price,volume
    return quotes


def bars(code, kind):
    df = pd.DataFrame(rows(kind,code))
    assert df.day.is_unique
    df["date"] = df.day.str[:10]
    df["end"] = df.day.str[11:16]
    return df.astype({k:float for k in ("open","close","volume","amount")})


def actions(code, dates):
    blocked = pd.Series(False,index=dates)
    cash,shares = {},{}
    for row in rows("ex",code):
        notice = (row.get("NOTICE_DATE") or "")[:10]
        ex = (row.get("EX_DIVIDEND_DATE") or "")[:10]
        if not (notice and ex and notice <= ex and notice <= dates[-1]):
            continue
        blocked.loc[notice:ex] = True
        cash[ex] = D(str(row.get("PRETAX_BONUS_RMB") or 0)) * D(".08")
        if row.get("BONUS_IT_RATIO") and PREV < ex <= END:
            assert (code,ex) in LISTINGS
            shares[ex,code] = D(str(row["BONUS_IT_RATIO"]))/10, cash[ex], ex
    supplements = {"SH601198": ("2026-07-20","2026-07-28",".09"),
                   "SH601918": ("2026-01-30","2026-02-06",".05")}
    if code in supplements:
        notice,ex,gross = supplements[code]
        blocked.loc[notice:ex] = True
        cash[ex] = D(gross)*D(".8")
    return blocked,cash,shares


def prepare(out, dynamic, through=END):
    calendar = pd.read_csv(DIR/"calendar.csv.gz")
    days = calendar.loc[calendar.is_trading_day.eq(1) & calendar.calendar_date.le(through),"calendar_date"].tolist()
    live = [d for d in days if PREV <= d <= END]
    assert live[0]==PREV and live[-1]==through
    market,terms,panels,tapes = {},{},[],{"5":{},"30":{}}
    checks = []
    history=constituent_history(through)
    delisted=delisting_dates()
    if not dynamic:
        history={d:history[PREV] for d in live}
    codes=sorted(set.union(*history.values()))
    for code in [*codes,"SH601995"]:
        daily = pd.DataFrame(rows("240",code)).set_index("day").astype({"close":float,"volume":float})
        daily = daily.loc[daily.index <= through]
        val = pd.DataFrame(rows("valuation",code))
        val.index = val.TRADE_DATE.str[:10]
        val = val.loc[val.index <= through]
        assert daily.index.is_unique and val.index.is_unique
        assert (daily.close-val.CLOSE_PRICE).dropna().abs().max() <= .011
        if val.SECURITY_NAME_ABBR.str.contains("ST").any():
            assert code in ST_START
        if code not in ("SH601995","SH603083"):
            tencent = json.loads((DIR/"daily_tencent"/(code+".json")).read_bytes())
            cross = pd.Series({r[0]:float(r[2]) for r in tencent})
        else:
            cross = daily.close
        trading = daily.volume.reindex(days).fillna(0).gt(0)
        close = val.CLOSE_PRICE.reindex(days)
        ret = val.CHANGE_RATE.reindex(days)/100
        # The last pre-merger mark exists through Oct 16. Missing marks after withdrawal
        # remain missing and the ledger stops if an unresolved position is held.
        assert not (trading & close.isna()).any()
        ret = ret.where(trading,0.0)
        adjusted = (1+ret).cumprod().where(trading)
        raw30 = bars(code,"30")
        raw30 = raw30.loc[raw30.date <= through]
        market_amount = raw30.groupby("date").amount.sum().reindex(days)
        assert market_amount.loc[:PREV].count()>=20
        blocked,cash,shares = actions(code,days)
        terms.update(shares)
        st = pd.Series([day>=ST_START.get(code,"9999") for day in days],index=days)
        eligible = trading & adjusted.rolling(61).count().eq(61) & ~st & ~blocked & market_amount.rolling(20).mean().ge(30e6)
        pe = val.PE_TTM.reindex(days)
        member=pd.Series([code in history.get(d,set()) for d in days],index=days)
        panel = pd.DataFrame(dict(code=code,raw=close,eligible=eligible & member,member=member,ret=ret,
                             forward=adjusted.shift(-5)/adjusted-1, reversal=-adjusted.pct_change(5,fill_method=None),
                             momentum=adjusted.shift(5)/adjusted.shift(60)-1,lowvol=-ret.rolling(20).std(),
                             value=1/pe.where(pe>0)))
        if code in codes:
            panels.append(panel.loc[PREV:].rename_axis("date").reset_index())
        for day in live[1:]:
            if code == "SH601995" and day < delisted["SH601198"]:
                continue  # Held only as a successor, never selected as a candidate.
            if day >= delisted.get(code,"9999"):
                assert not trading[day], (code,day,"trading after delisting")
                continue
            halt_start,halt_end = HALTS.get(code,("9999","9999"))
            suspended = halt_start <= day <= halt_end
            if bool(trading[day]) == suspended:
                raise ValueError(("unexplained session status",code,day))
            if pd.isna(close[day]):
                raise ValueError(("missing closing mark",code,day))
            px = study.dec(close[day])
            prev = study.dec(close[days[days.index(day)-1]])
            gross_dividend = cash.get(day,D(0)) / D(".8")
            stock_ratio = shares.get((day,code),(D(0),))[0]
            base = study.dec((prev-gross_dividend)/(1+stock_ratio))
            band = D(".20") if code.startswith("SZ30") else D(".10")
            action = "ex" if (day,code) in shares else "cash" if cash.get(day) else "none"
            info = Day(px,study.dec(base*(1-band)),study.dec(base*(1+band)),
                       "suspended" if suspended else "trading",action,cash.get(day,D(0)),"2027-01-04")
            assert info.lower<=px<=info.upper, (day,code,info)
            # End-of-day consistency check. It does not control any individual fill.
            reference = study.dec(close[day]/(1+ret[day]))
            if abs(base-reference) > (D(".01") if day in cash else 0):
                raise ValueError(("unexplained reference price",code,day,base,reference))
            market[day,code] = info
        for kind, df, times in (("30",raw30,{"11:30":"11:00","14:30":"14:00"}),
                                ("5",bars(code,"5"),{"11:05":"11:00","14:05":"14:00"})):
            df = df.loc[df.date <= through]
            volumes=df.groupby("date").volume.sum()
            eod=df.loc[df.end=="15:00"].set_index("date").close
            good=((volumes/daily.volume-1).abs().le(.02) & (eod-close).abs().le(.011)
                  & (cross-close).abs().le(.011) & df.groupby("date").size().eq(8 if kind=="30" else 48))
            tapes[kind].update(window_quotes(df,kind,market,code,times))
            checks.append(dict(code=code,scale=int(kind),first=df.date.min(),last=df.date.max(),
                               verified_2026_days=sum(bool(good.get(d,False)) for d in live[1:])))
    pd.DataFrame(checks).to_csv(out/"coverage.csv",index=False)
    paired=[]
    for key,(px,volume) in tapes["5"].items():
        if key in tapes["30"]:
            px30,vol30=tapes["30"][key]
            paired.append((*key,px,px30,volume,vol30))
    pd.DataFrame(paired,columns=["date","window","code","five_min_vwap","open","vol5","preceding_vol30"]).to_csv(out/"overlap.csv.gz",index=False)
    return live,market,tapes,pd.concat(panels),terms
