"""Raw-price inputs for offline two-window A-share simulations (Asia/Shanghai)."""
from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .validation import number, require, symbol

WINDOWS = ("11:00", "14:00")
BAR_ENDS = {"11:05": "11:00", "14:05": "14:00"}
DAILY = "date symbol close limit_down limit_up status action".split()
BARS = "date end symbol open volume".split()
ORDERS = "date window known_at symbol side shares".split()


def rows(path, columns, hashes):
    raw = Path(path).read_bytes()
    hashes[str(Path(path).resolve())] = hashlib.sha256(raw).hexdigest()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    require(reader.fieldnames is not None and set(columns) <= set(reader.fieldnames)
            and len(reader.fieldnames) == len(set(reader.fieldnames)), f"{path} 需要字段 {columns}")
    for row in reader:
        require(None not in row and all(row[col] is not None for col in columns),
                f"{path} 第 {reader.line_num} 行列数不符")
        yield row


def session(value):
    day = date.fromisoformat(value)
    require(day.isoformat() == value and day.weekday() < 5, f"无效交易日期 {value}")
    return value


def stock(value):
    code = symbol(value)
    require(re.fullmatch(r"(?:60[0135]\d{3}\.SH|00[0123]\d{3}\.SZ|30[01]\d{3}\.SZ)", code),
            f"首版支持沪深主板、创业板普通 A 股：{code}")
    return code


def price(value):
    x = number(value, "price")
    require(x > 0 and x == x.quantize(Decimal(".01")), f"原始股价应为正且精确到分：{value}")
    return x


@dataclass(frozen=True)
class Day:
    close: Decimal
    lower: Decimal | None
    upper: Decimal | None
    status: str
    action: str
    cash_per_share: Decimal = Decimal(0)
    pay_date: str = ""


def load_daily(path, hashes):
    market = defaultdict(dict)
    for r in rows(path, DAILY, hashes):
        day, code = session(r["date"]), stock(r["symbol"])
        require(code not in market[day], f"重复日线 {day} {code}")
        lower, upper = (None if r[k] == "unlimited" else price(r[k]) for k in ("limit_down", "limit_up"))
        close = price(r["close"])
        require((lower is None and upper is None) or
                (lower is not None and upper is not None and lower < upper and lower <= close <= upper),
                f"收盘价或涨跌停价异常 {day} {code}")
        require(r["status"] in ("trading", "suspended") and r["action"] in ("none", "cash", "ex", "unknown"),
                f"交易状态或公司行动字段不合法 {day} {code}")
        dividend, pay_date = Decimal(0), ""
        if r["action"] == "cash":
            dividend = number(r.get("cash_per_share"), "cash_per_share")
            pay_date = session(r.get("pay_date", ""))
            require(dividend > 0 and pay_date >= day, f"派现参数不合法 {day} {code}")
        else:
            require(not r.get("cash_per_share") and not r.get("pay_date"), f"派现字段与 action 不符 {day} {code}")
        market[day][code] = Day(close, lower, upper, r["status"], r["action"], dividend, pay_date)
    require(market, "日线输入为空")
    return dict(sorted(market.items()))


def load_bars(path, market, hashes):
    bars = {}
    for r in rows(path, BARS, hashes):
        day, code = session(r["date"]), stock(r["symbol"])
        require(r["end"] in BAR_ENDS, f"期末时间应为 11:05 或 14:05：{r['end']}")
        key = (day, BAR_ENDS[r["end"]], code)
        require(key not in bars and code in market.get(day, {}), f"窗口重复或无对应日线 {key}")
        info = market[day][code]
        p, volume = price(r["open"]), int(r["volume"])
        require(info.status == "trading" and volume >= 0 and
                (info.lower is None or info.lower <= p <= info.upper), f"窗口价格、量或状态不符 {key}")
        bars[key] = p, volume
    return bars


@dataclass(frozen=True)
class Order:
    code: str
    side: str
    shares: int
    known_at: str


def load_orders(path, market, hashes):
    orders = defaultdict(list)
    seen = set()
    for r in rows(path, ORDERS, hashes):
        day, w, code = session(r["date"]), r["window"], stock(r["symbol"])
        key = (day, w, code)
        require(day in market and w in WINDOWS and key not in seen, f"委托日期、窗口或重复代码异常 {key}")
        known = datetime.fromisoformat(r["known_at"])
        boundary = datetime.fromisoformat(f"{day}T{w}:00+08:00")
        require(known.utcoffset() is not None and known < boundary, f"信号须在操作窗口前可得 {key}")
        shares = int(r["shares"])
        require(r["side"] in ("buy", "sell") and shares > 0 and shares % 100 == 0,
                f"只接受 buy/sell 及 100 股整手正数 {key}")
        seen.add(key)
        orders[day, w].append(Order(code, r["side"], shares, known.isoformat()))
    return orders
