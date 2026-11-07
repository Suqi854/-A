"""Release integration guards, using only explicitly assigned test data and mock quotes."""
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

os.environ["AQ5_MOCK"] = "1"
os.environ["AQ5_DATA"] = os.environ["AQ5_TEST_DATA"]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aq5 import db, live, market, picker, server, sim, training  # noqa: E402
from aq5.config import load_settings  # noqa: E402


class ReleaseIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        sim.ensure_accounts()

    def setUp(self):
        sim.reset("me")
        sim.reset("ai")

    def test_stale_order_cannot_fill_twice_in_either_simulator(self):
        for account, side, expected_qty in (("me", "buy", 100), ("ai", "sell", -100)):
            with self.subTest(account=account, side=side):
                with db.tx() as c:
                    oid = c.execute("""INSERT INTO orders(account,code,name,side,kind,qty,status,created,updated)
                                      VALUES(?,?,?,?,?,?,'pending',?,?)""",
                                    (account, "sh600001", "公开样本", side, "market", 100,
                                     "2026-11-06 10:00:00", "2026-11-06 10:00:00")).lastrowid
                old = db.q1("SELECT * FROM orders WHERE id=?", (oid,))
                self.assertTrue(sim._execute(old, 10).startswith("已成交"))
                cash_after_first = db.q1("SELECT cash FROM accounts WHERE id=?", (account,))["cash"]
                self.assertFalse(sim._execute(old, 10).startswith("已成交"))
                self.assertEqual(db.q1("SELECT COUNT(*) AS n FROM trades WHERE order_id=?", (oid,))["n"], 1)
                self.assertEqual(db.q1("SELECT qty FROM positions WHERE account=?", (account,))["qty"], expected_qty)
                self.assertEqual(db.q1("SELECT cash FROM accounts WHERE id=?", (account,))["cash"], cash_after_first)

    def test_live_public_payload_uses_only_public_codes_and_fields(self):
        pick, index, personal = "sh600001", "sh000300", "sh600998"
        quotes = {code: {"code": code, "name": "公开名称", "price": 10.5, "pct": 0, "time": "20261106150000",
                         "amount": 12000000, "cost": 888, "qty": 900, "token": "mock-private"}
                  for code in (pick, index, personal)}
        with patch.object(picker, "runs", return_value=[{"items": [{"code": pick, "private": "mock-private"}]}]), patch.object(
            live, "load_settings", return_value={"watchlist": [personal], "indices": [index]}
        ):
            result = live.public_snapshot(quotes)
        self.assertEqual(set(result), {"time", "version", "phase", "pick_quotes", "indices", "quote_time"})
        self.assertEqual(set(result["pick_quotes"]), {pick})
        self.assertEqual([q["code"] for q in result["indices"]], [index])
        self.assertEqual(set(result["pick_quotes"][pick]), {"code", "name", "price", "pct", "time", "chg", "amount"})
        self.assertNotIn("mock-private", repr(result))
        self.assertNotIn(personal, repr(result))

    def test_training_codes_are_kept_before_bounded_watchlist(self):
        required = ["sh605998", "sz003999"]
        watchlist = [f"sh600{i:03d}" for i in range(55)] + ["SH605998"]
        with patch.object(training, "codes", return_value=required), patch.object(
            picker, "runs", return_value=[{"items": [{"code": "sh600000"}]}]
        ), patch.object(live, "load_settings", return_value={"watchlist": watchlist}):
            actual = live.codes()
        self.assertEqual(actual[:2], required)
        self.assertEqual(len(actual), 40)
        self.assertEqual(len(set(actual)), 40)

    def test_default_ai_account_does_not_place_legacy_selection_orders(self):
        self.assertIn("ai", sim.existing())
        self.assertFalse(load_settings()["strategy_v2"]["legacy_auto"])
        before = db.q1("SELECT COUNT(*) AS n FROM orders WHERE account='ai'")["n"]
        picks = [{"code": "sh600000", "tier": "A", "eligible": True},
                 {"code": "sh600001", "tier": "C", "eligible": False}]
        with patch.object(sim, "place") as place:
            sim.auto_follow(picks, "auction")
            sim.auto_follow(picks, "close")
            place.assert_not_called()
        self.assertEqual(db.q1("SELECT COUNT(*) AS n FROM orders WHERE account='ai'")["n"], before)

    def test_overview_falls_back_to_public_pick_quotes_if_snapshot_fails(self):
        code = "sh600001"
        quote = {"code": code, "price": 10.5, "pct": 0, "time": "20261106150000"}
        picks = [{"id": 1, "items": [{"code": code, "price": 10}]}]
        with patch.object(market, "snapshot", side_effect=RuntimeError("snapshot offline")), patch.object(
            market, "quotes", return_value={}
        ), patch.object(market, "quotes_recent", return_value={code: quote}) as direct, patch.object(
            picker, "runs", return_value=picks
        ), patch.object(server.analysis, "regime", return_value={}), patch.object(
            server, "lan_ip", return_value="127.0.0.1"
        ):
            result = server.api_overview({})
        self.assertIn("error", result["breadth"])
        direct.assert_called_once_with([code])
        self.assertEqual(result["pick_quotes"], {code: quote})
        self.assertEqual(result["last_run"]["items"][0]["price"], 10)
        self.assertIn("ai", result["accounts"])

    def test_frozen_account_can_be_read_from_a_new_connection(self):
        expected = sim.view("me", light=True)
        with patch.object(db, "_writes_frozen", False):
            db.freeze_writes()
            with patch.object(sim, "ensure_accounts", side_effect=AssertionError("read must not write")):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    view = executor.submit(sim.view, "me").result(timeout=5)
            self.assertEqual(view["id"], "me")
            self.assertEqual(view["cash"], expected["cash"])
            self.assertEqual(view["performance"]["verified_equity"], expected["performance"]["verified_equity"])
            with self.assertRaises(db.StateFrozen):
                with db.tx():
                    self.fail("a frozen transaction must never reach the body")


if __name__ == "__main__":
    unittest.main()
