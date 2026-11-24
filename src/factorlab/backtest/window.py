"""Reproducible legacy 11:00/14:00 replay, isolated from factor definitions."""
from __future__ import annotations

import argparse
from collections import Counter, deque
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from .data import WINDOWS, Day, Order, load_bars, load_daily, load_orders, rows
from .engine import Book, match, money, CENT, BPS
from .validation import number, require
from ..artifacts import write_json

D = Decimal


def add_parser(sub):
    p = sub.add_parser("backtest", help="独立的沪深股票历史回测")
    actions = p.add_subparsers(dest="action", required=True)
    fetch = actions.add_parser("fetch-bars", help="用近期新浪五分钟线补齐给定日线的两个操作窗口")
    fetch.add_argument("--daily", required=True)
    fetch.add_argument("--out", required=True, help="新 CSV 路径")
    run = actions.add_parser("run", help="按固定窗口回放因子或带时间戳的委托")
    run.add_argument("--daily", required=True)
    run.add_argument("--bars", required=True)
    run.add_argument("--benchmark", help="可选，全收益基准 CSV：date,level，含首日前一个交易日")
    choice = run.add_mutually_exclusive_group(required=True)
    choice.add_argument("--orders", help="预先形成的逐窗口委托 CSV")
    choice.add_argument("--strategy", choices=["reversal5"], help="演示因子，每日最多新开一手")
    run.add_argument("--cash", default="100000")
    run.add_argument("--max-symbols", type=int, default=10, choices=range(1, 11))
    run.add_argument("--commission-bps", default="2.5")
    run.add_argument("--min-commission", default="5")
    run.add_argument("--sell-tax-bps", default="5")
    run.add_argument("--transfer-bps", default="0.1")
    run.add_argument("--slippage-bps", default="5")
    run.add_argument("--participation", default="0.01", help="每笔最多占随后五分钟成交量的比例")
    run.add_argument("--out", default="outputs/backtests", help="每次回放在此生成独立目录")


def parser():
    result = argparse.ArgumentParser(prog="factorlab")
    add_parser(result.add_subparsers(dest="command", required=True))
    return result


def reversal_orders(window, book, history, day_index, day, previous_day, used):
    if previous_day is None:
        return []
    known = f"{previous_day}T15:00:00+08:00"
    if window == "11:00":
        return [Order(code, "sell", book.holdings[code], known)
                for code, opened in sorted(book.opened.items()) if day_index - opened >= 5]
    candidates = [(closes[-1] / closes[0], code) for code, closes in history.items()
                  if len(closes) == 6 and code not in book.holdings and code not in used]
    return [Order(min(candidates)[1], "buy", 100, f"{day}T11:05:00+08:00")] if candidates else []


