"""Offline public selection publication and first-cloud-export regression."""
import json
import os
import subprocess
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ["AQ5_MOCK"] = "1"
os.environ["AQ5_DATA"] = os.environ["AQ5_TEST_DATA"]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aq5 import ai, analysis, db, live, market, marketstate, picker, publisher, sectors, sim  # noqa: E402
from aq5.config import CN_TZ, DATA  # noqa: E402

OLD, ARCHIVE, NEW, FAIL, CAND, PRIVATE = ("sh600001", "sh600002", "sh600003", "sh600004", "sh600005", "sh600006")


def read(path):
    return json.loads(path.read_text("utf-8"))


def quote(code, price, stamp):
    return {"code": code, "price": price, "pct": 0, "time": stamp}


def klines(end):
    return [{"date": (date.fromisoformat(end) - timedelta(days=40 - i)).isoformat(), "close": 12}
            for i in range(41)]


class PublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        ai.init()
        live.init()
        sectors.init()
        sim.ensure_accounts()

    def setUp(self):
        with db.tx() as c:
            c.execute("DELETE FROM picks")
            c.execute("DELETE FROM pick_runs")

    def seed(self, day, codes):
        with db.tx() as c:
            rid = c.execute("""INSERT INTO pick_runs(date,session,started,finished,picks) VALUES(?,'close','14:45:00','14:48:00',?)""",
                            (day, len(codes))).lastrowid
            c.executemany("INSERT INTO picks(run_id,date,code,price) VALUES(?,?,?,?)",
                          [(rid, day, code, 7) for code in codes])
        return rid

    def test_partial_reconciles_runs_quotes_and_analysis_without_private_codes(self):
        root = DATA / "public-test"
        data = root / "data"
        first = self.seed("2026-11-04", (OLD, ARCHIVE))
        quotes = [quote(OLD, 10.5, "20261104150000"), quote(ARCHIVE, 20, "20261104150000")]
        analyzed = []

        def overview():
            return {"last_run": picker.runs(1)[0], "indices": [], "pick_quotes": {}}

        def full_analysis(code):
            analyzed.append(code)
            if code == FAIL:
                raise RuntimeError("public source unavailable")
            return {"code": code, "name": code, "score": 72, "rating": "测试"}

        def candidates(run_id, limit):
            return [{"code": CAND, "price": 17}] if run_id != first else []

        def direct(codes):
            return {CAND: quote(CAND, 18.25, "20261106150000"), PRIVATE: quote(PRIVATE, 77, "20261106150000")}

        with patch.object(publisher, "repo_dir", return_value=root), patch.object(
            publisher, "public_overview", side_effect=overview
        ), patch.object(publisher, "_private_pack", return_value=[]), patch.object(
            market, "snapshot", side_effect=lambda: quotes
        ), patch.object(market, "quotes", side_effect=direct), patch.object(
            marketstate, "state", return_value={}
        ), patch.object(analysis, "analyze", side_effect=full_analysis), patch.object(
            market, "kline_with_live", side_effect=lambda *_: klines(clock.return_value.date().isoformat())
        ), patch.object(picker, "top_candidates", side_effect=candidates), patch.object(
            publisher, "now", return_value=datetime(2026, 11, 4, 15, 2, tzinfo=CN_TZ)
        ) as clock:
            publisher.export(full=False)
            self.assertTrue(read(data / "manifest.json")["full"])
            old_quote = read(data / "quotes.json")["quotes"][ARCHIVE]
            old_analysis = read(data / "analysis" / f"{ARCHIVE}.json")
            self.assertEqual(read(data / "picks" / "latest.json")["id"], first)
            newest = self.seed("2026-11-06", (OLD, NEW, FAIL))
            clock.return_value = datetime(2026, 11, 6, 15, 2, tzinfo=CN_TZ)
            quotes[:] = [quote(OLD, 11, "20261106150000"), quote(NEW, 23, "20261106150000"),
                         quote(ARCHIVE, 2, "20261103150000"), quote(PRIVATE, 99, "20261106150000")]
            analyzed.clear()
            publisher.export(full=False)
            self.assertFalse(read(data / "manifest.json")["full"])
            runs = read(data / "runs.json")
            self.assertEqual([r["id"] for r in runs], [newest, first])
            self.assertEqual(runs[0]["cand_list"], [{"code": CAND, "price": 17}])
            self.assertEqual(read(data / "picks" / "latest.json")["id"], newest)
            self.assertEqual(read(data / "picks" / "2026-11-06.json")[0]["id"], newest)
            self.assertEqual(set(analyzed), {OLD, NEW, FAIL})
            index = {r["code"]: r for r in read(data / "analysis" / "index.json")}
            self.assertEqual(set(index), {OLD, NEW, ARCHIVE})
            self.assertEqual(index[OLD]["pick_run_id"], newest)
            self.assertEqual(read(data / "analysis" / f"{ARCHIVE}.json"), old_analysis)
            q = read(data / "quotes.json")["quotes"]
            self.assertEqual(set(q), {OLD, NEW, ARCHIVE, CAND})
            self.assertEqual(q[ARCHIVE], old_quote)
            self.assertEqual((q[NEW]["price"], q[NEW]["pct"], q[NEW]["time"]), (23, 0, "2026-11-06 15:00:00"))
            self.assertEqual(q[CAND]["price"], 18.25)
            analyzed.clear()
            publisher.export(full=False)
            self.assertEqual(analyzed, [FAIL])  # only retry unavailable analysis

    def test_latest_analysis_preserves_old_asset_until_current_daily_bars_arrive(self):
        data = DATA / "bar-retry"
        run_id = self.seed("2026-11-06", (NEW,))
        run = picker.runs(1)[0]
        index = data / "analysis" / "index.json"
        asset = index.with_name(NEW + ".json")
        old_payload = {"code": NEW, "analysis_time": "2026-11-04 16:00:00", "kline": klines("2026-11-04")}
        publisher._w(asset, old_payload)
        publisher._w(index, [{"code": NEW, "time": old_payload["analysis_time"]}])
        analyzer = {"code": NEW, "score": 70, "rating": "测试"}
        with patch.object(analysis, "analyze", side_effect=lambda _: dict(analyzer)) as analyze, patch.object(
            market, "kline_with_live", side_effect=[[], klines("2026-11-05"), klines("2026-11-06")]
        ):
            for _ in range(2):
                self.assertEqual(publisher._analysis_assets(data, run, False, [run]), 0)
                self.assertEqual(read(asset), old_payload)
                self.assertEqual(read(index)[0]["time"], old_payload["analysis_time"])
                self.assertNotIn("pick_run_id", read(index)[0])
            self.assertEqual(publisher._analysis_assets(data, run, False, [run]), 1)
            self.assertEqual(publisher._analysis_assets(data, run, False, [run]), 0)
            self.assertEqual(analyze.call_count, 3)
        self.assertEqual(read(asset)["kline"][-1]["date"], "2026-11-06")
        self.assertEqual(read(index)[0]["pick_run_id"], run_id)

    def test_latest_pick_codes_have_priority_at_full_cap(self):
        archive = tuple(f"sh601{i:03d}" for i in range(90))
        self.seed("2026-11-04", archive)
        self.seed("2026-11-06", (NEW,))
        with patch.object(publisher, "today", return_value="2026-11-06"):
            actual = publisher._analysis_codes([NEW])
        self.assertEqual(len(actual), 80)
        self.assertEqual(actual[0], NEW)
        self.assertEqual(len(set(actual)), 80)

    def test_fresh_directory_inherits_public_branch(self):
        remote = DATA / "public-baseline"
        remote.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "data"], cwd=remote, check=True)
        (remote / "data").mkdir()
        (remote / "data" / "old-public.json").write_text("{}", "utf-8")
        subprocess.run(["git", "add", "."], cwd=remote, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "public baseline"], cwd=remote, check=True)
        local = DATA / "fresh-publication"
        cfg = {"push": True, "remote": remote.as_uri(), "branch": "data"}
        publisher._inherit_public_branch(local, cfg)
        self.assertEqual(read(local / "data" / "old-public.json"), {})
        self.assertTrue((local / ".git").is_dir())

    def test_existing_git_without_head_cannot_replace_the_data_branch(self):
        local = DATA / "incomplete-git"
        (local / ".git").mkdir(parents=True)
        cfg = {"enabled": True, "push": True, "remote": "https://example.invalid/never-requested", "branch": "data"}
        with patch.object(publisher, "load_settings", return_value={"publish": cfg}), patch.object(
            publisher, "repo_dir", return_value=local
        ), patch.object(publisher, "_git", return_value=(128, "no HEAD")) as git, patch.object(
            publisher, "export"
        ) as export, patch.object(publisher, "commit_push") as push:
            with self.assertRaisesRegex(RuntimeError, "停止本次推送"):
                publisher.publish(full=False)
        git.assert_called_once_with(["rev-parse", "--verify", "HEAD"], local)
        export.assert_not_called()
        push.assert_not_called()

    def test_failed_public_branch_probe_prevents_export_and_push(self):
        local = DATA / "failed-publication"
        cfg = {"enabled": True, "push": True, "remote": "https://example.invalid/never-requested", "branch": "data"}
        with patch.object(publisher, "load_settings", return_value={"publish": cfg}), patch.object(
            publisher, "repo_dir", return_value=local
        ), patch.object(publisher, "_git", return_value=(128, "offline")), patch.object(
            publisher, "export"
        ) as export, patch.object(publisher, "commit_push") as push:
            with self.assertRaisesRegex(RuntimeError, "停止本次推送"):
                publisher.publish(full=False)
        export.assert_not_called()
        push.assert_not_called()


if __name__ == "__main__":
    unittest.main()
