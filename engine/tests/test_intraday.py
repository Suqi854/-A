"""Source-time regression tests for intraday shadow evaluation and opportunity state."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ["AQ5_DATA"] = tempfile.mkdtemp(dir=os.environ["AQ5_DATA"])
os.environ["AQ5_MOCK"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aq5 import db, intraday, market, signals  # noqa: E402
from aq5.config import CN_TZ  # noqa: E402

CODE = "sh600000"
T = datetime(2026, 11, 6, 10, 35, tzinfo=CN_TZ)


def market_at(t=T, **fields):
    return {"observed_at": t.isoformat(), "speed5": 0.02, "up_ratio": 0.52, "risk_off": False, **fields}


def quotes_at(t=T, seconds=60, *, amount=True):
    rows = []
    for i in range(seconds // 3 + 1):
        ts = t - timedelta(seconds=seconds - i * 3)
        price = 9.94 + i * 0.0015
        volume = 10000 + i * 100
        row = {"quote_time": ts.isoformat(), "received_at": ts.isoformat(), "source": "test-provider",
               "price": price, "vol": volume, "pct": 0.1}
        if amount:
            row["amount"] = volume * 9.95
        rows.append(row)
    return rows


def m1_at(t=T, count=65):
    return {"period": "m1", "bar_time": "close", "source": "minute-fixture",
            "rows": [{"date": (t - timedelta(minutes=count - 1 - i)).isoformat(),
                      "close": 10 + 0.02 * ((i + 4) % 5 - 2), "vol": 2000 if i == count - 1 else 1000}
                     for i in range(count)]}


def m5_at(t=T):
    return {"period": "m5", "bar_time": "close", "source": "structure-fixture",
            "rows": [{"date": (t - timedelta(minutes=i * 5)).isoformat(), "close": 10,
                      "low": 9.96, "high": 10.04, "vol": 5000} for i in range(7, -1, -1)]}


class Intraday(unittest.TestCase):
    def setUp(self):
        intraday.init()
        with db.tx() as conn:
            for table in ("intraday_samples", "intraday_latest", "intraday_revisions", "intraday_coverage"):
                conn.execute(f"DELETE FROM {table}")

    def evaluate(self, q=None, bars=None, context=None, variant="balanced", t=T):
        return intraday.evaluate(CODE, quotes_at(t) if q is None else q, m1_at(t) if bars is None else bars,
                                 market_at(t) if context is None else context, variant, t)

    def test_confirmed_direction_uses_closed_macd_price_volume_and_structural_target(self):
        result = self.evaluate(bars={"m1": m1_at(), "m5": m5_at()})
        self.assertEqual((result["phase"], result["side"], result["setup"]), ("confirmed", "buy", "range_edge"))
        self.assertEqual(result["features"]["structure"]["basis"], "m5_ohlc")
        self.assertIn("rolling_vwap_alignment", result["evidence"])
        self.assertIn("minute_macd_turn", result["evidence"])
        self.assertIn(result["features"]["macd"]["hist_shape"], ("expanding", "contracting", "flat"))
        self.assertLess(result["invalidation"], result["entry_zone"][0])
        self.assertEqual(result["target"], 10.04)
        self.assertGreater(result["expires_at"], result["evaluated_at"])

    def test_market_risk_is_immediate_and_low_volume_waits(self):
        risk = self.evaluate(q=[], bars={}, context=market_at(speed5=-0.5))
        self.assertEqual((risk["phase"], risk["setup"], risk["side"]), ("invalid", "risk_exit", None))
        bars = m1_at()
        bars["rows"][-1]["vol"] = 100
        weak = self.evaluate(bars=bars)
        self.assertNotEqual(weak["phase"], "confirmed")
        self.assertEqual(weak["reason"], "waiting_for_direction_volume_and_macd")
        stale = self.evaluate(context=market_at(index_data_at=(T - timedelta(minutes=3)).isoformat()))
        self.assertEqual(stale["reason"], "market_context_unavailable")

    def test_capped_window_independent_of_quote_count_and_variants_share_input(self):
        short = quotes_at()[10:]
        fast = self.evaluate(q=short, variant="fast")
        balanced = self.evaluate(q=short)
        self.assertIsNotNone(fast["features"]["w30"])
        self.assertIsNone(fast["features"]["w60"])
        self.assertEqual(balanced["reason"], "quote_window_incomplete")
        gap = quotes_at()[:13] + quotes_at()[18:]
        self.assertEqual(self.evaluate(q=gap)["reason"], "quote_window_incomplete")
        reset = quotes_at()
        for i, row in enumerate(reset[-6:]):
            row.update(vol=10 + i, amount=100 + i)
        self.assertEqual(self.evaluate(q=reset)["reason"], "quote_window_incomplete")

    def test_future_unfinished_and_after_available_cannot_leak_into_macd(self):
        baseline = self.evaluate()
        bars = m1_at()
        bars["rows"].append({"date": (T + timedelta(minutes=1)).isoformat(), "close": 990, "vol": 1000000})
        self.assertEqual(self.evaluate(bars=bars)["features"]["macd"], baseline["features"]["macd"])
        bars["available_at"] = (T + timedelta(seconds=1)).isoformat()
        self.assertEqual(self.evaluate(bars=bars)["reason"], "macd_warming_up")
        shifted = m1_at(count=60)
        shifted["bar_time"] = "start"
        self.assertEqual(self.evaluate(bars=shifted)["reason"], "macd_warming_up")

    def test_minute_gap_and_m5_only_are_not_macd_input(self):
        bars = m1_at()
        del bars["rows"][50]
        self.assertEqual(self.evaluate(bars=bars)["missing"], ["60_consecutive_closed_m1"])
        self.assertEqual(self.evaluate(bars=m5_at())["reason"], "macd_warming_up")
        bars = m1_at()
        bars["rows"] += [{**bars["rows"][-4], "close": 11}]
        self.assertEqual(self.evaluate(bars=bars)["reason"], "macd_warming_up")

    def test_amount_unavailable_does_not_invent_vwap_or_ohlc(self):
        result = self.evaluate(q=quotes_at(amount=False))
        self.assertIsNone(result["features"]["vwap"])
        self.assertIsNone(result["features"]["w60"]["vwap"])
        self.assertNotIn("rolling_vwap_alignment", result["evidence"])
        self.assertEqual(result["features"]["structure"]["basis"], "m1_closes")

    def test_ingestion_rejects_stale_duplicate_and_late_and_resets_on_sources(self):
        q = quotes_at()[-1]
        self.assertTrue(intraday.ingest(CODE, q, T, q["source"]))
        self.assertFalse(intraday.ingest(CODE, q, T, q["source"]))
        self.assertFalse(intraday.ingest(CODE, quotes_at()[-2], T, q["source"]))
        self.assertFalse(intraday.ingest(CODE, q, T + timedelta(seconds=13), q["source"]))
        next_t = T + timedelta(seconds=3)
        other = quotes_at(next_t)[-1]
        self.assertTrue(intraday.ingest(CODE, other, next_t, "second-provider"))
        stat = intraday.status()
        self.assertEqual((stat["accepted"], stat["duplicate"], stat["late"], stat["stale"], stat["resets"]),
                         (2, 1, 1, 1, 1))
        self.assertEqual(stat["gaps"], {"source_changed": 1})

    def test_lunch_and_next_session_reset_instead_of_splicing_samples(self):
        for t in (T, T.replace(hour=13, minute=0), T + timedelta(days=3)):
            q = quotes_at(t)[-1]
            self.assertTrue(intraday.ingest(CODE, q, t, q["source"]))
        self.assertEqual(intraday.status()["resets"], 2)
        afternoon = T.replace(hour=13, minute=0, second=30)
        quote = quotes_at(afternoon)[-1]
        self.assertEqual(self.evaluate(q=[*quotes_at(T), quote], t=afternoon)["reason"], "macd_warming_up")

    def test_afternoon_macd_keeps_real_completed_morning_bars_without_quote_splicing(self):
        morning = m1_at(T.replace(hour=11, minute=30), count=60)
        afternoon = T.replace(hour=13, minute=1)
        morning["rows"].append({"date": afternoon.isoformat(), "close": 10.01, "vol": 1700})
        result = self.evaluate(q=quotes_at(afternoon), bars=morning, t=afternoon)
        self.assertEqual(result["features"]["macd"]["samples"], 61)
        self.assertEqual(result["features"]["w60"]["seconds"], 60)
        last = quotes_at(afternoon, seconds=15)
        sparse = self.evaluate(q=quotes_at(T) + last, bars=morning, t=afternoon)
        self.assertEqual(sparse["reason"], "quote_window_incomplete")
        late = T.replace(hour=13, minute=3)
        morning["rows"][-1]["date"] = late.isoformat()
        missing = self.evaluate(q=quotes_at(late), bars=morning, t=late)
        self.assertEqual(missing["reason"], "macd_warming_up")

    def test_opportunity_revisions_are_stable_and_closed_records_immutable(self):
        quotes = quotes_at()
        for q in quotes[:-1]:
            intraday.ingest(CODE, q, q["received_at"], q["source"])
        one = intraday.observe(CODE, quotes[-1], m1_at(), market_at(), T)
        duplicate = intraday.observe(CODE, quotes[-1], m1_at(), market_at(), T + timedelta(milliseconds=600))
        self.assertEqual(one, duplicate)
        self.assertEqual(set(one), {"fast", "balanced", "slow"})
        original = one["balanced"]
        self.assertEqual(original["phase"], "confirmed")
        self.assertEqual(original["revision"], 1)
        intraday.init()
        self.assertEqual(signals.intraday_latest([CODE])[CODE], one)
        later = T + timedelta(seconds=3)
        risk = intraday.observe(CODE, quotes_at(later)[-1], m1_at(), market_at(later, risk_off=True), later)["balanced"]
        self.assertEqual((risk["phase"], risk["opportunity_id"], risk["revision"]),
                         ("invalid", original["opportunity_id"], 2))
        payloads = db.q("SELECT payload FROM intraday_revisions WHERE opportunity_id=? ORDER BY revision",
                        (original["opportunity_id"],))
        self.assertEqual(json.loads(payloads[0]["payload"]), original)
        intraday.observe(CODE, quotes[-1], m1_at(), market_at(), T)
        self.assertEqual(intraday.latest()[CODE]["balanced"], risk)
        self.assertEqual(signals.intraday_status()["last_eval"], later.isoformat())

    def test_shared_runtime_returns_source_quote_without_second_fetch(self):
        q = quotes_at()[-1]
        with patch.object(intraday, "now", return_value=T), patch.object(intraday, "_refresh_inputs"), \
             patch.object(market, "quotes_fast", return_value={CODE: q}) as fetch:
            result = intraday.runtime_tick([CODE, CODE], return_quotes=True)
        fetch.assert_called_once_with([CODE])
        self.assertEqual(result["asof"], T.isoformat())
        self.assertEqual(result["quotes"][CODE]["source"], "模拟")
        self.assertEqual(set(result["signals"][CODE]), {"fast", "balanced", "slow"})


if __name__ == "__main__":
    unittest.main(verbosity=2)

