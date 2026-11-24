"""Fetch and verify recent five-minute window data for legacy replay."""
import csv
import io
import json
import os
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

from ..backtest.data import BAR_ENDS, BARS, load_daily, price
from ..backtest.validation import require

SOURCE = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"


def fetch_bars(daily, out):
    """Extract only the requested sessions; refuse incomplete rolling histories."""
    market = load_daily(daily, {})
    records = []
    codes = sorted({code for symbols in market.values() for code in symbols})
    for code in codes:
        number_part, exchange = code.lower().split(".")
        url = f"{SOURCE}?symbol={exchange}{number_part}&scale=5&ma=no&datalen=1023"
        with urlopen(Request(url, headers={"User-Agent": "factorlab/0.2"}), timeout=15) as response:
            payload = json.load(response)
        require(isinstance(payload, list), f"{code} 行情源未返回分钟线")
        candles = {}
        for bar in payload:
            stamp = bar["day"]
            require(stamp not in candles, f"重复行情 {code} {stamp}")
            candles[stamp] = bar
        for day, symbols in market.items():
            info = symbols.get(code)
            if info is None or info.status == "suspended":
                continue
            final = candles.get(f"{day} 15:00:00")
            require(final and price(final["close"]) == info.close, f"{code} {day} 缺完整收盘或日线收盘不符")
            for end in BAR_ENDS:
                candle = candles.get(f"{day} {end}:00")
                require(candle is not None, f"{code} {day} {end} 缺窗口，需历史分钟数据")
                p, vol = price(candle["open"]), int(candle["volume"])
                require(vol >= 0 and (info.lower is None or info.lower <= p <= info.upper),
                        f"{code} {day} 分钟行情与日线不符")
                records.append((day, end, code, p, vol))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Prepare in memory; an existing historical snapshot is never replaced.
    contents = io.StringIO(newline="")
    writer = csv.writer(contents)
    writer.writerow(BARS)
    writer.writerows(sorted(records))
    with tempfile.TemporaryDirectory(dir=out.parent) as staging:
        staged = Path(staging) / "bars.csv"
        staged.write_text(contents.getvalue(), encoding="utf-8", newline="")
        os.link(staged, out)
    return {"bars": len(records), "symbols": len(codes), "path": str(out.resolve()), "source": SOURCE}