def replay(args):
    hashes = {}
    market = load_daily(args.daily, hashes)
    bars = load_bars(args.bars, market, hashes)
    orders = load_orders(args.orders, market, hashes) if args.orders else {}
    initial = number(args.cash, "cash", CENT)
    require(initial == money(initial), "初始资金最多两位小数")
    costs = {name: number(getattr(args, name), name, D(0)) for name in
             ("commission_bps", "min_commission", "sell_tax_bps", "transfer_bps", "slippage_bps", "participation")}
    require(0 < costs["participation"] <= 1 and costs["slippage_bps"] < BPS, "成交量比例或滑点不合法")
    book = Book(initial)
    ledger, nav, history = [], [], {}
    previous_day = None
    peak, drawdown, total_fees, total_slippage, turnover = initial, D(0), D(0), D(0), D(0)
    for index, (day, symbols) in enumerate(market.items()):
        accrued = D(0)
        for code in book.holdings:
            require(code in symbols, f"{day} 持仓 {code} 缺收盘估值，补齐日线")
            info = symbols[code]
            require(info.action not in ("unknown", "ex"), f"{day} 持仓 {code} 公司行动待处理，无法生成可靠净值")
            if info.action == "cash":
                value = money(book.holdings[code] * info.cash_per_share)
                accrued += value
                book.receivables[info.pay_date] += value
        paid = sum((book.receivables.pop(due) for due in sorted(book.receivables) if due <= day), D(0))
        book.cash += paid
        book.sellable = book.holdings.copy()
        used = set()
        for window in WINDOWS:
            batch = (reversal_orders(window, book, history, index, day, previous_day, used)
                     if args.strategy else orders.get((day, window), ()))
            # Sells release cash before buys; stable within each side (input priority).
            for order in sorted(batch, key=lambda o: o.side == "buy"):
                code = order.code
                if code not in used and len(used) >= args.max_symbols:
                    result = {"reason": "daily_symbol_cap"}
                else:
                    used.add(code)
                    result = match(book, order, symbols.get(code), bars.get((day, window, code)), costs, index)
                total_fees += D(result.get("fees", "0"))
                total_slippage += D(result.get("slippage_cost", "0"))
                turnover += D(result.get("gross", "0"))
                ledger.append({"date": day, "window": window, "known_at": order.known_at, "symbol": code,
                               "side": order.side, "requested_shares": order.shares,
                               "filled_shares": order.shares if result["reason"] == "filled" else 0,
                               **result, "cash_after": str(book.cash)})
        receivable = sum(book.receivables.values(), D(0))
        equity = book.cash + receivable + sum(qty * symbols[code].close for code, qty in book.holdings.items())
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
        nav.append({"date": day, "cash": str(book.cash), "equity": str(money(equity)),
                    "positions": dict(book.holdings), "symbols_submitted": len(used),
                    "dividend_accrued": str(accrued), "dividend_paid": str(paid), "receivable": str(receivable)})
        if args.strategy:
            # Six consecutive unadjusted closes give five returns; reset on gaps, halts and ex dates.
            history = {code: history.get(code, deque(maxlen=6)) for code, row in symbols.items()
                       if row.status == "trading" and row.action == "none"}
            for code, closes in history.items():
                closes.append(symbols[code].close)
        previous_day = day

    count = Counter(row["reason"] for row in ledger)
    expected = sum(len(WINDOWS) for symbols in market.values() for row in symbols.values() if row.status == "trading")
    summary = {"first_date": nav[0]["date"], "last_date": previous_day, "sessions": len(nav),
               "expected_window_bars": expected, "available_window_bars": len(bars),
               "initial_cash": str(initial), "final_equity": nav[-1]["equity"],
               "net_return": str((equity / initial - 1).quantize(D(".00000001"))),
               "max_drawdown": str(drawdown.quantize(D(".00000001"))),
               "fees": str(total_fees), "slippage_cost": str(total_slippage),
               "traded_value": str(turnover), "order_counts": dict(count)}
    if args.benchmark:
        levels = {}
        for row in rows(args.benchmark, ("date", "level"), hashes):
            day = date.fromisoformat(row["date"]).isoformat()
            level = number(row["level"], "benchmark level")
            require(day not in levels and level > 0, f"基准点位重复或不为正 {day}")
            levels[day] = level
        before = [day for day in levels if day < nav[0]["date"]]
        in_range = {day for day in levels if nav[0]["date"] <= day <= previous_day}
        require(before and market.keys() == in_range, "基准缺期初点位或交易日历不一致")
        base_date = max(before)
        for point in nav:
            point["benchmark_return"] = str((levels[point["date"]] / levels[base_date] - 1).quantize(D(".00000001")))
        summary.update(benchmark_base_date=base_date, benchmark_return=nav[-1]["benchmark_return"],
                       excess_return=str(D(summary["net_return"]) - D(nav[-1]["benchmark_return"])))
    return {"format": "a_share_backtest.v1", "strategy": args.strategy or "orders", "inputs_sha256": hashes,
            "settings": {"windows": list(WINDOWS), "max_symbols": args.max_symbols,
                         **{k: str(v) for k, v in costs.items()}}, "summary": summary,
            "orders": ledger, "daily_nav": nav}


def run_command(args):
    if args.action == "fetch-bars":
        from ..data.providers import fetch_bars
        return fetch_bars(args.daily, args.out)
    result = replay(args)
    folder = Path(args.out).resolve() / uuid4().hex
    folder.mkdir(parents=True)
    path = folder / "result.json"
    write_json(path, result)
    return {**result["summary"], "result": str(path)}
