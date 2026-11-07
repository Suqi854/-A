"""Isolated regression tests for the ordinary and plan simulation order guards."""
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

# Require an explicit scratch location, never the default engine data directory.
os.environ["AQ5_DATA"] = tempfile.mkdtemp(dir=os.environ["AQ5_DATA"])
os.environ["AQ5_MOCK"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aq5 import db, market, plan, sim  # noqa: E402
from aq5.config import now, today  # noqa: E402


class ExecutionGuards(unittest.TestCase):
    code = "sh600000"
    quote = {"name": "Test stock", "price": 10.0}
    reason = "计划·持仓做T：测试"

    def setUp(self):
        db.init()
        with db.tx() as conn:
            for table in ("orders", "trades", "positions", "accounts"):
                conn.execute(f"DELETE FROM {table}")
        sim.ensure_accounts()
        with db.tx() as conn:
            conn.execute("INSERT INTO accounts(id, name, cash) VALUES('plan', 'Test plan', 100000)")
        trade_hours = patch.object(sim, "can_trade_now", return_value=True)
        quotes = patch.object(market, "quotes", return_value={self.code: self.quote})
        trade_hours.start()
        quotes.start()
        self.addCleanup(trade_hours.stop)
        self.addCleanup(quotes.stop)

    def seed_old_position(self, account, qty):
        opened = (now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        with db.tx() as conn:
            conn.execute("INSERT INTO positions(account, code, name, qty, cost, opened) VALUES(?,?,?,?,?,?)",
                         (account, self.code, self.quote["name"], qty, 10.0, opened))

    def order(self, oid):
        return db.q1("SELECT * FROM orders WHERE id=?", (oid,))

    def test_pending_sell_cannot_fill_using_new_shares(self):
        for account in ("me", "plan"):
            with self.subTest(account=account):
                self.seed_old_position(account, 200)
                delayed = sim.place(account, self.code, "sell", 200, kind="limit", price=11.0)
                self.assertEqual(delayed["status"], "pending")
                self.assertEqual(sim.place(account, self.code, "buy", 100)["status"], "filled")
                self.assertEqual(sim.place(account, self.code, "sell", 100)["status"], "filled")
                self.assertEqual(sim.available(account, self.code), 100)
                sim.try_fill(delayed["id"], {**self.quote, "price": 11.0})
                rejected = self.order(delayed["id"])
                self.assertEqual(rejected["status"], "rejected")
                self.assertIn("T+1", rejected["note"])
                self.assertEqual(sim.available(account, self.code), 100)
                self.assertFalse(db.q1("SELECT 1 FROM trades WHERE order_id=?", (delayed["id"],)))

    def test_stale_pending_snapshot_cannot_sell_twice(self):
        self.seed_old_position("me", 300)
        delayed = sim.place("me", self.code, "sell", 100, kind="limit", price=11.0)
        sim.try_fill(delayed["id"], {**self.quote, "price": 11.0})
        sim._execute(delayed, 11.0)
        self.assertEqual(self.order(delayed["id"])["status"], "filled")
        self.assertEqual(sim.available("me", self.code), 200)
        self.assertEqual(len(db.q("SELECT 1 FROM trades WHERE order_id=?", (delayed["id"],))), 1)

    def test_rejected_plan_buy_does_not_count_as_success(self):
        with db.tx() as conn:
            conn.execute("UPDATE accounts SET cash=0 WHERE id='plan'")
        self.assertFalse(plan._buy({"code": self.code, "price": 10.0}, 2000, "test", "attack"))
        self.assertEqual(db.q1("SELECT status FROM orders WHERE account='plan'")["status"], "rejected")

    def seed_t_order(self, status, side, *, created, updated, qty=100):
        with db.tx() as conn:
            return conn.execute("INSERT INTO orders(account, code, side, qty, status, created, updated, reason)"
                                " VALUES('plan',?,?,?,?,?,?,?)",
                                (self.code, side, qty, status, created, updated, self.reason)).lastrowid

    def test_t_counts_fills_on_fill_day_only(self):
        yesterday = (now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        stamp = today() + " 10:00:00"
        self.seed_t_order("filled", "sell", created=yesterday, updated=stamp)
        self.assertEqual(plan._t_qty(self.code, "sell"), 100)
        self.seed_t_order("filled", "sell", created=yesterday, updated=yesterday)
        self.seed_t_order("pending", "sell", created=stamp, updated=stamp)
        self.seed_t_order("rejected", "buy", created=stamp, updated=stamp)
        self.assertEqual(plan._t_qty(self.code, "sell"), 100)
        self.assertEqual(plan._t_qty(self.code, "buy"), 0)

    def test_legacy_fill_without_updated_uses_linked_trade_time(self):
        yesterday = (now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        oid = self.seed_t_order("filled", "sell", created=yesterday, updated=None)
        with db.tx() as conn:
            conn.execute("INSERT INTO trades(account, code, side, qty, order_id, time)"
                         " VALUES('plan',?,'sell',100,?,?)", (self.code, oid, today() + " 10:00:00"))
        self.assertEqual(plan._t_qty(self.code, "sell"), 100)

    def test_pending_t_order_blocks_both_legs_until_resolved(self):
        stamp = today() + " 10:00:00"
        self.seed_t_order("filled", "sell", created=stamp, updated=stamp)
        yesterday = (now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        for pending_side in ("sell", "buy"):
            with self.subTest(pending_side=pending_side):
                oid = self.seed_t_order("pending", pending_side, created=yesterday, updated=yesterday)
                with patch.object(sim, "place") as place:
                    for side in ("sell", "buy"):
                        plan._t_order(self.code, side, 100, "做T测试", {}, "t_trade")
                    place.assert_not_called()
                    sim.cancel(oid)
                    plan._t_order(self.code, "buy", 100, "做T测试", {}, "t_trade")
                    place.assert_called_once()

    def submit_as_pending_fills(self, side, pending_qty, requested_qty):
        stamp = today() + " 10:00:00"
        if side == "buy":
            self.seed_t_order("filled", "sell", created=stamp, updated=stamp, qty=requested_qty)
        else:
            self.seed_old_position("plan", 300)
        limit = 9.0 if side == "buy" else 11.0
        delayed = sim.place("plan", self.code, side, pending_qty, kind="limit", price=limit, reason=self.reason)
        self.assertEqual(delayed["status"], "pending")
        self.assertEqual(plan._t_qty(self.code, side), 0)
        read = db.q1

        def fill_before_pending_lookup(sql, params=()):
            if sql.startswith("SELECT 1 FROM orders"):
                sim.try_fill(delayed["id"], {**self.quote, "price": limit})
            return read(sql, params)

        with patch.object(db, "q1", side_effect=fill_before_pending_lookup), patch.object(sim, "place") as place:
            plan._t_order(self.code, side, requested_qty, "做T测试", {}, "t_trade")
        self.assertEqual(self.order(delayed["id"])["status"], "filled")
        return place

    def test_pending_buy_filled_during_check_cannot_buy_twice(self):
        self.submit_as_pending_fills("buy", 200, 200).assert_not_called()

    def test_pending_buy_filled_during_check_caps_remaining_buyback(self):
        place = self.submit_as_pending_fills("buy", 100, 200)
        place.assert_called_once()
        self.assertEqual(place.call_args.args[:4], ("plan", self.code, "buy", 100))

    def test_pending_sell_filled_during_check_cannot_sell_twice(self):
        self.submit_as_pending_fills("sell", 100, 100).assert_not_called()

    def test_today_buyback_blocks_new_sale_even_if_first_leg_was_yesterday(self):
        yesterday = (now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        stamp = today() + " 10:00:00"
        self.seed_t_order("filled", "sell", created=yesterday, updated=yesterday)
        self.seed_t_order("filled", "buy", created=yesterday, updated=stamp)
        with patch.object(sim, "place") as place:
            plan._t_order(self.code, "sell", 100, "做T测试", {}, "t_trade")
        place.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)

