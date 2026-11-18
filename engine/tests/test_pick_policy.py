"""强 A 原榜独立留样、分场准入、失效和正式选股的离线回归。"""
import os
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ["AQ5_MOCK"] = "1"
os.environ["AQ5_DATA"] = os.environ["AQ5_TEST_DATA"]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aq5 import ai, config, db, learn, market, pick_policy, picker, research, sectors


class PickPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        research.init()
        sectors.init()
        ai.init()

    def setUp(self):
        with db.tx() as c:
            for table in ("picks", "samples", "pick_runs", "research_runs", "research_samples",
                          "research_controls", "research_label_windows"):
                c.execute(f"DELETE FROM {table}")
        self.scope = {"mock": True}
        self.stamp = datetime(2026, 11, 6, 16, tzinfo=config.CN_TZ)
        clock = patch.object(pick_policy, "now", return_value=self.stamp)
        clock.start()
        self.addCleanup(clock.stop)

    def seed(self, returns, session="close", n=35):
        start = date(2026, 7, 1)
        days = [start + timedelta(days=i) for i in range(90) if (start + timedelta(days=i)).weekday() < 5]
        with db.tx() as c:
            for i in range(n):
                day = days[i].isoformat()
                rid = c.execute("INSERT INTO pick_runs(date,session) VALUES(?,?)", (day, session)).lastrowid
                codes = ["sh600001", "sh600002"]
                signal = {"protocol": pick_policy.VERSION, "scope": self.scope,
                          "baseline_codes": codes, "quality_blocked": False}
                c.execute("INSERT INTO research_runs VALUES(?,?,?,?)",
                          (rid, day, research.VERSION, db.dumps({"selection": signal})))
                for code, ret in zip(codes, returns):
                    sid = c.execute("""INSERT INTO samples(run_id,code,date,session,ret_3d,evaluated)
                                       VALUES(?,?,?,?,?,1)""", (rid, code, day, session, ret)).lastrowid
                    c.execute("INSERT INTO research_label_windows VALUES(?,?,?)",
                              (sid, days[i+3].isoformat(), days[i+5].isoformat()))
        return days

    def review(self):
        return pick_policy.review()

    def test_top_one_requires_paired_lift_can_revoke_and_scope_changes_block(self):
        self.seed((2, -4))
        close = self.review()["close"]
        self.assertEqual(close["mode"], "top1", close)
        self.assertEqual(close["validation"]["top1"]["net_pct"], 1.7)
        self.assertLess(close["folds"][0]["train_label_end"], close["folds"][0]["test_start"])
        self.assertEqual(pick_policy.current("close", self.scope)["mode"], "top1")
        self.assertEqual(pick_policy.current("close", {"mock": False})["mode"], "observe")
        self.assertEqual(pick_policy.current("auction")["mode"], "observe")
        self.assertIsNotNone(db.q1("SELECT value FROM research_controls WHERE name=?", (close["evidence_id"],)))
        with db.tx() as c:
            c.execute("UPDATE samples SET ret_3d=0.2")
        self.assertEqual(self.review()["close"]["status"], "rejected")
        self.assertEqual(learn.summary()["pick_policy"]["close"]["mode"], "observe")

    def test_absolute_all_a_and_missing_latest_label_fail_closed(self):
        self.seed((1, 2), "auction")
        self.assertEqual(self.review()["auction"]["mode"], "all_a")
        with db.tx() as c:
            c.execute("DELETE FROM research_label_windows WHERE sample_id=(SELECT MAX(id) FROM samples)")
        result = self.review()["auction"]
        self.assertEqual((result["mode"], result["status"]), ("observe", "insufficient"))

    def test_no_forward_or_legacy_evidence_is_admitted(self):
        days = self.seed((2, -4), n=31)
        result = pick_policy.evaluate("close", days[29].isoformat())
        self.assertEqual(result["mode"], "observe")
        self.assertEqual(result["mature_signal_dates"], 25)
        with db.tx() as c:
            c.execute("UPDATE research_runs SET report='{}'")
        self.assertEqual(self.review()["close"]["mature_signal_dates"], 0)

    def test_actual_picker_freezes_untruncated_a_and_retains_b_c_as_research(self):
        quotes = market.snapshot()[:5]
        stamp = self.stamp.replace(hour=14, minute=46)
        for q in quotes:
            q.update(name="测试", price=10, prev=10, open=10, amount=5e8, pct=0, suspended=False, listed=None,
                     time=stamp.strftime("%Y%m%d%H%M%S"))
        scores = dict(zip((q["code"] for q in quotes), (90, 89, 79, 75, 48)))
        def evaluate(code, *_):
            score = scores[code]
            return {"code": code, "score": score, "parts": {"trend": score}, "raw": {"trend": score},
                    "signals": [{"id": "x", "why": "测试", "name": "测试", "conf": .8 if score > 75 else .2}],
                    "prob": .5, "prob_src": "heuristic", "plan": {"rr": 1.5, "stop": 9, "target1": 11, "target2": 12},
                    "ind": {"extra": {"rsi6": 70}}}
        with patch.object(market, "snapshot", return_value=quotes), patch.object(
            picker.analysis, "evaluate", side_effect=evaluate
        ), patch.object(picker.analysis, "regime", return_value={"code": "bull"}), patch.object(
            sectors, "compute", return_value={"stock": {}, "by_code": {}}
        ), patch.object(research, "timestamp", return_value=stamp.isoformat(timespec="seconds")), patch.object(
            ai, "ready", return_value=False
        ), patch.object(picker, "today", return_value=stamp.date().isoformat()), patch.object(picker, "load_settings",
            return_value={**config.load_settings(), "pick": {**config.load_settings()["pick"], "auto_trade": False}}
        ), patch("aq5.plan.on_picks") as follow:
            initial = picker.run("close")
            self.assertEqual(initial["picks"], [])
            follow.assert_not_called()
            s = initial["research"]["selection"]
            self.assertEqual(s["tier_counts"], {"A": 3, "B": 1, "C": 1})
            self.assertEqual(len(s["baseline_codes"]), 3)
            research_pool = picker.top_candidates(initial["run_id"])
            self.assertFalse(any(p["eligible"] for p in research_pool))
            self.assertEqual([p["tier"] for p in research_pool], list("AAABC"))
            self.scope = s["scope"]
            self.seed((2, -4))
            with patch.object(pick_policy, "now", return_value=self.stamp - timedelta(days=1)):
                approved = self.review()["close"]
            self.assertEqual(approved["mode"], "top1")
            later = picker.run("close")
            self.assertEqual([p["code"] for p in later["picks"]], s["baseline_codes"][:1])
            self.assertEqual(later["research"]["selection"]["baseline_codes"], s["baseline_codes"])
            self.assertEqual(later["research"]["selection"]["evidence_id"], approved["evidence_id"])
            self.assertTrue(later["picks"][0]["eligible"])
            self.assertEqual(later["picks"][0]["selection_protocol"], pick_policy.VERSION)
            follow.assert_called_once()
            self.assertEqual(len(db.q("SELECT id FROM samples WHERE run_id IN (?,?)",
                                      (initial["run_id"], later["run_id"]))), 10)
            self.assertEqual(picker.runs(1)[0]["items"][0]["selection_protocol"], pick_policy.VERSION)
            self.assertIsInstance(s["scope"], str)
            verified_quote = dict(quotes[0])
            quotes[0].pop("time")
            with patch.object(market, "quotes", return_value={quotes[0]["code"]: verified_quote}) as lookup:
                self.assertFalse(picker.run("close")["research"]["selection"]["quality_blocked"])
                lookup.assert_called_once_with([quotes[0]["code"]])
            verified_quote["price"] = 11
            with patch.object(market, "quotes", return_value={quotes[0]["code"]: verified_quote}):
                bad = picker.run("close")
                self.assertEqual(bad["picks"], [])
                self.assertTrue(bad["research"]["selection"]["quality_blocked"])

    def test_only_verified_reference_and_same_price_basis_mature(self):
        days = self.seed((1, 2), n=1)
        with db.tx() as c:
            c.execute("DELETE FROM research_label_windows")
            c.execute("UPDATE samples SET evaluated=0,price=10,ret_3d=NULL")
            for entry in c.execute("SELECT run_id,code FROM samples").fetchall():
                ref = {"open": 10} if entry["code"] == "sh600001" else None
                c.execute("INSERT INTO research_samples VALUES(?,?,80,1,?)",
                          (entry["run_id"], entry["code"], db.dumps({"reference": ref})))
        adjusted = [{"date": d.isoformat(), "open": 5, "close": 5.5, "high": 5.6, "low": 4.9} for d in days[:6]]
        def incomplete(code, _):
            return adjusted[:1] + adjusted[2:] if code == "sh000300" else adjusted
        with patch.object(learn, "_closed_rows", side_effect=incomplete):
            learn.fill_outcomes()
        self.assertEqual(len(db.q("SELECT * FROM research_label_windows")), 0)
        with patch.object(learn, "_closed_rows", return_value=adjusted):
            learn.fill_outcomes()
        quotes = db.q("SELECT ret_3d,evaluated FROM samples ORDER BY code")
        self.assertEqual(quotes, [{"ret_3d": 10.0, "evaluated": 1}, {"ret_3d": None, "evaluated": 0}])
        self.assertEqual(len(db.q("SELECT * FROM research_label_windows")), 1)

    def test_provider_time_and_cash_session_are_verified(self):
        at = config.now().replace(hour=14, minute=45, second=0, microsecond=0)
        raw = {"f13": 1, "f12": "600001", "f17": 10, "f124": at.timestamp()}
        row = market._em_row(raw)
        self.assertEqual(pick_policy.reference(row, "close", at.isoformat())["source"], "东财快照")
        self.assertIsNone(pick_policy.reference(row, "auction", at.isoformat()))
        for age in (-1, 310):
            self.assertIsNone(pick_policy.reference(row, "close", (at + timedelta(seconds=age)).isoformat()))


if __name__ == "__main__":
    unittest.main()
