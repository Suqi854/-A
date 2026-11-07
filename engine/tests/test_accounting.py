"""Offline regression coverage for account valuation and realized trade statistics."""
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AQ5_DATA"] = tempfile.mkdtemp(dir=os.environ["AQ5_DATA"])
os.environ["AQ5_MOCK"] = "1"

from aq5 import db, market, sim  # noqa: E402
from aq5.config import CN_TZ  # noqa: E402


class AccountingTests(unittest.TestCase):
    day = "2026-11-06"
    prior = "2026-11-05"
    code = "sh600000"

    def setUp(self):
        db.init()
        sim.ensure_accounts()
        for acct in ("me", "ai"):
            sim.reset(acct)
        self.quotes = {}
        self.clock = "14:30:00"
        for target, attr, kwargs in (
            (sim, "today", {"side_effect": lambda: self.day}),
            (sim, "now", {"side_effect": self.now}),
            (sim, "can_trade_now", {"return_value": True}),
            (market, "quotes", {"side_effect": self.get_quotes}),
            (market, "quotes_fast", {"side_effect": self.get_quotes}),
            (market, "quotes_recent", {"side_effect": self.get_quotes}),
        ):
            mock = patch.object(target, attr, **kwargs)
            mock.start()
            self.addCleanup(mock.stop)

    def now(self):
        return datetime.fromisoformat(self.day + "T" + self.clock).replace(tzinfo=CN_TZ)

    def get_quotes(self, codes):
        return {code: self.quotes[code] for code in codes if code in self.quotes}

    def quote(self, price, prev=10.0, stamp=None):
        self.quotes[self.code] = {
            "name": "Synthetic security", "price": price, "prev": prev,
            "time": stamp if stamp is not None else self.now().strftime("%Y%m%d%H%M%S"),
        }

    def fill(self, acct, side, price, qty=100, day=None):
        """Use the normal fill path while keeping price and dates deterministic."""
        original = self.day
        self.day = day or original
        self.quote(price)
        try:
            order = sim.place(acct, self.code, side, qty, qd=self.quotes[self.code])
        finally:
            self.day = original
        self.assertEqual(order["status"], "filled")
        return order

    def pnl(self, acct):
        return sim.view(acct, light=True)["today_pnl"]

    def test_full_exit_keeps_todays_loss_and_sell_fee(self):
        self.fill("me", "buy", 9, day=self.prior)
        sale = self.fill("me", "sell", 8)
        self.quote(8.2)
        expected = (sale["fill_price"] - 10) * 100 - sale["fee"]
        self.assertEqual(self.pnl("me"), round(expected, 2))

    def test_first_buy_counts_post_fill_move_and_buy_fee(self):
        buy = self.fill("me", "buy", 10)
        self.quote(11, prev=None)
        self.assertEqual(self.pnl("me"), round((11 - buy["fill_price"]) * 100 - buy["fee"], 2))

    def test_sell_and_rebuy_uses_both_fees_and_preserves_opening_shares(self):
        self.fill("me", "buy", 9, day=self.prior)
        sale = self.fill("me", "sell", 12)
        buy = self.fill("me", "buy", 10)
        self.quote(11)
        expected = (11 - 10 + sale["fill_price"] - buy["fill_price"]) * 100 - sale["fee"] - buy["fee"]
        self.assertEqual(self.pnl("me"), round(expected, 2))

    def test_short_cover_counts_signed_opening_position(self):
        self.fill("ai", "sell", 10, day=self.prior)
        cover = self.fill("ai", "buy", 9)
        self.quote(8.8)
        expected = (10 - cover["fill_price"]) * 100 - cover["fee"]
        self.assertEqual(self.pnl("ai"), round(expected, 2))

    def test_remaining_short_uses_signed_current_mark(self):
        self.fill("ai", "sell", 10, day=self.prior)
        self.quote(9)
        self.assertEqual(self.pnl("ai"), 100.0)

    def test_same_day_flat_round_trip_requires_no_mark(self):
        sale = self.fill("ai", "sell", 10)
        buy = self.fill("ai", "buy", 9)
        self.quotes.clear()
        expected = (sale["fill_price"] - buy["fill_price"]) * 100 - sale["fee"] - buy["fee"]
        self.assertEqual(self.pnl("ai"), round(expected, 2))

    def test_missing_previous_close_after_sale_is_unknown(self):
        self.fill("me", "buy", 9, day=self.prior)
        self.fill("me", "sell", 10)
        self.quotes.clear()
        account = sim.view("me")
        self.assertIsNone(account["today_pnl"])
        self.assertEqual(account["daily_pnl_status"]["code"], "missing_previous_close")

    def test_stale_quote_and_unexplained_cash_do_not_report_zero(self):
        self.fill("me", "buy", 10)
        self.quote(10, stamp="20261105150000")
        account = sim.view("me")
        self.assertIsNone(account["today_pnl"])
        self.assertEqual(account["daily_pnl_status"]["code"], "missing_current_mark")
        self.quote(10)
        with db.tx() as c:
            c.execute("UPDATE accounts SET cash=cash+1200 WHERE id='me'")
        account = sim.view("me")
        self.assertIsNone(account["today_pnl"])
        self.assertEqual(account["daily_pnl_status"]["code"], "unreconciled_cash")

    def test_full_stats_include_old_closes_short_covers_and_breakeven(self):
        with db.tx() as c:
            rows = [("ai", "sell", 4.0, 2.0, 1), ("ai", "buy", -6.0, 3.0, 1),
                    ("ai", "sell", 0.0, 1.0, 1)] + [("ai", "buy", None, 1.0, 0)] * 201
            c.executemany("INSERT INTO trades(account, side, pnl, fee, hold_days) VALUES(?,?,?,?,?)", rows)
        account = sim.view("ai")
        self.assertEqual(len(account["trades"]), 200)
        stats = account["stats"]
        self.assertEqual(stats["sells"], 3)
        self.assertEqual(stats["sell_closes"], 2)
        self.assertEqual(stats["short_cover_closes"], 1)
        self.assertEqual((stats["wins"], stats["losses"], stats["breakeven"]), (1, 1, 1))
        self.assertEqual(stats["win_rate"], 0.333)
        self.assertEqual(stats["avg_loss"], -6.0)
        self.assertEqual(stats["fees"], 207.0)


    def test_same_date_old_quote_is_not_a_current_valuation(self):
        self.fill("me", "buy", 10, day=self.prior)
        self.quote(11, stamp="20261106093100")
        result = sim.view("me")
        self.assertIsNone(result["today_pnl"])
        self.assertIsNone(result["total_pnl"])
        self.assertIsNone(result["performance"]["valuation_time"])
        self.quote(11, stamp="20261106142945")
        result = sim.view("me")
        self.assertIsNotNone(result["today_pnl"])
        self.assertEqual(result["performance"]["valuation_time"], "2026-11-06T14:29:45+08:00")

    def test_quote_before_a_later_same_day_fill_is_unknown_even_inside_freshness_window(self):
        self.fill("me", "buy", 10)
        self.clock = "14:30:05"
        self.quote(11, stamp="20261106142959")
        result = sim.view("me")
        self.assertIsNone(result["today_pnl"])
        self.assertIsNone(result["total_pnl"])
        self.quote(11, stamp="20261106143002")
        self.assertIsNotNone(sim.view("me")["today_pnl"])

    def test_record_equity_requires_every_held_quote_at_or_after_the_close(self):
        self.fill("me", "buy", 10)
        self.clock = "15:05:00"
        self.quote(11, stamp="20261106145959")
        sim.record_equity()
        self.assertEqual(db.q("SELECT * FROM equity WHERE account='me'"), [])
        self.assertFalse(sim.view("me")["performance"]["close_eligible"])
        self.assertTrue(sim.view("ai")["performance"]["close_eligible"])  # Cash-only at the actual close.
        self.quote(11, stamp="20261106150000")
        sim.record_equity()
        self.assertTrue(sim.view("me")["performance"]["close_eligible"])
        self.assertEqual(len(db.q("SELECT * FROM equity WHERE account='me'")), 1)

    def test_weekend_cumulative_marks_previous_close_and_day_pnl_unknown(self):
        self.fill("me", "buy", 10)
        self.day = "2026-11-07"
        self.quote(11, stamp="20261106150003")
        account = sim.view("me")
        self.assertIsNone(account["today_pnl"])
        self.assertIsNotNone(account["total_pnl"])
        self.assertEqual(account["performance"]["valuation_asof"], "2026-11-06")
        self.assertEqual(account["performance"]["valuation_freshness"], "prior_close")
        self.assertEqual(account["performance"]["verified_equity"], account["equity"])

    def test_stale_quotes_are_not_cumulative_marks_during_live_trading(self):
        self.fill("me", "buy", 10, day=self.prior)
        self.quote(11, stamp="20261105150003")
        with patch.object(market, "is_trading_day", return_value=True):
            account = sim.view("me")
        self.assertIsNone(account["today_pnl"])
        self.assertIsNone(account["total_pnl"])
        self.assertEqual(account["performance"]["valuation_freshness"], "unknown")

    def test_recorded_close_resolves_old_cash_gap_for_subsequent_dates_only(self):
        day = self.day
        self.day = self.prior
        self.fill("me", "buy", 10)
        with db.tx() as c:
            c.execute("UPDATE accounts SET cash=cash-5 WHERE id='me'")
        self.assertEqual(sim.view("me")["daily_pnl_status"]["code"], "unreconciled_cash")
        self.clock = "15:05:00"
        sim.record_equity()
        self.assertIsNone(self.pnl("me"))  # An observed close cannot prove that day's historical cash flows.
        self.day = day
        self.clock = "14:30:00"
        self.quote(12, prev=10)
        self.assertEqual(self.pnl("me"), 200.0)

    def test_funding_is_not_pnl_and_does_not_rewrite_previous_equity(self):
        self.fill("me", "buy", 10, day=self.prior)
        day = self.day
        self.day = self.prior
        self.clock = "15:05:00"
        self.quote(10, stamp="20261105150000")
        sim.record_equity()
        old_curve = db.q("SELECT * FROM equity WHERE account='me'")
        self.assertEqual(len(old_curve), 1)
        self.day = day
        self.clock = "14:30:00"
        self.quote(10)
        with patch.object(sim, "load_settings", return_value={"init_cash": 1500000}):
            sim.migrate_capital()
            sim.migrate_capital()
        self.assertEqual(self.pnl("me"), 0.0)
        self.assertEqual(db.q("SELECT * FROM equity WHERE account='me'"), old_curve)
        self.assertEqual(len(db.q("SELECT 1 FROM accounting_cash WHERE account='me' AND kind='funding'")), 1)

    def test_interest_is_an_idempotent_daily_expense(self):
        self.fill("ai", "sell", 10, qty=1000, day=self.prior)
        self.quote(10)
        sim.accrue_interest()
        sim.accrue_interest()
        interest = db.q("SELECT amount FROM accounting_cash WHERE account='ai' AND kind='interest'")
        self.assertEqual(len(interest), 1)
        self.assertLess(interest[0]["amount"], 0)
        self.assertEqual(self.pnl("ai"), round(interest[0]["amount"], 2))

    def test_plan_distinguishes_historical_cost_and_saved_creation_marks(self):
        from aq5 import accounting

        day = self.day + " 10:00:00"
        cfg = {"cash": 500.0, "holdings": [{"code": self.code, "qty": 100, "cost": 20.0, "price": 10.0}],
               "init_total": 2500.0, "started": day}
        with db.tx() as c:
            c.execute("INSERT INTO accounts(id,name,cash,init_cash,created) VALUES('plan','Plan',500,2500,?)", (self.day,))
            c.execute("INSERT INTO plan(id,created,cfg) VALUES(1,?,?)", (day, db.dumps(cfg)))
            c.execute("INSERT INTO positions(account,code,qty,cost,opened) VALUES('plan',?,100,20,?)", (self.code, day))
        self.quote(9.0, prev=11.0)
        account = sim.view("plan")
        self.assertEqual(account["today_pnl"], -100.0)
        self.assertEqual(account["performance"]["daily_basis"], "recorded_plan_inception_mark")
        p = accounting.plan_performance(cfg, account)
        self.assertEqual(p["historical_cost"]["pnl"], -1100.0)
        self.assertEqual(p["inception_mark"]["pnl"], -100.0)
        self.assertEqual(p["holding_comparison"]["status"]["code"], "not_frozen")
        del cfg["holdings"][0]["price"]
        self.assertIsNone(accounting.plan_performance(cfg, account)["inception_mark"]["pnl"])
        self.quotes.clear()
        p = accounting.plan_performance(cfg, sim.view("plan"))
        self.assertIsNone(p["historical_cost"]["pnl"])
        with db.tx() as c:
            c.execute("DELETE FROM positions WHERE account='plan'")
            c.execute("DELETE FROM accounts WHERE id='plan'")
            c.execute("DELETE FROM plan")

if __name__ == "__main__":
    unittest.main()
