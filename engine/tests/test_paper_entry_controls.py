"""Exercise the active simulation entry points against an isolated ledger."""
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AQ5_DATA"] = tempfile.mkdtemp(prefix="paper-entry-tests-")
os.environ["AQ5_MOCK"] = "1"
from aq5 import config, db, hf, learn, marketstate, signals, sim
from aq5.forecast import runtime


class PaperEntryTests(unittest.TestCase):
    code = "sh600000"
    day = "2026-11-18"
    quote = {"name": "fixture", "price": 10.0}

    def setUp(self):
        db.init()
        with db.tx() as c:
            for table in ("positions", "trades", "orders", "accounts", "picks", "plan"):
                c.execute("DELETE FROM " + table)
        sim.ensure_accounts()
        hf._feedback.cache_clear()
        self.settings = config.load_settings()
        self.settings["strategy_v2"]["legacy_auto"] = True
        self.stack = self.enterContext(ExitStack())
        for module in (hf, sim, signals):
            self.stack.enter_context(patch.object(module, "today", return_value=self.day))
        self.stack.enter_context(patch.object(hf, "now", return_value=datetime(2026, 11, 18, 10, 0)))
        self.stack.enter_context(patch.object(hf, "load_settings", return_value=self.settings))
        self.stack.enter_context(patch.object(sim, "can_trade_now", return_value=True))

    def hf_tick(self, momentum, short=False, execute=False):
        f = {"n": 16, "p": 10, "vwap": 10.1 if short else 9.9, "m120": momentum,
             "m30": momentum, "m300": momentum, "imb": -.6 if short else .6,
             "pct": -1. if short else 1., "flow": 0, "dv": 0}
        cand = {"code": self.code, "name": "fixture", "score": 15 if short else 85}
        pool = {"long": [] if short else [cand], "short": [cand] if short else [], "t": hf.time.time()}
        with patch.object(hf, "_scan", pool), patch.object(hf, "_feat", return_value=f), \
             patch.object(hf.market, "quotes_fast", return_value={self.code: self.quote}), \
             patch.object(marketstate, "state", return_value={"stance": "中性"}), \
             patch.object(sim, "exposure", return_value={"equity": 100000, "gross": 0}):
            if execute:
                hf.tick()
                return db.q1("SELECT COUNT(*) AS n FROM trades WHERE account='ai'")["n"]
            with patch.object(sim, "place", return_value={"status": "filled"}) as place:
                hf.tick()
                return place.call_count

    def test_hf_cost_stops_and_independent_opportunity(self):
        self.assertEqual(self.hf_tick(.15), 0)
        self.assertEqual(self.hf_tick(.8), 1)
        with db.tx() as c:
            c.execute("INSERT INTO trades(account,code,side,time,qty,pnl,reason) VALUES('ai',?,'sell',?,100,-20,'高频·平多 止损 -1.1%')",
                      (self.code, self.day + " 09:40"))
        self.assertEqual(self.hf_tick(.8), 0)
        self.assertEqual(self.hf_tick(-.8, short=True), 1)
        with db.tx() as c:
            c.execute("UPDATE trades SET time='2026-11-17 14:20'")
        self.assertEqual(self.hf_tick(.8), 1)

    def test_t_requires_filled_leg_and_exclusive_owner(self):
        buy = [{"side": "buy", "strategy": "vwap_low"}]
        levels = {"price": 10, "t_low": 10, "t_high": 11}
        with db.tx() as c:
            c.execute("INSERT INTO orders(account,code,side,status,qty,reason,created) VALUES('ai',?,'sell','cancelled',600,'做T高抛',?)",
                      (self.code, self.day))
        with patch.object(sim, "place") as place:
            signals._ai_t(self.code, {}, buy, levels)
            place.assert_not_called()
            with db.tx() as c:
                c.execute("INSERT INTO trades(account,code,side,qty,reason,time) VALUES('ai',?,'sell',200,'做T高抛',?)",
                          (self.code,self.day))
            with patch.object(runtime, "owns_ai", return_value=True):
                signals._ai_t(self.code, {}, buy, levels)
            place.assert_not_called()
            signals._ai_t(self.code, {}, buy, levels)
            self.assertEqual(place.call_args.args[3], 200)

    def test_hf_cost_gate_reaches_the_actual_simulator(self):
        self.assertEqual(self.hf_tick(.15, execute=True), 0)
        self.assertEqual(self.hf_tick(.80, execute=True), 1)
        t = db.q1("SELECT reason,side,qty,fee FROM trades WHERE account='ai'")
        self.assertTrue(t["reason"].startswith("高频·做多"))
        self.assertEqual(t["side"], "buy")
        self.assertGreaterEqual(t["qty"],100)
        self.assertGreater(t["fee"],0)

    def history(self, date, number, *, profitable=False):
        with db.tx() as c:
            for i in range(number):
                code = "sh%06d" % (600200 + i)
                long = i % 2 == 0
                delta = (.2 if profitable else -.2) * (1 if long else -1)
                first = ("buy", "高频·做多") if long else ("sell", "高频·融券做空")
                last = ("sell", "高频·平多") if long else ("buy", "高频·平空")
                c.executemany("INSERT INTO trades(account,code,time,side,reason,qty,price,fee,pnl) "
                              "VALUES('ai',?,?,?,?,100,?,5,?)", [
                    (code, date+" 10:00:00", *first, 10, None),
                    (code, date+" 10:05:00", *last, 10+delta, (20 if profitable else -20)-5)])

    def test_previous_session_loss_restricts_actual_fills_for_one_day(self):
        self.history("2026-11-17", 15)
        with db.tx() as c:
            c.execute("INSERT INTO trades(account,code,time,side,reason,qty,price,fee,pnl) VALUES"
                      "('ai','sh600250','2026-11-17 13:30','sell','高频·平多',100,11,5,95)")
        baseline = hf._round_trip_pct(10, 400, self.settings)
        info = hf.status()["entry_feedback"]
        self.assertEqual((info["trading_day"],info["source_day"]), (self.day,"2026-11-17"))
        self.assertEqual((info["active"],info["matched"],info["closes"],info["net"]), (True,15,16,-450))
        self.assertEqual(hf.status()["entry_feedback"],info)
        self.assertEqual(self.hf_tick(baseline*1.05, execute=True),31)
        self.assertEqual(self.hf_tick(baseline*1.15, execute=True),32)
        self.assertEqual(learn.tune_hf("2026-11-19")["multiplier"],1)
        self.assertEqual(hf.cfg()["mom"],.12)

    def test_feedback_needs_enough_reconciled_pairs_and_never_loosens(self):
        self.history("2026-11-17",15)
        with db.tx() as c:
            c.execute("UPDATE trades SET fee=NULL WHERE code='sh600200' AND side='buy'")
        feedback = learn.tune_hf(self.day)
        self.assertEqual((feedback["active"],feedback["matched"],feedback["closes"]), (False,14,15))
        self.history("2026-11-13",16, profitable=True)
        monday = learn.tune_hf("2026-11-16")
        self.assertEqual((monday["source_day"],monday["multiplier"],monday["net"]),("2026-11-13",1,160))
        self.assertFalse(learn.tune_hf("2026-11-15")["active"])

    def test_attribution_uses_reconciled_inventory_and_actual_opening_fees(self):
        from aq5 import learn
        with db.tx() as c:
            pid = c.execute("INSERT INTO picks(code,strategies,session) VALUES(?,?,?)",
                            (self.code,db.dumps([{"id":"fixture"}]),"auction")).lastrowid
        with patch.object(sim, "exposure", return_value={"equity": 1e6, "gross": 0}):
            entry = sim.place("ai", self.code, "buy", 300, qd=self.quote, pick_id=pid)
            sale = sim.place("ai", self.code, "sell", 100, qd={"price": 11}, reason="触发止损")
            trade = db.q1("SELECT * FROM trades WHERE order_id=?", (sale["id"],))
            self.assertEqual(trade["pick_id"], pid)
            self.assertAlmostEqual(trade["pnl"], (sale["fill_price"] - entry["fill_price"]) * 100
                                   - sale["fee"] - entry["fee"]/3, places=2)
            self.assertEqual(learn._realized()[0]["ids"], ["fixture"])
            sim.place("ai", self.code, "buy", 100, qd=self.quote)
            mixed = sim.place("ai", self.code, "sell", 100, qd=self.quote)
            self.assertIsNone(db.q1("SELECT pick_id FROM trades WHERE order_id=?", (mixed["id"],))["pick_id"])

    def test_entry_requires_protocol_and_eligibility(self):
        p = {"tier": "A", "eligible": True, "selection_protocol": "strict-a-v1"}
        self.assertTrue(sim.tradable_pick(p))
        for other in ({"tier": "B"}, {"eligible": False}, {"selection_protocol": None}):
            self.assertFalse(sim.tradable_pick({**p, **other}))

    def test_unfilled_protective_sale_cannot_multiply_or_flip_short(self):
        q = {"price": 9.0, "dn_limit": 9.0}
        with patch.object(sim, "exposure", return_value={"equity": 1e6, "gross": 0}), \
             patch.object(sim.market, "quotes", return_value={self.code: q}):
            sim.place("ai", self.code, "buy", 300, qd=self.quote, stop=9.1)
            sim.tick()
            sim.tick()
            pending = db.q("SELECT * FROM orders WHERE status='pending'")
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["qty"], 300)
            q["price"] = 9.05
            sim.tick()
            self.assertEqual(db.q("SELECT * FROM positions WHERE account='ai'"), [])
            self.assertEqual(len(db.q("SELECT * FROM trades WHERE account='ai'")), 2)

    def test_delayed_first_target_advances_stage_once(self):
        q = {"price": 11, "suspended": True}
        with patch.object(sim, "exposure", return_value={"equity": 1e6, "gross": 0}), \
             patch.object(sim.market, "quotes", return_value={self.code: q}):
            sim.place("ai", self.code, "buy", 400, qd=self.quote, target1=11)
            sim.tick()
            self.assertEqual(db.q1("SELECT stage FROM positions WHERE code=?", (self.code,))["stage"], 0)
            q["suspended"] = False
            sim.tick()
            sim.tick()
            pos = db.q1("SELECT qty,stage,stop,cost FROM positions WHERE code=?", (self.code,))
            self.assertEqual((pos["qty"],pos["stage"]), (200,1))
            self.assertAlmostEqual(pos["stop"], pos["cost"], places=2)


if __name__ == "__main__":
    unittest.main()
