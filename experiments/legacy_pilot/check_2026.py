"""Independent unadjusted daily closes for the 2026 price audit."""
import json
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from .collect import get, write
from .collect_2026 import DIR, END
from .quotes_2026 import constituents


def collect(codes):
    def fetch(code):
        p = DIR / "daily_tencent" / (code+".json")
        if p.exists():
            return
        symbol=code.lower()
        url=f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,2026-01-01,{END},320,"
        rows=get(url).json()["data"][symbol]["day"]
        if not rows or not all("2026-01-01"<=r[0]<=END for r in rows):
            raise ValueError(code)
        write(p,json.dumps(rows).encode())
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(fetch,sorted(codes)))


def main():
    codes=set(constituents())-{"SH603083"}
    collect(codes)
    print("Independent close histories",len(codes))


if __name__=="__main__":
    main()
