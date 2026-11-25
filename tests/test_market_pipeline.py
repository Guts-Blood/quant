"""Transport interruptions, temporal alignment and full three-arm replay regressions."""

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

try:
    import duckdb
except ModuleNotFoundError as exc:
    raise unittest.SkipTest("Install .[market] for portable data tests") from exc
import pandas as pd

from experiments.market_research import CONFIG, overlay, run
from experiments.market_snapshot import manifest_bytes
from experiments.market_store import pack_run, restore
from experiments.option_capture import CST, collect, observation
from experiments.shortlist_dcf import annotate
from test_risk_expectations import option_fixture, dcf_fixture


class UpdateTests(unittest.TestCase):
    def test_failed_stale_missing_then_resume_and_repeat(self):
        cutoff = datetime(2026, 11, 25, 15, 1, tzinfo=CST)
        clock = lambda: cutoff + timedelta(seconds=10)
        rows = [{**q, "quoted_at": cutoff.isoformat(), "underlying": "510500"} for q in option_fixture()]
        stale = [{**q, "quoted_at": (cutoff - timedelta(seconds=301)).isoformat()} for q in rows]
        stale[0]["quoted_at"] = cutoff.isoformat()
        stale[0]["bid"] = None
        with TemporaryDirectory() as tmp:
            root = Path(tmp)

            def missing(client, **kw):
                return {"quotes": stale, "expected_contracts": len(rows)}

            failed = collect(root, rate=0.02, now=clock, fetch=missing)
            failure = observation(failed, "daily")
            self.assertEqual(failure["calculation"]["status"], "unavailable")
            self.assertEqual(
                failure["quality"]["excluded_by_reason"]["future or stale quote"], len(rows) - 1
            )
            self.assertEqual(failure["quality"]["with_bid_and_ask"], len(rows) - 1)

            def interrupted(client, **kw):
                raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt):
                collect(root, rate=0.02, now=clock, fetch=interrupted, retry_unavailable=True)
            good = collect(
                root, rate=0.02, now=clock, fetch=lambda *a, **kw: {"quotes": rows}, retry_unavailable=True
            )
            self.assertEqual(observation(good, "daily")["calculation"]["status"], "ok")
            self.assertEqual(collect(root, rate=0.02, now=clock, fetch=interrupted), good)
            self.assertEqual(len(list(root.glob("*.zip"))), 2)
            with self.assertRaisesRegex(ValueError, "different rate"):
                collect(root, rate=0.03, now=clock, fetch=interrupted)

    def test_pack_run_pins_exact_selected_outputs(self):
        from zipfile import ZipFile

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "schedule.csv").write_bytes(b"schedule")
            (root / "unlisted.txt").write_bytes(b"temporary")
            receipt = {
                "snapshot": "v1",
                "manifest_sha256": "0" * 64,
                "outputs": {"schedule.csv": hashlib.sha256(b"schedule").hexdigest()},
            }
            (root / "run.json").write_text(json.dumps(receipt))
            dest = pack_run(root, root / "archives")
            self.assertEqual(dest, pack_run(root, root / "archives"))
            with ZipFile(dest) as z:
                self.assertEqual(set(z.namelist()), {"run.json", "schedule.csv"})
            (root / "schedule.csv").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "Changed research output"):
                pack_run(root, root / "archives")

    def test_split_restore_resumes_and_refuses_changed_version(self):
        import shutil

        if not shutil.which("rclone"):
            self.skipTest("requires rclone")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, dest = root / "remote", root / "destination"
            remote.mkdir()

            def spec(name, body, parts):
                return {
                    "name": name,
                    "source": None,
                    "drive_parts": parts,
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "bytes": len(body),
                }

            items = [spec("first", b"first", ["a"]), spec("second", b"second", ["b", "c"])]
            manifest = root / "pin.json"
            manifest.write_bytes(manifest_bytes({"snapshot": "v1", "files": items}))
            for name, body in (("a", b"first"), ("b", b"sec"), ("c", b"bad")):
                (remote / name).write_bytes(body)
            with self.assertRaisesRegex(ValueError, "mismatch"):
                restore(str(remote), dest, manifest, False)
            self.assertEqual((dest / "first").read_bytes(), b"first")
            self.assertFalse((dest / "manifest.json").exists())
            self.assertFalse((dest / "second").exists())
            (remote / "c").write_bytes(b"ond")
            restore(str(remote), dest, manifest, False)
            self.assertEqual((dest / "second").read_bytes(), b"second")
            manifest.write_bytes(manifest_bytes({"snapshot": "v2", "files": items}))
            with self.assertRaisesRegex(ValueError, "conflict"):
                restore(str(remote), dest, manifest, False)


