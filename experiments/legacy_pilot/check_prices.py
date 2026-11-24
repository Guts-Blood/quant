"""Get an independent unadjusted daily series wherever local conversion disagrees with intraday closes."""

import json
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from .collect import CACHE, LOCAL, get, write


def main():
    names = set()
    for path in (CACHE / "raw").glob("*/*.csv"):
        code = path.stem
        if code in names:
            continue
        raw = pd.read_csv(path, usecols=["日期", "时间", "收盘"])
        close = raw[raw["时间"] == "15:00"].set_index("日期")["收盘"]
        d = pd.read_csv(LOCAL / (code.lower() + ".csv"), usecols=["date", "close", "factor"]).set_index(
            "date"
        )
        if ((d.close / d.factor - close).abs() > 0.011).any():
            names.add(code)

    def fetch(pair):
        c, year = pair
        dest = CACHE / "daily_crosscheck" / str(year) / (c + ".json")
        if dest.exists():
            return
        ticker = c.lower()
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={ticker},day,{year}-01-01,{year}-12-31,320,"
        packet = get(url).json()["data"].get(ticker, {})
        data = packet.get("day", [])
        if not all(row[0].startswith(str(year)) for row in data):
            raise ValueError(dest)
        write(dest, json.dumps(data).encode())

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(fetch, [(c, y) for c in names for y in range(2021, 2025)]))
    print("Raw daily cross-check", len(names), "codes", sorted(names))


if __name__ == "__main__":
    main()
