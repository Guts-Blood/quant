import csv
import sys
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factorlab.backtest.window import replay, run_command
from factorlab.backtest.data import BARS, DAILY, ORDERS
from factorlab.data.providers import fetch_bars
from factorlab.backtest.window import parser
from factorlab.backtest.validation import ValidationError as AgentError


class BacktestTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dates = ["2026-11-16", "2026-11-17", "2026-11-18"]
        self.code = "600036.SH"
        self.market = []
        self.bars = []
        self.orders = []
        self.add_code(self.code)

    def add_code(self, code):
        for day in self.dates:
            self.market.append([day, code, "10", "9", "11", "trading", "none", "", ""])
            self.bars.extend([[day, end, code, "10", 100000] for end in ("11:05", "14:05")])

    def order(self, idx, window, side, code=None, qty=100):
        day = self.dates[idx]
        self.orders.append([day, window, day + "T10:59:59+08:00", code or self.code, side, qty])

    def write(self, name, header, items):
        with (self.root / name).open("w", encoding="utf-8", newline="") as out:
            w = csv.writer(out)
            w.writerow(header)
            w.writerows(items)
        return str(self.root / name)

    def args(self, *extra, strategy=False):
        daily = self.write("daily.csv", DAILY + ["cash_per_share", "pay_date"], self.market)
        bars = self.write("bars.csv", BARS, self.bars)
        orders = self.write("orders.csv", ORDERS, self.orders)
        mode = ["--strategy", "reversal5"] if strategy else ["--orders", orders]
        return parser().parse_args(["backtest", "run", "--daily", daily, "--bars", bars, *mode, *extra])

    def test_t1_book_fees_rounding_and_carry(self):
        self.order(0, "11:00", "buy")
        self.order(0, "14:00", "sell")
        self.order(1, "11:00", "buy")
        self.order(1, "14:00", "sell")
        result = replay(self.args("--cash", "4000"))
        self.assertEqual([o["reason"] for o in result["orders"]], ["filled", "t_plus_one", "filled", "filled"])
        self.assertEqual([o.get("fill_price") for o in result["orders"]], ["10.01", None, "10.01", "9.99"])
        self.assertEqual(result["daily_nav"][1]["positions"], {self.code: 100})
        # gross buys 2002, gross sell 999, commissions 15, levy .50, transfer .03
        self.assertEqual(D(result["daily_nav"][-1]["cash"]), D("2981.47"))
        self.assertEqual(D(result["summary"]["final_equity"]), D("3981.47"))
        self.assertEqual(D(result["summary"]["fees"]), D("15.53"))
        self.assertEqual(D(result["summary"]["slippage_cost"]), D("3"))

    def test_ten_distinct_submissions_across_both_windows_and_reset(self):
        codes = [f"{600101+i}.SH" for i in range(11)]
        for i, code in enumerate(codes):
            self.add_code(code)
            self.order(0, "11:00" if i < 6 else "14:00", "buy", code)
        self.order(0, "14:00", "buy", codes[0])
        self.order(1, "11:00", "buy", codes[-1])
        self.bars = [r for r in self.bars if not (r[2] == codes[0] and r[1] == "11:05")]
        result = replay(self.args())
        self.assertEqual([o["reason"] for o in result["orders"]][-3:], ["daily_symbol_cap", "filled", "filled"])
        self.assertEqual([n["symbols_submitted"] for n in result["daily_nav"]], [10, 1, 0])
        self.assertEqual(result["orders"][0]["reason"], "missing_bar")

    def test_execution_quality_and_gaps(self):
        self.order(0, "11:00", "buy")
        self.order(0, "14:00", "buy")
        self.order(1, "11:00", "buy")
        self.order(1, "14:00", "buy")
        self.order(2, "11:00", "buy")
        self.bars[0][3] = "11"
        self.bars[1][4] = 0
        self.bars[2][4] = 99
        self.market[-1][5] = "suspended"
        self.bars = self.bars[:4]
        r = replay(self.args("--cash", "1000"))
        self.assertEqual([o["reason"] for o in r["orders"]],
                         ["price_limit", "zero_volume", "volume_cap", "insufficient_cash", "suspended"])

    def test_invalid_order_batches_and_signal_time(self):
        for window, known in [("11:01", "2026-11-16T08:00:00+08:00"),
                              ("11:00", "2026-11-16T03:00:00Z"), ("14:00", "2026-11-16T11:00")]:
            with self.subTest(window=window, known=known):
                self.orders = [[self.dates[0], window, known, self.code, "buy", 100]]
                with self.assertRaises(AgentError):
                    replay(self.args())
        self.orders = []
        self.order(0, "11:00", "buy")
        self.order(0, "11:00", "sell")
        with self.assertRaises(AgentError):
            replay(self.args())

    def test_sell_precedes_buy_and_cash_is_reusable(self):
        other = "000333.SZ"
        self.add_code(other)
        self.order(0, "11:00", "buy")
        self.order(1, "11:00", "buy", other)
        self.order(1, "11:00", "sell")
        r = replay(self.args("--cash", "1020"))
        self.assertEqual([(o["side"], o["reason"]) for o in r["orders"]],
                         [("buy", "filled"), ("sell", "filled"), ("buy", "filled")])

    def test_dividend_entitlement_and_valuation_fail_closed(self):
        self.order(0, "11:00", "buy")
        self.order(1, "11:00", "sell")
        self.market[1][6:] = ["cash", "0.15", self.dates[2]]
        r = replay(self.args())
        nav = r["daily_nav"]
        self.assertEqual(D(nav[1]["receivable"]), D(15))
        self.assertEqual(D(nav[2]["cash"]) - D(nav[1]["cash"]), D(15))
        self.assertEqual(nav[1]["equity"], nav[2]["equity"])
        self.market[1][6:] = ["unknown", "", ""]
        with self.assertRaisesRegex(AgentError, "公司行动"):
            replay(self.args())
        self.add_code("000333.SZ")
        del self.market[1]
        self.bars = [r for r in self.bars if r[0] != self.dates[1] or r[2] != self.code]
        with self.assertRaisesRegex(AgentError, "缺收盘估值"):
            replay(self.args())

    def test_strategy_only_uses_previous_closes(self):
        self.market, self.bars, self.dates = [], [], []
        start = date(2026, 11, 2)
        for i in range(15):
            d = start + timedelta(days=i)
            if d.weekday() < 5:
                self.dates.append(d.isoformat())
        self.add_code(self.code)
        self.add_code("000333.SZ")
        self.market[5][2] = "9.1"
        self.market[6][2] = "10.9"
        before = replay(self.args(strategy=True))
        self.market[6][2] = "9.1"
        after = replay(self.args(strategy=True))
        for result in [before, after]:
            first = result["orders"][0]
            self.assertEqual((first["date"], first["window"], first["symbol"]),
                             (self.dates[6], "14:00", self.code))

    def test_input_coverage_benchmark_and_frozen_output(self):
        self.order(0, "11:00", "buy")
        bench = self.write("bench.csv", ["date", "level"],
                           [["2026-11-13", 100], *zip(self.dates, [100, 101, 102])])
        args = self.args("--benchmark", bench, "--out", str(self.root / "runs"))
        first = run_command(args)
        second = run_command(args)
        self.assertEqual(D(first["benchmark_return"]), D(".02"))
        self.assertEqual(first["expected_window_bars"], 6)
        self.assertNotEqual(first["result"], second["result"])
        self.assertEqual(Path(first["result"]).read_bytes(), Path(second["result"]).read_bytes())
        self.write("bench.csv", ["date", "level"], zip(self.dates, [100, 101, 102]))
        with self.assertRaisesRegex(AgentError, "基准"):
            replay(args)

    def test_fetch_rejects_incomplete_source_before_publishing(self):
        args = self.args()
        with patch("factorlab.data.providers.urlopen"), patch("factorlab.data.providers.json.load", return_value=[]):
            output = self.root / "bad.csv"
            with self.assertRaisesRegex(AgentError, "收盘"):
                fetch_bars(args.daily, output)
            self.assertFalse(output.exists())