def sample(db, dates):
    dates_frame = pd.DataFrame({"date": dates.date, "i": range(len(dates))})
    db.register("dates_frame", dates_frame)
    db.execute("CREATE TABLE trading_calendar AS SELECT date,i FROM dates_frame")
    db.execute("""CREATE TABLE daily AS SELECT t.date,cast(600000+n AS VARCHAR) stock_code,
        round(10+n*.01+t.i*.01,2) AS open, round(10+n*.01+t.i*.01,2) AS close,
        10000000 AS volume, case when n<12 then 50000000 else 10000 end AS amount
        FROM trading_calendar t,range(501) s(n)""")
    db.execute(
        "CREATE VIEW valuation AS SELECT date,stock_code,false AS is_st, CAST(stock_code AS INTEGER)-599980 AS pe_ttm FROM daily"
    )
    db.execute("""CREATE VIEW execution AS SELECT date,stock_code,0.001 AS adj_return,true AS tradable,
        'none' AS action,20.00 AS limit_up,5.00 AS limit_down,false AS limit_exempt,
        NULL::DOUBLE AS cash_per_share,NULL::DATE AS pay_date FROM daily""")
    db.execute("""CREATE VIEW csi500_members AS SELECT t.date,cast(600000+n AS VARCHAR) stock_code
        FROM trading_calendar t,range(501) s(n) WHERE CASE WHEN t.i<130 THEN n<500 ELSE n>0 END""")
    db.execute("CREATE VIEW session_coverage AS SELECT date, true AS usable FROM trading_calendar")
    db.execute("""CREATE TABLE option_observations AS SELECT date,'daily' AS kind,'510500' AS underlying,
        'ok' AS status,cast(case when i=126 or i>=130 then .30 when i=127 then .10 else .20 end as double) AS iv30,
        timezone('Asia/Shanghai',date+TIME '15:05') AS available_at FROM trading_calendar
        WHERE i<>129""")
    db.execute("""INSERT INTO option_observations SELECT date,'intraday','510500','ok',.95,
        timezone('Asia/Shanghai',date+TIME '14:10') FROM trading_calendar WHERE i=129""")
    db.execute("""UPDATE option_observations SET available_at=available_at+INTERVAL 2 HOUR
        WHERE date=(SELECT date FROM trading_calendar WHERE i=131)""")


class MatchedResearchTests(unittest.TestCase):
    def test_prior_only_session_window_and_three_matched_arms(self):
        dates = pd.bdate_range("2024-01-01", periods=140)
        cfg = json.loads(CONFIG.read_bytes())
        with duckdb.connect() as db, TemporaryDirectory() as tmp:
            sample(db, dates)
            flags = overlay(db, dates, cfg)
            self.assertFalse(flags.iloc[125].valid)
            self.assertTrue(flags.iloc[126].high)
            self.assertEqual(flags.iloc[126].threshold, 0.2)
            self.assertFalse(flags.iloc[129].valid)  # only intraday
            self.assertFalse(flags.iloc[131].valid)  # became available after cutoff
            root = Path(tmp)
            report = run(
                db,
                {"snapshot": "synthetic", "files": []},
                "0" * 64,
                start=dates[126].date(),
                end=dates[134].date(),
                out=root,
                mode="compare",
            )
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["effective_rebalances"], 6)
            self.assertEqual(report["high_vol_rebalances"], 5)
            self.assertTrue(flags.iloc[128].high)  # the equality boundary is inclusive
            self.assertFalse(flags.iloc[127].high)
            self.assertEqual(report["high_vol_rebalances"], int(flags.iloc[125:134].high.sum()))
            candidates = pd.read_csv(root / "candidates.csv", dtype={"symbol": str})
            self.assertTrue((candidates.symbol == "600000").any())
            self.assertFalse(
                (
                    (candidates.trade_date == dates[130].date().isoformat())
                    & (candidates.symbol == "600000")
                    & candidates.member_on_trade_date
                ).any()
            )
            self.assertGreater(report["metrics"]["original"]["fees"], 0)
            self.assertLess(
                report["metrics"]["iv30_dynamic"]["mean_actual_exposure"],
                report["metrics"]["original"]["mean_actual_exposure"],
            )
            allowed = set(pd.read_csv(root / "schedule.csv").query("effective").trade_date)
            for label in report["metrics"]:
                self.assertTrue(set(pd.read_csv(root / f"{label}-orders.csv").date) <= allowed)
            before = (root / "candidates.csv").read_bytes()
            r = candidates.iloc[0]
            entry = {
                "signal_date": r.signal_date,
                "symbol": r.symbol,
                "shares": 100,
                "available_at": r.signal_date + "T15:30:00+08:00",
                "source_id": "analytic fixture",
                "debt": 1,
                "cash": 3,
                "minority_interests": 0,
                "nonoperating_assets": 0,
                "model": dcf_fixture(),
                "attainable_growth": 0.1,
                "stress_growth": -0.05,
            }
            inputs = root / "inputs.json"
            inputs.write_text(json.dumps([entry]))
            notes = annotate(root, inputs, root / "notes.json")["notes"]
            self.assertEqual(len(notes), len(candidates))
            self.assertEqual(notes[0]["scenarios"]["stress"]["growth"], -0.05)
            self.assertEqual((root / "candidates.csv").read_bytes(), before)
            inputs.write_text(json.dumps([{**entry, "available_at": r.signal_date + "T16:01:00+08:00"}]))
            with self.assertRaisesRegex(ValueError, "after"):
                annotate(root, inputs, root / "late.json")

    def test_old_valid_observations_expire_with_the_252_session_window(self):
        dates = pd.bdate_range("2022-01-03", periods=400)
        with duckdb.connect() as db:
            sample(db, dates)
            db.execute("""DELETE FROM option_observations WHERE date IN
                (SELECT date FROM trading_calendar WHERE i BETWEEN 127 AND 325)""")
            flags = overlay(db, dates, json.loads(CONFIG.read_bytes()))
            self.assertFalse(flags.iloc[326].valid)
            self.assertLess(flags.iloc[326].prior_valid, 126)
