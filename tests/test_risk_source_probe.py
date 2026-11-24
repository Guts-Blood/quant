"""Offline protocol, provenance and bounded-failure tests; no live requests."""
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from experiments.legacy_pilot.risk_source_probe import (
    CompanyTypeParser, SnapshotClient, parse_sina_quote, sina_data, statement_rows,
)


class FixtureResponse:
    status = 200

    def __init__(self, body=b'{"value": 7}'):
        self.body = body
        self.headers = Message()
        self.headers["Content-Type"] = "application/json; charset=utf-8"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return "https://push2.eastmoney.com/fixture"

    def read(self, limit):
        return self.body[:limit]


class SnapshotClientTests(unittest.TestCase):
    def test_archives_exact_response_and_availability(self):
        body = b'{"value": 7}'
        with tempfile.TemporaryDirectory() as folder, patch(
            "experiments.legacy_pilot.risk_source_probe.urlopen", return_value=FixtureResponse(body)
        ):
            client = SnapshotClient(folder)
            self.assertEqual(client.json("fixture", "https://push2.eastmoney.com/fixture"), {"value": 7})
            row = client.records[0]
            self.assertEqual(Path(row["raw_path"]).read_bytes(), body)
            self.assertEqual(row["sha256"], hashlib.sha256(body).hexdigest())
            self.assertGreaterEqual(datetime.fromisoformat(row["available_at"]),
                                    datetime.fromisoformat(row["observed_at"]))
            self.assertTrue(row["transport_ok"])

    def test_malformed_json_preserves_transport_evidence(self):
        body = b"<html>maintenance</html>"
        with tempfile.TemporaryDirectory() as folder, patch(
            "experiments.legacy_pilot.risk_source_probe.urlopen", return_value=FixtureResponse(body)
        ):
            client = SnapshotClient(folder)
            with self.assertRaises(json.JSONDecodeError):
                client.json("fixture", "https://push2.eastmoney.com/fixture")
            self.assertTrue(client.records[0]["transport_ok"])
            self.assertEqual(Path(client.records[0]["raw_path"]).read_bytes(), body)

    def test_forbidden_response_does_not_retry(self):
        error = HTTPError("https://push2.eastmoney.com/fixture", 403, "Forbidden", None,
                          io.BytesIO(b"forbidden"))
        with tempfile.TemporaryDirectory() as folder, patch(
            "experiments.legacy_pilot.risk_source_probe.urlopen", side_effect=error
        ) as opener:
            client = SnapshotClient(folder, retries=1)
            with self.assertRaises(HTTPError):
                client.text("fixture", "https://push2.eastmoney.com/fixture")
            self.assertEqual(opener.call_count, 1)
            self.assertEqual(client.records[0]["http_status"], 403)
            self.assertFalse(client.records[0]["transport_ok"])

    def test_connection_failure_is_bounded_and_recorded(self):
        with tempfile.TemporaryDirectory() as folder, patch(
            "experiments.legacy_pilot.risk_source_probe.urlopen", side_effect=URLError("fixture failure")
        ) as opener, patch("experiments.legacy_pilot.risk_source_probe.time.sleep"):
            client = SnapshotClient(folder, retries=1)
            with self.assertRaises(URLError):
                client.text("fixture", "https://push2.eastmoney.com/fixture")
            self.assertEqual(opener.call_count, 2)
            self.assertEqual(len(client.records), 2)
            self.assertTrue(all(not row["transport_ok"] for row in client.records))

    def test_rejects_unconfigured_destination(self):
        with tempfile.TemporaryDirectory() as folder, patch(
            "experiments.legacy_pilot.risk_source_probe.urlopen"
        ) as opener:
            with self.assertRaises(ValueError):
                SnapshotClient(folder).text("fixture", "http://example.com/")
            opener.assert_not_called()


class VendorContractTests(unittest.TestCase):
    def fields(self):
        # Synthetic fixture matching the documented 43-field Sina contract.
        fields = ["0"] * 43
        fields[1], fields[3], fields[7] = "0.10", "0.12", "7.5"
        fields[32] = "2026-11-24 10:00:00"
        fields[36], fields[37] = "510500", "500ETF购12月7500"
        return fields

    def parse(self, fields, **kwargs):
        arguments = {"underlying": "510500", "kind": "call",
                     "expiry": datetime(2026, 12, 23, 15, tzinfo=timezone(timedelta(hours=8)))}
        arguments.update(kwargs)
        return parse_sina_quote("fixture", fields, **arguments)

    def test_quote_mapping_and_timezone(self):
        quote = self.parse(self.fields())
        self.assertEqual((quote["bid"], quote["ask"], quote["strike"]), (0.1, 0.12, 7.5))
        self.assertEqual(datetime.fromisoformat(quote["quoted_at"]).utcoffset(), timedelta(hours=8))
        self.assertIs(quote["adjusted"], False)

    def test_missing_fields_fail_instead_of_becoming_zero(self):
        with self.assertRaises(ValueError):
            self.parse(self.fields()[:32])

    def test_wrong_underlying_fails(self):
        with self.assertRaises(ValueError):
            self.parse(self.fields(), underlying="510050")

    def test_wrong_option_side_fails(self):
        with self.assertRaises(ValueError):
            self.parse(self.fields(), kind="put")

    def test_missing_quote_time_fails(self):
        fields = self.fields()
        fields[32] = "-"
        with self.assertRaises(ValueError):
            self.parse(fields)

    def test_adjusted_contract_is_marked(self):
        fields = self.fields()
        fields[37] += "A"
        self.assertIs(self.parse(fields)["adjusted"], True)

    def test_api_error_is_not_empty_success(self):
        with self.assertRaises(ValueError):
            sina_data({"result": {"status": {"code": 1}, "data": {}}})

    def test_financial_report_requires_date(self):
        for payload in ({"data": []}, {"data": [{"income": 5}]}, {}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                statement_rows(payload)

    def test_company_type_does_not_depend_on_attribute_order(self):
        parser = CompanyTypeParser()
        parser.feed('<input value="4" type="hidden" id="hidctype">')
        self.assertEqual(parser.company_type, "4")


if __name__ == "__main__":
    unittest.main()
