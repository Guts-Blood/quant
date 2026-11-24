"""Bounded, archived probes for local IV30 and reverse-DCF data dependencies.

Run from the repository root:
    python -m experiments.legacy_pilot.risk_source_probe --rounds 3 --interval 30
Short probes establish observed availability, not long-term source reliability.
Public endpoint field mappings were checked against AKShare's upstream source.
No credentials, broker connection, account-state writes, or scheduled service.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .risk_expectations import forward_dcf, local_iv30, reverse_dcf

ROOT = Path(__file__).resolve().parents[2]
CST = timezone(timedelta(hours=8))
SINA_API = "https://stock.finance.sina.com.cn/futures/api/openapi.php/StockOptionService."
EM_API = "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/"
CATEGORIES = {"510050": "50ETF", "510300": "300ETF", "510500": "500ETF"}
ALLOWED_HOSTS = {"stock.finance.sina.com.cn", "hq.sinajs.cn",
                 "emweb.securities.eastmoney.com", "push2.eastmoney.com"}
HQ_PATTERN = re.compile(r'var\s+hq_str_([A-Za-z0-9_]+)="([^"\r\n]*)";?')


def schema_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def utc_now():
    return datetime.now(timezone.utc)


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


class SnapshotClient:
    def __init__(self, folder, timeout=15, retries=1):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.timeout, self.retries = timeout, retries
        self.records = []

    def text(self, label, url, params=None, encoding=None):
        if params:
            url += "?" + urlencode(params)
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise ValueError("only configured public HTTPS sources are allowed")
        headers = {"User-Agent": "Mozilla/5.0 (compatible; local-research/1.0)",
                   "Accept": "*/*", "Accept-Encoding": "identity", "Cache-Control": "no-cache",
                   "Referer": "https://stock.finance.sina.com.cn/" if "sina" in parsed.hostname
                   else "https://emweb.securities.eastmoney.com/"}
        for attempt in range(self.retries + 1):
            observed = utc_now()
            start = time.monotonic()
            body, failure, charset = b"", None, encoding or "utf-8"
            meta = {"label": label, "url": url, "observed_at": observed.isoformat(),
                    "available_at": None, "attempt": attempt + 1, "transport_ok": False}
            try:
                with urlopen(Request(url, headers=headers), timeout=self.timeout) as response:
                    meta["http_status"] = response.status
                    meta["content_type"] = response.headers.get("Content-Type", "")
                    meta["final_url"] = response.geturl()
                    charset = encoding or response.headers.get_content_charset() or "utf-8"
                    body = response.read(4_000_001)
                    if len(body) > 4_000_000:
                        raise ValueError("response exceeds 4 MB probe limit")
                    meta["transport_ok"] = 200 <= response.status < 300
                    if not meta["transport_ok"]:
                        raise ValueError("unexpected HTTP status")
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                failure = exc
                meta["error"] = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, HTTPError):
                    meta["http_status"] = exc.code
                    body = exc.read(4_000_000)
            meta["latency_ms"] = round((time.monotonic() - start) * 1000, 2)
            meta["available_at"] = utc_now().isoformat()
            meta["bytes"] = len(body)
            meta["sha256"] = hashlib.sha256(body).hexdigest()
            name = f"{len(self.records):05d}-{hashlib.sha256(label.encode()).hexdigest()[:8]}"
            raw_path = self.folder / (name + ".bin")
            with raw_path.open("xb") as handle:
                handle.write(body)
            meta["raw_path"] = str(raw_path)
            write_json(self.folder / (name + ".json"), meta)
            self.records.append(meta)
            if failure is None:
                return body.decode(charset)
            status = meta.get("http_status")
            retryable = status is None or status == 429 or status >= 500
            if attempt == self.retries or not retryable:
                raise failure
            time.sleep(1 + attempt)
        raise RuntimeError("unreachable retry state")

    def json(self, label, url, params=None):
        return json.loads(self.text(label, url, params))


def sina_data(payload):
    if payload.get("result", {}).get("status", {}).get("code") != 0:
        raise ValueError("Sina API did not report success")
    value = payload["result"].get("data")
    if not isinstance(value, dict):
        raise ValueError("Sina response data is not an object")
    return value


def parse_sina_quote(contract, fields, *, underlying, kind, expiry):
    if len(fields) < 43:
        raise ValueError(f"{contract}: expected at least 43 quote fields")
    if fields[36] != underlying:
        raise ValueError(f"{contract}: underlying mismatch")
    name = fields[37]
    side_word = "购" if kind == "call" else "沽"
    if side_word not in name:
        raise ValueError(f"{contract}: contract name does not confirm option side")
    quoted = datetime.fromisoformat(fields[32])
    if quoted.tzinfo is None:
        quoted = quoted.replace(tzinfo=CST)
    return {"contract_id": contract, "underlying": underlying, "kind": kind,
            "strike": float(fields[7]), "bid": float(fields[1]), "ask": float(fields[3]),
            "expiry": expiry.isoformat(), "quoted_at": quoted.isoformat(),
            "adjusted": bool(re.search(r"[A-Za-z]$", name)), "contract_name": name,
            "vendor_status": fields[34], "field_count": len(fields)}


def fetch_options(client, underlying="510500", rate=0.02):
    category = CATEGORIES[underlying]
    metadata = sina_data(client.json("sina.option_months", SINA_API + "getStockName",
                                    {"exchange": "null", "cate": category}))
    if metadata.get("stockId") != underlying:
        raise ValueError("Sina metadata returned another underlying")
    months = sorted({m for m in metadata["contractMonth"] if re.fullmatch(r"\d{4}-\d{2}", m)})
    if len(months) < 2:
        raise ValueError("not enough listed expiry months")
    catalog = {}
    expiry_dates = []
    for month in months[:4]:
        info = sina_data(client.json("sina.option_expiry", SINA_API + "getRemainderDay",
                                    {"exchange": "null", "cate": category, "date": month}))
        expiry_day = datetime.fromisoformat(info["expireDay"][:10]).date()
        expiry = datetime.combine(expiry_day, datetime.min.time(), tzinfo=CST).replace(hour=15)
        if expiry <= utc_now():
            continue
        if expiry_day.isoformat()[:7] != month:
            raise ValueError("expiry date and contract month disagree")
        expiry_dates.append(expiry.isoformat())
        suffix = month.replace("-", "")[2:]
        symbols = [f"OP_UP_{underlying}{suffix}", f"OP_DOWN_{underlying}{suffix}"]
        codes_text = client.text("sina.option_contracts", "https://hq.sinajs.cn/list=" + ",".join(symbols),
                                 encoding="gb18030")
        code_rows = dict(HQ_PATTERN.findall(codes_text))
        for symbol, kind in zip(symbols, ("call", "put")):
            if symbol not in code_rows:
                raise ValueError("missing contract-list response")
            codes = re.findall(r"CON_OP_(\d+)", code_rows[symbol])
            if not codes:
                raise ValueError("empty option contract list")
            for code in codes:
                if code in catalog:
                    raise ValueError("duplicate contract across expiry/side lists")
                catalog[code] = (kind, expiry)
    quotes, parse_errors = [], []
    codes = sorted(catalog)
    for offset in range(0, len(codes), 60):
        chunk = codes[offset:offset + 60]
        text = client.text("sina.option_quotes", "https://hq.sinajs.cn/list=" + ",".join("CON_OP_" + c for c in chunk),
                           encoding="gb18030")
        rows = dict(HQ_PATTERN.findall(text))
        for code in chunk:
            try:
                kind, expiry = catalog[code]
                quote = parse_sina_quote(code, rows["CON_OP_" + code].split(","), underlying=underlying,
                                         kind=kind, expiry=expiry)
                quotes.append(quote)
            except (KeyError, ValueError, TypeError) as exc:
                parse_errors.append({"contract_id": code, "reason": str(exc)})
    if not quotes:
        raise ValueError("no usable option quote records")
    asof = utc_now()
    result = local_iv30(quotes, asof=asof, rate=rate)
    return {"status": "ok", "provider": "sina", "underlying": underlying,
            "observed_at": asof.isoformat(), "expected_contracts": len(catalog),
            "parsed_quotes": len(quotes), "parse_errors": parse_errors,
            "schema_fingerprint": schema_hash({"metadata_keys": sorted(metadata),
                                                "quote_field_counts": sorted({q["field_count"] for q in quotes})}),
            "expiry_dates": expiry_dates, "quotes": quotes, "calculation": result,
            "source_quality_flags": ["unofficial_public_endpoint", "name_based_adjustment_check",
                                     "15_00_expiry_time_assumption", "flat_rate_assumption",
                                     "historical_point_in_time_coverage_not_validated"]}


class CompanyTypeParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.company_type = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("id") == "hidctype":
            self.company_type = attrs.get("value")


def statement_rows(payload):
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
        raise ValueError("empty or changed financial-statement schema")
    if any(not row.get("REPORT_DATE") for row in rows):
        raise ValueError("financial statement lacks report date")
    return rows


def fetch_financials(client, stock="SH600519"):
    if not re.fullmatch(r"(?:SH|SZ)\d{6}", stock):
        raise ValueError("stock must be SH/SZ plus six digits")
    parser = CompanyTypeParser()
    parser.feed(client.text("eastmoney.company_type", EM_API + "Index", {"type": "web", "code": stock.lower()}))
    if parser.company_type is None:
        raise ValueError("company-type metadata missing; do not guess financial-sector model")
    statements = {}
    for kind in ("lrb", "zcfzb", "xjllb"):
        params = {"companyType": parser.company_type, "reportDateType": "0", "code": stock}
        dates = statement_rows(client.json("eastmoney." + kind + "_dates", EM_API + kind + "DateAjaxNew", params))
        selected = sorted({str(row["REPORT_DATE"])[:10] for row in dates}, reverse=True)[:5]
        if any(datetime.fromisoformat(day).date() > utc_now().date() for day in selected):
            raise ValueError("financial report date is in the future")
        params.update(reportType="1", dates=",".join(selected))
        rows = statement_rows(client.json("eastmoney." + kind, EM_API + kind + "AjaxNew", params))
        if any(str(row["REPORT_DATE"])[:10] not in selected for row in rows):
            raise ValueError("returned report periods differ from requested periods")
        statements[kind] = rows
    market = "1" if stock.startswith("SH") else "0"
    price_payload = client.json("eastmoney.market_cap", "https://push2.eastmoney.com/api/qt/stock/get",
                                {"secid": f"{market}.{stock[2:]}", "fltt": "2", "invt": "2",
                                 "fields": "f43,f57,f58,f84,f116,f127"})
    quote = price_payload.get("data")
    if not isinstance(quote, dict) or quote.get("f57") != stock[2:]:
        raise ValueError("market-cap quote missing or wrong security")
    market_cap, shares = float(quote["f116"]), float(quote["f84"])
    if not all(math.isfinite(x) and x > 0 for x in (market_cap, shares)):
        raise ValueError("nonpositive market cap or share count")
    price = float(quote["f43"])
    if not math.isfinite(price) or price <= 0 or abs(market_cap / shares - price) > max(0.02, price * 0.005):
        raise ValueError("price, total shares and market cap do not reconcile")
    fields = {"lrb": ["TOTAL_OPERATE_INCOME", "OPERATE_PROFIT", "TOTAL_PROFIT", "INCOME_TAX", "NETPROFIT"],
              "zcfzb": ["MONETARYFUNDS", "TOTAL_ASSETS", "TOTAL_LIABILITIES", "MINORITY_EQUITY",
                         "SHORT_LOAN", "LONG_LOAN", "BOND_PAYABLE"],
              "xjllb": ["NETCASH_OPERATE", "CONSTRUCT_LONG_ASSET"]}
    coverage = {kind: {field: sum(row.get(field) is not None for row in rows) for field in fields[kind]}
                for kind, rows in statements.items()}
    return {"status": "ok", "provider": "eastmoney", "stock": stock,
            "observed_at": utc_now().isoformat(), "company_type": parser.company_type,
            "market_cap_cny": market_cap, "total_shares": shares, "quote_raw": quote,
            "statements": statements, "field_coverage": coverage,
            "schema_fingerprint": schema_hash({kind: sorted({key for row in rows for key in row})
                                                for kind, rows in statements.items()}),
            "report_periods": {kind: sorted({str(row["REPORT_DATE"])[:10] for row in rows})
                               for kind, rows in statements.items()},
            "announcement_fields_present": {kind: sorted({key for row in rows for key in row
                                                          if "NOTICE" in key or "UPDATE" in key})
                                             for kind, rows in statements.items()},
            "automatic_dcf_status": "requires_explicit_reviewed_model_inputs",
            "missing_assumptions": ["operating_EBIT_reconciliation", "excess_cash_and_debt_bridge",
                                    "margin_path", "reinvestment", "discount_rate", "terminal_growth_ROIC"],
            "quality_flags": ["vendor_current_version_not_historical_vintage", "price_timestamp_not_verified",
                              "operating_profit_not_automatically_treated_as_EBIT"]}


def summarize_requests(records):
    labels = sorted({row["label"] for row in records})
    summary = []
    for label in labels:
        rows = [row for row in records if row["label"] == label]
        latency = sorted(row["latency_ms"] for row in rows)
        summary.append({"label": label, "attempts": len(rows),
                        "transport_successes": sum(row["transport_ok"] for row in rows),
                        "median_latency_ms": statistics.median(latency),
                        "p95_latency_ms": latency[math.ceil(0.95 * len(latency)) - 1],
                        "distinct_body_hashes": len({row["sha256"] for row in rows})})
    return summary


def run_probe(*, rounds=3, interval=30, underlying="510500", stock="SH600519", out=None,
              rate=0.02, dcf_input=None):
    if type(rounds) is not int or not 1 <= rounds <= 10 or not 0 <= interval <= 60:
        raise ValueError("probe bounded to 1..10 rounds and 0..60 seconds between rounds")
    if underlying not in CATEGORIES:
        raise ValueError("unsupported option underlying")
    started = utc_now()
    out = Path(out) if out else ROOT / "backtests" / "risk_expectations" / started.strftime("probe-%Y%m%dT%H%M%S%fZ")
    out.mkdir(parents=True, exist_ok=False)
    client = SnapshotClient(out / "raw")
    outcomes = []
    for index in range(rounds):
        for name, collect in (("options", lambda: fetch_options(client, underlying, rate)),
                              ("financials", lambda: fetch_financials(client, stock))):
            try:
                result = collect()
            except Exception as exc:
                result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
            path = out / f"round-{index + 1}-{name}.json"
            write_json(path, result)
            row = {"round": index + 1, "source": name, "status": result["status"], "artifact": str(path),
                   "observed_at": utc_now().isoformat(), "schema_fingerprint": result.get("schema_fingerprint")}
            if name == "options" and result["status"] == "ok":
                row.update(parsed_quotes=result["parsed_quotes"], expected_contracts=result["expected_contracts"],
                           calculation_status=result["calculation"]["status"],
                           iv30_percent=result["calculation"].get("iv30_percent"),
                           missing_reason=result["calculation"].get("reason"),
                           quote_coverage=result["parsed_quotes"] / result["expected_contracts"],
                           max_quote_age_seconds=result["calculation"].get("max_quote_age_seconds"))
            if name == "financials" and result["status"] == "ok":
                row.update(field_coverage=result["field_coverage"],
                           automatic_dcf_status=result["automatic_dcf_status"])
            if "error" in result:
                row["error"] = result["error"]
            outcomes.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
        if index + 1 < rounds:
            time.sleep(interval)
    model = {"base_revenue": 100, "tax_rate": 0.25, "sales_to_capital": 2,
             "discount_rate": 0.1, "terminal_growth": 0, "terminal_margin": 0.2,
             "terminal_roic": 0.15, "operating_margins": [0.2] * 5}
    synthetic = {"kind": "synthetic_fixture_not_a_stock_valuation", "known_growth": 0.08,
                 "result": reverse_dcf(forward_dcf(0.08, model)["enterprise_value"], model,
                                       growth_bounds=(0, 0.5))}
    write_json(out / "dcf-smoke.json", synthetic)
    if dcf_input is not None:
        supplied = json.loads(Path(dcf_input).read_text(encoding="utf-8"))
        write_json(out / "dcf-input.json", supplied)
        write_json(out / "dcf-result.json", reverse_dcf(supplied["target_enterprise_value"], supplied["model"],
                                                       growth_bounds=supplied["growth_bounds"]))
    report = {"started_at": started.isoformat(), "finished_at": utc_now().isoformat(),
              "scope": "short_window_probe_only", "long_term_stability": "not_established",
              "rounds": rounds, "interval_seconds": interval, "underlying": underlying, "stock": stock,
              "rate_assumption": rate, "outcomes": outcomes,
              "requests": summarize_requests(client.records),
              "schema_versions_seen": {name: len({r["schema_fingerprint"] for r in outcomes
                                                   if r["source"] == name and r.get("schema_fingerprint")})
                                       for name in ("options", "financials")},
              "historical_backtest_eligibility": "not_established_by_current_snapshots",
              "output_directory": str(out)}
    write_json(out / "health.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--underlying", choices=tuple(CATEGORIES), default="510500")
    parser.add_argument("--stock", default="SH600519")
    parser.add_argument("--rate", type=float, default=0.02, help="explicit flat rate assumption, not a live yield")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--dcf-input", type=Path)
    args = parser.parse_args()
    report = run_probe(rounds=args.rounds, interval=args.interval, underlying=args.underlying,
                       stock=args.stock, out=args.out, rate=args.rate, dcf_input=args.dcf_input)
    print(json.dumps({"output_directory": report["output_directory"], "requests": report["requests"]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
