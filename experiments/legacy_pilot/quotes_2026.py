"""Archive independent intraday, daily valuation and ex-dividend data for the 2026 replay."""
import gzip
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from .collect import get, write
from .collect_2026 import DIR, END, PREV, START

SINA = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
EAST = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def delisting_dates():
    folder=DIR/"audit_inputs"
    sh=json.loads((folder/"sse_delisted.json").read_bytes())
    assert len(sh["result"])==sh["pageHelp"]["total"]
    dates={"SH"+r["COMPANY_CODE"]:pd.to_datetime(r["DELIST_DATE"],format="%Y%m%d").date().isoformat()
           for r in sh["result"] if r["DELIST_DATE"].isdigit()}
    sz=pd.read_excel(folder/"szse_delisted.xlsx",dtype=str)
    dates.update({"SZ"+r["证券代码"]:r["终止上市日期"] for _,r in sz.iterrows()})
    return dates


def constituent_history(through=END):
    calendar=pd.read_csv(DIR/"calendar.csv.gz")
    days=calendar.loc[calendar.is_trading_day.eq(1) & calendar.calendar_date.between(PREV,through),"calendar_date"]
    removals={"2026-10-12": ("sh.600363","sz.301297"),"2026-10-19": ("sh.601198","sh.603083")}
    history={}
    names=set()
    # Frozen intersection with local Qlib coverage and the original SHA-256 split.
    sample=set(json.loads((DIR/"sample_codes.json").read_bytes()))
    for day in days:
        if day<="2026-08-05":
            data=pd.read_csv(DIR/"members"/(day+".csv.gz"))
            assert len(data)==500 and data.updateDate.max()<=day
            names=set(data.code)
        elif day in removals:
            old,new=removals[day]
            names.remove(old)
            names.add(new)
        assert len(names)==500
        history[day]={c.replace(".","").upper() for c in names} & sample
    if through == END:
        endfile=DIR/"members_20261123.xls"
        ending=pd.read_excel(endfile,dtype=str)
        assert ending.iloc[:,0].eq(END.replace("-","")).all()
        assert set(ending.iloc[:,4].str.zfill(6))=={c[3:] for c in names}
    return history


def constituents():
    return sorted(set.union(*constituent_history().values()))


def path(kind, code):
    return DIR / "source" / kind / (code + ".json.gz")


def rows(kind, code):
    return json.loads(gzip.decompress(path(kind, code).read_bytes()))


def fetch(kind, code):
    dest = path(kind, code)
    if dest.exists():
        return
    if kind in ("5", "30", "240"):
        data = get(SINA, dict(symbol=code.lower(), scale=kind, ma="no", datalen=370 if kind == "240" else 1970)).json()
        if not data or data[0]["day"][:10] >= END:
            raise ValueError((code, kind))
        data = [r for r in data if START <= r["day"][:10] <= END]
    else:
        params = dict(reportName="RPT_VALUEANALYSIS_DET" if kind == "valuation" else "RPT_SHAREBONUS_DET",
                      columns="ALL", filter=f'(SECURITY_CODE="{code[2:]}")', pageSize=500, source="WEB", client="WEB")
        if kind == "valuation":
            params["filter"] += f"(TRADE_DATE>='{START}')(TRADE_DATE<='{END}')"
        resp = get(EAST, params).json()
        if resp.get("result"):
            assert resp["result"]["pages"] <= 1
            data = resp["result"]["data"]
        elif kind == "ex" and resp.get("code") == 9201:
            data = []
        else:
            raise ValueError((kind,code,resp.get("message")))
    write(dest, gzip.compress(json.dumps(data,ensure_ascii=False).encode(),mtime=0))


def main():
    jobs = [(kind,c) for c in [*constituents(),"SH601995"] for kind in ("30","5","240","valuation","ex")]
    failed=[]
    with ThreadPoolExecutor(max_workers=5) as pool:
        tasks = {pool.submit(fetch,*job):job for job in jobs}
        for i,task in enumerate(as_completed(tasks),1):
            try: task.result()
            except Exception as exc: failed.append((tasks[task],repr(exc)[:180]))
            if i%100==0: print(i,len(jobs),"errors",failed[:2],flush=True)
    if failed: raise ValueError(failed)

if __name__ == "__main__":
    main()
