"""Isolated, deterministic tests of the forward-only T practice books."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AQ5_DATA"] = tempfile.mkdtemp(dir=os.environ["AQ5_DATA"])
os.environ["AQ5_MOCK"] = "0"

from aq5 import db, intraday, market, training  # noqa: E402
from aq5.config import CN_TZ  # noqa: E402


class TrainingTests(unittest.TestCase):
    code = "sh600000"
    other = "sz000001"
    weekday = datetime(2026, 11, 9, 9, 30, tzinfo=CN_TZ)
    saturday = datetime(2026, 11, 7, 14, 30, tzinfo=CN_TZ)
    tables = ("training_marks", "training_observations", "training_orders",
              "training_cycles", "training_books", "training_runs")

    def setUp(self):
        db.init()
        training.init()
        with db.tx() as c:
            for table in self.tables + ("accounts", "trades", "orders", "positions", "equity", "plan"):
                c.execute(f"DELETE FROM {table}")

    def cfg(self, qty=100, cash=1000, other=False):
        holdings = [{"code": self.code, "qty": qty, "price": 21.8, "cost": 25}]
        if other:
            holdings.append({"code": self.other, "qty": 200, "price": 10, "cost": 12})
        return {"cash": cash, "holdings": holdings, "buys": [{"code": "sh600001", "qty": 100}],
                "started": "2026-11-03 13:14:41"}

    def configure(self, modes=("fast", "balanced", "slow"), **kwargs):
        result = training.start_from_plan(self.cfg(**kwargs), asof=self.saturday)
        if modes:
            training.arm_paper(modes)
        return result

    def quote(self, t, price, stamp=None, source="test-provider", **kwargs):
        return {"price": price, "time": (stamp or t).strftime("%Y%m%d%H%M%S"), "source": source,
                "received_at": t.isoformat(), "up_limit": 15.0, "dn_limit": 8.0, **kwargs}

    def sig(self, t, quote, code, variant, side, op, **kwargs):
        price = quote["price"]
        zone = [price - 0.2, price + 0.2]
        target = price + (0.7 if side == "buy" else -0.7)
        invalidation = price + (-0.3 if side == "buy" else 0.3)
        return {"version": intraday.VERSION, "code": code, "variant": variant, "side": side, "phase": "confirmed",
                "setup": "range_edge", "opportunity_id": op, "revision": 1, "quote_time": quote["time"],
                "evaluated_at": t.isoformat(), "expires_at": (t + timedelta(minutes=2)).isoformat(),
                "entry_zone": zone, "target": target, "invalidation": invalidation,
                "features": {"source": quote["source"], "market": {"risk_off": False}}, **kwargs}

    def step(self, seconds, price, side=None, op=None, *, day=None, stamp=None, source="test-provider",
             quote_overrides=None, signal_overrides=None, other=False, variants=None):
        t = (day or self.weekday) + timedelta(seconds=seconds)
        quote = self.quote(t, price, stamp=stamp, source=source)
        quote.update(quote_overrides or {})
        quotes = {self.code: quote}
        if other:
            quotes[self.other] = self.quote(t, 10.0)
        signals = {}
        if side:
            signals[self.code] = {}
            for variant in variants or training.VARIANTS:
                sig = self.sig(t, quote, self.code, variant, side, op)
                sig.update(signal_overrides or {})
                signals[self.code][variant] = sig
        return training.tick(signals, quotes, asof=t)

    def book(self, variant="fast"):
        return training.view()["experiments"][variant]

    def test_register_original_config_idempotently_and_do_not_trade_saturday(self):
        with db.tx() as c:
            c.execute("INSERT INTO plan(id,created,cfg,mode) VALUES(1,?,?,?)",
                      ("2026-11-03 13:14:41", db.dumps(self.cfg()), "t_trade"))
            c.execute("INSERT INTO accounts(id,name,cash,init_cash) VALUES('plan','Original',200,400)")
        plan_before = db.q("SELECT * FROM plan")
        accounts_before = db.q("SELECT * FROM accounts")
        first = training.start_from_plan(asof=self.saturday)
        self.assertEqual(first["status"], "awaiting_session")
        self.assertEqual(training.start_from_plan(asof=self.saturday)["run_id"], first["run_id"])
        self.assertEqual(training.codes(), [self.code])
        training.arm_paper(["fast"])
        t = self.saturday
        s = training.tick({}, {self.code: self.quote(t, 11)}, asof=t)
        self.assertEqual(s["reason"], "out_of_continuous_session")
        self.assertIsNone(training.view()["opened_at"])
        self.assertEqual(db.q("SELECT * FROM accounts"), accounts_before)
        self.assertEqual(db.q("SELECT * FROM plan"), plan_before)
        self.assertEqual(db.q("SELECT * FROM trades"), [])
        self.assertEqual(db.q("SELECT * FROM training_orders"), [])

    def test_all_opening_marks_are_required_and_freeze_equal_baseline(self):
        self.configure(other=True)
        stale = self.weekday - timedelta(days=3)
        s = self.step(0, 11, "sell", "one", stamp=stale)
        self.assertEqual(s["status"], "awaiting_session")
        self.assertEqual(s["blocked"], {"stale_data": 1, "missing_data": 1})
        s = self.step(3, 11, "sell", "one", other=True)
        self.assertEqual(s["reason"], "opening_allocation_frozen")
        marks = [self.book(v) for v in training.VARIANTS]
        self.assertEqual([b["opening_equity"] for b in marks], [4100] * 3)
        self.assertEqual([b["valuation"]["excess"] for b in marks], [0] * 3)
        self.assertEqual([b["opportunities"]["confirmed"] for b in marks], [1] * 3)
        self.assertEqual(db.q("SELECT * FROM training_orders"), [])
        self.assertEqual(training.view()["ignored_opening_buys"], 1)

    def test_shadow_observes_without_orders_and_cannot_change_mode_after_opening(self):
        self.configure(modes=())
        self.step(0, 11)
        self.step(3, 11, "sell", "one")
        self.assertEqual(self.book()["mode"], "shadow")
        self.assertEqual(self.book()["opportunities"]["decisions"]["shadow"], 1)
        self.assertEqual(self.book()["orders"], {})
        with self.assertRaises(ValueError):
            training.arm_paper(["fast"])

    def test_no_mock_quote_can_open_paper(self):
        self.configure()
        with patch.object(market, "MOCK", True):
            s = self.step(0, 11)
        self.assertEqual(s["status"], "awaiting_session")
        self.assertEqual(s["blocked"], {"mock_feed_disabled": 1})

    def test_both_legs_require_later_fresh_quote_and_include_both_fees(self):
        self.configure()
        self.step(0, 11)
        self.step(3, 11, "sell", "sale")
        self.assertEqual(self.book()["orders"], {"pending": 1})
        self.assertEqual(self.step(3, 11, "sell", "sale")["reason"], "duplicate_or_out_of_order")
        self.step(4, 11, "sell", "sale", stamp=self.weekday + timedelta(seconds=3))
        self.assertEqual(self.book()["orders"], {"pending": 1})
        self.step(6, 11, "sell", "sale")
        self.assertEqual(self.book()["orders"], {"filled": 1})
        self.assertEqual(self.book()["open_risk"]["cycles"], 1)
        self.step(9, 10, "buy", "buyback")
        self.assertEqual(self.book()["open_risk"]["cycles"], 1)
        self.assertEqual(self.book()["orders"], {"filled": 1, "pending": 1})
        self.step(12, 10, "buy", "buyback")
        rows = db.q("SELECT side,qty,fill_price_fen,fee_fen,cycle_id FROM training_orders "
                    "WHERE variant='fast' ORDER BY id")
        self.assertEqual([r["side"] for r in rows], ["sell", "buy"])
        self.assertEqual(rows[0]["cycle_id"], rows[1]["cycle_id"])
        expected = ((rows[0]["fill_price_fen"] - rows[1]["fill_price_fen"]) * 100
                    - sum(r["fee_fen"] for r in rows)) / 100
        book = self.book()
        self.assertEqual(book["closed_cycles"], {"count": 1, "net_after_fees": expected, "completion_rate": 1.0})
        self.assertEqual(book["valuation"]["excess"], expected)
        self.assertEqual(book["open_risk"]["cycles"], 0)
        self.assertEqual(db.q("SELECT * FROM trades"), [])
        self.assertEqual(db.q("SELECT * FROM orders"), [])

    def test_t1_cooldown_and_open_cycle_precedence(self):
        self.configure()
        self.step(0, 11)
        self.step(3, 11, "sell", "sale")
        self.step(6, 11, "sell", "sale")
        self.step(7, 11, "sell", "second-sale")
        self.assertEqual(self.book()["opportunities"]["blocked_reasons"]["open_cycle_priority"], 1)
        self.step(9, 10, "buy", "buyback")
        self.step(12, 10, "buy", "buyback")
        self.step(30, 11, "sell", "too-soon")
        self.step(200, 11, "sell", "locked")
        self.assertEqual(self.book()["opportunities"]["blocked_reasons"]["cooldown"], 1)
        self.assertEqual(self.book()["opportunities"]["blocked_reasons"]["insufficient_sellable"], 1)
        next_day = self.weekday + timedelta(days=1)
        self.step(1, 11, "sell", "fresh-next-day", day=next_day)
        self.assertEqual(self.book()["orders"]["pending"], 1)
        self.assertEqual(len(self.book()["curve"]), 2)

    def test_signal_without_sell_first_does_not_buy_and_cannot_borrow_cash(self):
        self.configure(cash=0)
        self.step(0, 11)
        self.step(1, 10, "buy", "wrong")
        self.step(3, 11, "sell", "sale")
        self.step(6, 11, "sell", "sale")
        self.step(9, 12, "buy", "unaffordable")
        book = self.book()
        self.assertEqual(book["opportunities"]["blocked_reasons"]["no_open_cycle"], 1)
        self.assertEqual(book["opportunities"]["blocked_reasons"]["insufficient_cash"], 1)
        self.assertEqual(book["orders"], {"filled": 1})
        self.assertGreater(book["cash"], 0)
        self.assertEqual(book["open_risk"]["cycles"], 1)
        self.assertLess(book["open_risk"]["net_if_restored_at_mark"], 0)

    def test_restore_estimate_includes_buy_slippage_without_rewriting_market_equity(self):
        self.configure()
        self.step(0, 11)
        self.step(3, 11, "sell", "sale")
        self.step(6, 11, "sell", "sale")
        self.step(9, 10)
        sale = db.q1("SELECT sold_fen FROM training_cycles WHERE variant='fast'")
        book = self.book()
        self.assertEqual(book["open_risk"]["restore_cost_at_mark"], 1006.01)
        self.assertEqual(book["open_risk"]["net_if_restored_at_mark"], (sale["sold_fen"] - 100601) / 100)
        self.assertEqual(book["valuation"]["excess"], (sale["sold_fen"] - 100000) / 100)
        self.assertIsNone(book["closed_cycles"]["net_after_fees"])

    def test_insufficient_features_count_as_data_blocker(self):
        self.configure()
        self.step(0, 11)
        self.step(3, 11, "sell", "warming", signal_overrides={
            "phase": "unknown", "reason": "macd_warming_up", "missing": ["completed_m1"],
        })
        self.assertEqual(self.book()["opportunities"]["blocked_reasons"], {"missing_data": 1})
        self.assertEqual(self.book()["orders"], {})

    def test_limits_zone_and_defensive_regime_block_pending(self):
        self.configure()
        self.step(0, 11)
        self.step(3, 11, "sell", "sale")
        self.step(6, 11, "sell", "sale", quote_overrides={"dn_limit": 11.0})
        self.assertEqual(self.book()["orders"], {"pending": 1})
        self.assertEqual(self.book()["opportunities"]["blocked_reasons"]["limit_locked"], 1)
        self.step(9, 10.73, "sell", "sale")
        self.assertEqual(self.book()["opportunities"]["blocked_reasons"]["outside_zone"], 1)
        self.step(12, 11, "sell", "sale",
                  signal_overrides={"features": {"market": {"risk_off": True}, "source": "test-provider"}})
        self.assertEqual(self.book()["orders"], {"invalid": 1})
        self.assertEqual(self.book()["opportunities"]["blocked_reasons"]["market_defense"], 1)
        self.step(15, 11, "sell", "risk", signal_overrides={"setup": "risk_exit"})
        self.assertEqual(self.book()["opportunities"]["blocked_reasons"]["risk_exit"], 1)

    def test_pending_order_expires_at_lunch_without_fill(self):
        self.configure()
        self.step(0, 11)
        self.step(2 * 3600 - 1, 11, "sell", "end-of-morning")
        self.assertEqual(self.book()["orders"], {"pending": 1})
        t = self.weekday + timedelta(hours=2)
        training.tick(asof=t)
        self.assertEqual(self.book()["orders"], {"unfilled": 1})
        self.assertEqual(self.book()["opportunities"]["blocked_reasons"]["expired"], 1)
        self.assertFalse(self.book()["valuation"]["matches_last_tick"])

    def test_variant_thresholds_use_one_feed_without_shared_trades(self):
        self.configure()
        self.step(0, 10.2)
        terms = {"entry_zone": [10.18, 10.22], "target": 10.066, "invalidation": 10.25}
        self.step(3, 10.2, "sell", "marginal", signal_overrides=terms)
        self.assertEqual(self.book("fast")["orders"], {"pending": 1})
        for variant in ("balanced", "slow"):
            self.assertEqual(self.book(variant)["orders"], {})
            self.assertEqual(self.book(variant)["opportunities"]["blocked_reasons"]["insufficient_net_edge"], 1)
        self.step(6, 10.2, "sell", "marginal", signal_overrides=terms)
        self.assertEqual(self.book("fast")["orders"], {"filled": 1})
        self.assertEqual(self.book("balanced")["open_risk"]["cycles"], 0)
        self.assertEqual(self.book("slow")["cash"], 1000.0)

    def test_new_source_or_old_quote_cannot_fill(self):
        self.configure()
        self.step(0, 11)
        self.step(3, 11, "sell", "sale")
        self.step(20, 11, "sell", "sale", stamp=self.weekday + timedelta(seconds=3))
        self.assertEqual(self.book()["opportunities"]["blocked_reasons"]["stale_data"], 1)
        self.step(23, 11, "sell", "sale", source="other-provider")
        self.assertEqual(self.book()["orders"], {"invalid": 1})
        self.assertEqual(self.book()["opportunities"]["blocked_reasons"]["opportunity_or_source_changed"], 1)

    def test_risk_estimate_is_null_if_current_marks_incomplete(self):
        self.configure(other=True)
        self.step(0, 11, other=True)
        self.step(3, 11, "sell", "sale", other=True)
        self.step(6, 11, "sell", "sale")  # one fresh order quote, but incomplete portfolio mark
        book = self.book()
        self.assertEqual(book["open_risk"]["cycles"], 1)
        self.assertIsNone(book["open_risk"]["restore_cost_at_mark"])
        self.assertFalse(book["valuation"]["matches_last_tick"])

    def test_out_of_order_tick_cannot_change_training_state(self):
        self.configure()
        self.step(3, 11)
        before = training.view()
        result = self.step(2, 11, "sell", "old")
        self.assertEqual(result["reason"], "duplicate_or_out_of_order")
        self.assertEqual(training.view(), before)

    def test_opening_inventory_cannot_exceed_shared_stream_capacity(self):
        cfg = self.cfg()
        cfg["holdings"] = [{"code": f"sh600{i:03}", "qty": 100} for i in range(training.MAX_CODES + 1)]
        with self.assertRaisesRegex(ValueError, "40"):
            training.start_from_plan(cfg, asof=self.saturday)
        self.assertEqual(training.view()["status"], "unconfigured")
        cfg["holdings"].pop()
        training.start_from_plan(cfg, asof=self.saturday)
        self.assertEqual(len(training.codes()), training.MAX_CODES)

    def test_non_lot_and_duplicate_opening_inventory_is_rejected(self):
        with self.assertRaises(ValueError):
            training.start_from_plan(self.cfg(qty=55), asof=self.saturday)
        cfg = self.cfg()
        cfg["holdings"].append(dict(cfg["holdings"][0]))
        with self.assertRaises(ValueError):
            training.start_from_plan(cfg, asof=self.saturday)
        self.assertEqual(training.view()["status"], "unconfigured")


if __name__ == "__main__":
    unittest.main()
