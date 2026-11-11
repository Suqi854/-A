"""Synthetic counterexamples for research state; never profitability evidence."""
import copy
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ["AQ5_DATA"] = os.environ["AQ5_TEST_DATA"]
os.environ["AQ5_MOCK"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aq5 import db, decision as d, decision_evidence as e, research
from aq5.config import CN_TZ

AT = datetime(2026, 11, 11, 14, 0, tzinfo=CN_TZ)
CODE = "sh600000"
iso = lambda t: t.isoformat(timespec="seconds")


def records(code=CODE):
    return [{"code": code, "kind": k, "value": True, "note": "合成测试证据 " + k,
             "published_at": iso(AT-timedelta(days=1)), "available_at": iso(AT-timedelta(days=1)),
             "expires_at": iso(AT+timedelta(days=2)), "source_url": "https://www.sse.com.cn/disclosure/test",
             "basis": "comparable_quarter_yoy",
             "metrics": {"revenue_yoy": 30, "prior_revenue_yoy": 20, "profit_yoy": 40, "prior_profit_yoy": 30}}
            for k in ("growth", "catalyst", "mainline", "core", "cooling", "hot_money")]


def fixture():
    dt = iso(AT)
    center = lambda lo, hi, i: dict(id=str(i), low=lo, high=hi, confirmed=True, start=dt, end=dt, confirmed_at=dt)
    frames = {CODE+":"+tf: {"code": CODE, "timeframe": tf, "asof": dt, "quality": {"eligible": True}, "structures": {
        "centers": [center(9, 9.5, 1), center(11, 12, 2)],
        "segments": [{"direction": "up", "end": dt, "confirmed_at": dt, "confirmed": True}],
        "buy_sell_points": [{"at": dt, "confirmed_at": dt, "confirmed": True, "side": "buy"}]}}
        for tf in ("day", "60m", "15m", "5m")}
    return {"structures": frames, "quote": {"code": CODE, "price": 10, "source_time": dt},
            "last_completed_close": {"price": 10},
            "decision_price_basis": {"verified": True, "a": 1, "b": 0, "date": "2026-11-11"}}


class DecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init(); e.init(); d.init()

    def setUp(self):
        with db.tx() as c:
            c.execute("DELETE FROM decision_targets"); c.execute("DELETE FROM decision_evidence")
            c.execute("DELETE FROM decision_observations")
        with patch.object(e, "now", return_value=AT):
            e.import_records(records())
        self.forecast = fixture()

    def assess(self, **kwargs):
        return d.assess(CODE, self.forecast["quote"], self.forecast, AT, **kwargs)["routes"]["emotion"]

    def saved(self):
        p = self.assess()["proposal"]
        return {**p, "confirmed_at": iso(AT-timedelta(minutes=1))}

    def test_observe_then_confirm_then_participate(self):
        self.assertEqual(self.assess()["state"], "await_target")
        self.assertEqual(self.assess(saved={"emotion": self.saved()})["state"], "participate")

    def test_missing_hot_money_is_not_an_entry_veto(self):
        claims = e.latest(AT)[CODE]; claims.pop("hot_money")
        result = self.assess(claims=claims, saved={"emotion": self.saved()})
        self.assertEqual(result["state"], "participate")
        self.assertNotIn("游资参与", result["missing"])
        self.assertFalse(result["hot_money_bonus"])

    def test_future_virtual_and_old_point_do_not_trigger(self):
        for field, value in (("confirmed", False), ("confirmed_at", iso(AT+timedelta(minutes=1))),
                             ("at", iso(AT-timedelta(days=1)))):
            with self.subTest(field=field):
                f = copy.deepcopy(self.forecast)
                f["structures"][CODE+":15m"]["structures"]["buy_sell_points"][0][field] = value
                result = d.assess(CODE, f["quote"], f, AT, saved={"emotion": self.saved()})
                self.assertNotEqual(result["routes"]["emotion"]["state"], "participate")

    def test_conflicting_higher_structure_blocks_entry(self):
        self.forecast["structures"][CODE+":60m"]["structures"]["segments"][0]["direction"] = "down"
        self.assertNotEqual(self.assess(saved={"emotion": self.saved()})["state"], "participate")

    def test_target_reached_does_not_wait_for_sell_point(self):
        saved = self.saved(); self.forecast["quote"]["price"] = 11.1
        result = self.assess(saved={"emotion": saved})
        self.assertEqual(result["state"], "take_profit")
        self.assertEqual(result["target_zone"], [11,12])

    def test_stop_exit_before_target(self):
        saved = self.saved(); self.forecast["quote"]["price"] = 8.9
        self.assertEqual(self.assess(saved={"emotion": saved})["state"], "exit")

    def test_stale_quote_and_basis_change_do_not_fire_price_exit(self):
        saved = self.saved(); self.forecast["quote"]["price"] = 8
        self.forecast["quote"]["source_time"] = iso(AT-timedelta(days=1))
        self.assertNotEqual(self.assess(saved={"emotion": saved})["state"], "exit")
        self.forecast["quote"]["source_time"] = iso(AT)
        self.forecast["decision_price_basis"]["a"] = 0.8
        self.assertNotEqual(self.assess(saved={"emotion": saved})["state"], "exit")

    def test_invalid_logic_exits_even_with_missing_quotes(self):
        claims = e.latest(AT)[CODE]
        claims["mainline"] = {**claims["mainline"], "value": False}
        self.forecast["quote"] = {}
        self.assertEqual(self.assess(claims=claims)["state"], "exit")

    def test_core_loss_cannot_silently_change_into_early_entry(self):
        saved = self.saved(); claims = e.latest(AT)[CODE]
        claims["core"] = {**claims["core"], "value": False}
        self.assertEqual(self.assess(claims=claims, saved={"emotion": saved})["state"], "exit")

    def test_wrong_stock_quote_cannot_trigger(self):
        saved = self.saved(); self.forecast["quote"].update(code="sz000001", price=8)
        self.assertNotEqual(self.assess(saved={"emotion": saved})["state"], "exit")

    def test_confirmation_accepts_server_id_not_roundtrip_float_hash(self):
        self.forecast["decision_price_basis"]["a"] = 1.000000000000001
        with patch.object(d, "now", return_value=AT), patch.object(d, "load_forecast", return_value=self.forecast):
            proposal = d.view(CODE)["routes"]["emotion"]["proposal"]
            result = d.confirm_target({"code": CODE, "route": "emotion", "proposal_id": proposal["id"]})
            self.assertEqual(result["basis"]["a"], 1.000000000000001)

    def test_expired_evidence_does_not_revive_older_true(self):
        with db.tx() as c:
            c.execute("UPDATE decision_evidence SET expires_at=? WHERE kind='mainline'", (iso(AT),))
        self.assertFalse(self.assess()["eligible"])

    def test_old_geometry_not_current_structure(self):
        self.forecast["structures"][CODE+":15m"]["asof"] = iso(AT-timedelta(hours=1))
        self.assertEqual(self.assess()["state"], "watch")

    def test_no_longer_cooling_does_not_exit_an_existing_thesis(self):
        saved = self.saved(); claims = e.latest(AT)[CODE]
        claims["cooling"] = {**claims["cooling"], "value": False}
        result = self.assess(claims=claims, saved={"emotion": saved})
        self.assertEqual(result["state"], "track")
        self.assertFalse(result["invalid"])

    def test_repeated_auto_acquisition_preserves_semantic_evidence_id(self):
        row = records()[1]
        row["note"] = "自动采集同一事件"
        row["provenance"] = {"provider":"test", "raw_sha256":"a"*64, "retrieved_at":iso(AT),
                             "rule_version":"test-1", "method":"public event"}
        with patch.object(e,"now",return_value=AT+timedelta(seconds=2)):
            e.import_records([row],automated=True)
        first = e.latest(AT+timedelta(seconds=2))[CODE]["catalyst"]
        row["provenance"].update(retrieved_at=iso(AT+timedelta(seconds=3)),raw_sha256="b"*64)
        with patch.object(e,"now",return_value=AT+timedelta(seconds=4)):
            self.assertEqual(e.import_records([row],automated=True)["inserted"],0)
        self.assertEqual(first,e.latest(AT+timedelta(seconds=4))[CODE]["catalyst"])

    def test_failed_new_observation_masks_old_auto_but_not_manual_evidence(self):
        row = records()[1]
        row["note"] = "自动事件"
        row["provenance"] = {"provider":"test", "raw_sha256":"a"*64, "retrieved_at":iso(AT),
                             "rule_version":"test-1", "method":"public event"}
        with patch.object(e,"now",return_value=AT+timedelta(seconds=1)):
            result=e.import_records([row],automated=True)
            e.record_observation([CODE],{CODE:result["ids"]})
        self.assertTrue(e.latest(AT+timedelta(seconds=1))[CODE]["catalyst"]["value"])
        with patch.object(e,"now",return_value=AT+timedelta(seconds=2)):
            e.record_observation([CODE],{})
        self.assertIsNone(e.latest(AT+timedelta(seconds=2))[CODE]["catalyst"]["value"])
        self.assertTrue(e.latest(AT+timedelta(seconds=2))[CODE]["growth"]["value"])
        self.assertTrue(e.latest(AT+timedelta(seconds=1))[CODE]["catalyst"]["value"])

    def test_bonus_or_acquisition_order_does_not_rewrite_target_dependencies(self):
        claims=e.latest(AT)[CODE]
        first=e.classify(CODE,claims)["emotion"]["evidence_ids"]
        claims["hot_money"]={**claims["hot_money"],"id":"different-bonus-id"}
        second=e.classify(CODE,dict(reversed(list(claims.items()))))["emotion"]["evidence_ids"]
        self.assertEqual(first,second)

    def test_no_known_price_basis_no_target(self):
        self.forecast.pop("decision_price_basis")
        self.assertIsNone(self.assess()["proposal"])

    def test_confirm_idempotent_cas_and_history(self):
        candidate = self.assess()["proposal"]
        body = {"code": CODE, "route": "emotion", "proposal": candidate, "previous_id": None}
        with patch.object(d, "now", return_value=AT), patch.object(d, "load_forecast", return_value=self.forecast):
            first = d.confirm_target(body)
            self.assertEqual(d.confirm_target(body)["id"], first["id"])
            f = copy.deepcopy(self.forecast)
            f["structures"][CODE+":day"]["structures"]["centers"][-1]["high"] = 13
            new = d.assess(CODE, f["quote"], f, AT)["routes"]["emotion"]["proposal"]
            with patch.object(d, "load_forecast", return_value=f):
                with self.assertRaises(ValueError):
                    d.confirm_target({**body, "proposal": new})
                d.confirm_target({**body, "proposal": new, "previous_id": first["id"]})
        self.assertEqual(len(db.q("SELECT * FROM decision_targets")), 2)
        self.assertEqual(db.loads(db.q1("SELECT body FROM decision_targets WHERE id=?", (first["id"],))["body"])["target_zone"], [11,12])

    def test_tampered_target_and_stale_confirm_rejected(self):
        candidate = self.assess()["proposal"]
        with patch.object(d, "now", return_value=AT), patch.object(d, "load_forecast", return_value=self.forecast):
            bad = {**candidate, "stop": 1}
            with self.assertRaises(ValueError):
                d.confirm_target({"code": CODE, "route": "emotion", "proposal": bad})
        with patch.object(d, "now", return_value=AT+timedelta(hours=1)), patch.object(d, "load_forecast", return_value=self.forecast):
            with self.assertRaises(ValueError):
                d.confirm_target({"code": CODE, "route": "emotion", "proposal": candidate})

    def test_core_priority_beats_low_cap_and_two_branches(self):
        other = records("sz000001")
        other = [r for r in other if r["kind"] != "core"]
        with patch.object(e, "now", return_value=AT): e.import_records(other)
        result = e.screen([{"code": CODE, "mcap": 1000}, {"code": "sz000001", "mcap": 30}], AT)
        candidates = result["emotion"]["candidates"]
        self.assertEqual([x["code"] for x in candidates], [CODE, "sz000001"])
        self.assertEqual(candidates[0]["branch"], "核心回调")
        self.assertIn("潜在", candidates[1]["branch"])

    def test_future_import_bad_growth_and_conflict_rejected_atomically(self):
        for change in ({"available_at": iso(AT+timedelta(hours=1))}, {"basis": "ttm"},
                       {"metrics": {"revenue_yoy": float("nan")}}, {"value": "true"}):
            with self.subTest(change=change), patch.object(e, "now", return_value=AT), self.assertRaises(ValueError):
                e.import_records([{**records()[0], **change}])
        self.assertEqual(len(db.q("SELECT * FROM decision_evidence")), 6)

    def test_import_does_not_backdate_and_read_path_does_not_write(self):
        self.assertEqual(e.latest(AT-timedelta(seconds=1)), {})
        before = '\n'.join(db.conn().iterdump())
        with patch.object(db, "tx", side_effect=AssertionError("read must not write")):
            self.assess(); e.screen([{"code": CODE}], AT)
        self.assertEqual(before, '\n'.join(db.conn().iterdump()))

    def test_merge_preserves_input_and_clears_legacy_fixed_hold(self):
        a = {"plan": {"hold_days": 20, "target1": 99, "target2": 100}, "score": 80}
        fused = d.assess(CODE, self.forecast["quote"], self.forecast, AT)
        merged = d.merge(a, fused)
        self.assertEqual(a["plan"]["target1"], 99)
        self.assertIsNone(merged["plan"]["hold_days"])
        self.assertEqual(merged["plan"]["target1"], 11)


if __name__ == "__main__": unittest.main()
