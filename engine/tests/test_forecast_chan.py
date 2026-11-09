"""Real pinned-engine tests. Synthetic bars test causality, not profitability."""
import copy
import hashlib
import json
import math
import random
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aq5.forecast import chan

CN = timezone(timedelta(hours=8))


def fixture(n=360, *, timeframe="day"):
    base = datetime(2025, 1, 1, 15, tzinfo=CN)
    delta = timedelta(days=1) if timeframe == "day" else timedelta(minutes=int(timeframe[:-1]))
    rows = []
    for i in range(n):
        p = 50 + 8 * math.sin(i * .35) + 3 * math.sin(i * .08) + i * .015
        date = (base + delta * i).isoformat()
        rows.append({"date": date, "available_at": date, "open": p - .1, "close": p + .1,
                     "high": p + .3, "low": p - .3, "vol": 10000, "source": "synthetic-fixture",
                     "adjustment": "none", "adjustment_verified": True})
    return rows


def swing_fixture(seed):
    """Frozen deterministic swings, not mined price history or return evidence."""
    rng = random.Random(seed)
    rows, p = [], 100
    base = datetime(2025, 1, 1, 15, tzinfo=CN)
    for j in range(60):
        end = p + (1 if j % 2 == 0 else -1) * rng.uniform(3, 16)
        for k in range(7):
            x = p + (end - p) * k / 7
            t = (base + timedelta(days=len(rows))).isoformat()
            rows.append(dict(date=t, available_at=t, open=x, close=x, high=x + .1, low=x - .1,
                             vol=10000, source="synthetic-fixture", adjustment="none", adjustment_verified=True))
        p = end
    return rows


class ChanEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = fixture()
        cls.snapshot = chan.analyze("sh600000", "day", cls.rows, cls.rows[-1]["date"])

    def test_exact_pinned_upstream_bytes(self):
        manifest = json.loads((chan.VENDOR / "PROVENANCE.json").read_text("utf-8"))
        self.assertEqual(chan.verify_vendor()["commit"], chan.UPSTREAM_COMMIT)
        self.assertFalse(manifest["upstream_modified"])
        for name, expected in manifest["files"].items():
            self.assertEqual(hashlib.sha256((chan.VENDOR / name).read_bytes()).hexdigest(), expected)

    def test_real_engine_complete_structure_chain(self):
        result = self.snapshot
        self.assertTrue(result["quality"]["eligible"], result["quality"])
        for key in ("fractals", "strokes", "segments", "centers", "buy_sell_points"):
            self.assertGreater(len(result["structures"][key]), 0, key)
            self.assertTrue(any(x["confirmed"] for x in result["structures"][key]), key)
        self.assertTrue(all(math.isfinite(v) for v in result["features"].values()))
        self.assertEqual(chan.CONFIG["seg_algo"], "chan")
        self.assertFalse(chan.CONFIG["one_bi_zs"])
        self.assertLess(chan.CONFIG["divergence_rate"], 1)

    def test_confirmations_never_backdated(self):
        confirmed = 0
        delayed = 0
        for kind, values in self.snapshot["structures"].items():
            if kind in ("klines", "revisions"):
                continue
            for value in values:
                if value["confirmed"]:
                    confirmed += 1
                    occurred = value.get("end", value.get("at"))
                    self.assertGreaterEqual(value["confirmed_at"], occurred)
                    delayed += value["confirmed_at"] > occurred
        self.assertGreater(confirmed, 0)
        self.assertGreater(delayed, 0)

    def test_future_bars_and_revisions_do_not_change_old_snapshot(self):
        cut = 140
        asof = self.rows[cut - 1]["date"]
        prefix = chan.analyze("sh600000", "day", self.rows[:cut], asof)
        full = chan.analyze("sh600000", "day", self.rows, asof)
        self.assertEqual(prefix, full)
        correction = copy.deepcopy(self.rows[20])
        correction.update(open=60, close=60, high=61, low=59, available_at=self.rows[cut + 1]["date"])
        self.assertEqual(prefix, chan.analyze("sh600000", "day", self.rows + [correction], asof))

    def test_input_immutability_and_determinism(self):
        rows = fixture(90)
        original = copy.deepcopy(rows)
        one = chan.analyze("sh600000", "day", rows, rows[-1]["date"])
        two = chan.analyze("sh600000", "day", rows, rows[-1]["date"])
        self.assertEqual(one, two)
        self.assertEqual(rows, original)

    def test_missing_evidence_can_render_but_cannot_trade(self):
        rows = fixture(80)
        for row in rows:
            row.pop("source")
            row.pop("available_at")
            row.pop("adjustment_verified")
        result = chan.analyze("sh600000", "day", rows, rows[-1]["date"])
        self.assertFalse(result["quality"]["eligible"])
        self.assertFalse(result["confirmed"])
        for reason in ("source_unknown", "availability_unknown", "adjustment_unverified"):
            self.assertIn(reason, result["quality"]["reason"])
        self.assertTrue(result["structures"]["strokes"])

    def test_invalid_and_conflicting_input_block_execution(self):
        rows = fixture(70)
        duplicate = dict(rows[20], close=200)
        result = chan.analyze("sh600000", "day", rows + [duplicate], rows[-1]["date"])
        self.assertIn("invalid_bar", result["quality"]["reason"])
        duplicate = dict(rows[20], high=rows[20]["high"] + 1)
        result = chan.analyze("sh600000", "day", rows + [duplicate], rows[-1]["date"])
        self.assertIn("conflicting_revision", result["quality"]["reason"])

    def test_historical_fetch_does_not_invent_historical_confirmation(self):
        rows = fixture(80)
        acquired = rows[-1]["date"]
        for row in rows:
            row["available_at"] = acquired
        result = chan.analyze("sh600000", "day", rows, acquired)
        for value in result["structures"]["strokes"]:
            if value["confirmed"]:
                self.assertEqual(value["confirmed_at"], acquired)

    def test_inclusion_and_fractal_are_upstream_computed(self):
        rows = fixture(3)
        for row, values in zip(rows, [(10, 12, 9, 11), (10, 11, 9.5, 10.5), (12, 14, 11, 13)]):
            row.update(zip(("open", "high", "low", "close"), values))
        result = chan.analyze("sh600000", "day", rows, rows[-1]["date"])
        self.assertEqual([x["count"] for x in result["structures"]["klines"]], [2, 1])
        for row, p in zip(rows, [10, 13, 10]):
            row.update(open=p, close=p, high=p + .5, low=p - .5)
        result = chan.analyze("sh600000", "day", rows, rows[-1]["date"])
        self.assertEqual(result["structures"]["fractals"][0]["kind"], "top")
        self.assertEqual(result["structures"]["fractals"][0]["confirmed_at"], rows[-1]["date"])

    def test_all_periods_and_cross_level_conflict(self):
        results = []
        for tf in ("day", "60m", "15m", "5m"):
            rows = fixture(80, timeframe=tf)
            result = chan.analyze("sh600000", tf, rows, rows[-1]["date"])
            self.assertTrue(result["quality"]["eligible"], result["quality"])
            results.append(result)
        results[0]["features"]["chan_segment_direction"] = 1
        results[1]["features"]["chan_segment_direction"] = -1
        relation = chan.align_levels(results)
        self.assertEqual(relation["state"], "conflict")
        self.assertEqual(relation["direction"], 0)

    def test_segment_macd_area_extension_matches_signed_histograms(self):
        chan._load_engine()
        from Seg.Seg import CSeg
        from Common.CEnum import MACD_ALGO
        from types import SimpleNamespace
        units = [SimpleNamespace(idx=i, macd=SimpleNamespace(macd=v), next=None)
                 for i, v in enumerate([1, -3, -2, 4])]
        for a, b in zip(units, units[1:]):
            a.next = b
        fake = SimpleNamespace(get_begin_klu=lambda: units[0], get_end_klu=lambda: units[-1],
                               is_down=lambda: True, is_up=lambda: False)
        self.assertAlmostEqual(CSeg.cal_macd_metric(fake, MACD_ALGO.AREA, False), 5.0000001)

    def test_point_in_time_adjustment_is_required(self):
        rows = fixture(80)
        for row in rows:
            row["adjustment"] = "pit"
            row["adjustment_asof"] = "2030-01-01"
        result = chan.analyze("sh600000", "day", rows, rows[-1]["date"])
        self.assertIn("adjustment_not_point_in_time", result["quality"]["reason"])

    def test_virtual_structures_not_encoded_as_trade_features(self):
        result = self.snapshot
        points = [p for p in result["structures"]["buy_sell_points"] if p["confirmed"]]
        if points:
            last = points[-1]
            self.assertEqual(result["features"]["chan_buy_point"], float(last["side"] == "buy"))
            self.assertGreaterEqual(result["features"]["chan_point_age_bars"], 0)
        self.assertTrue(any(not s["confirmed"] for s in result["structures"]["strokes"]))

    def test_all_buy_sell_point_families_run_through_pinned_engine(self):
        observed = set()
        for seed, expected in ((0, {"1", "1p", "2", "2s", "3b"}), (9, {"1", "2", "2s", "3a", "3b"})):
            rows = swing_fixture(seed)
            result = chan.analyze("sh600000", "day", rows, rows[-1]["date"])
            self.assertTrue(result["quality"]["eligible"], result["quality"])
            kinds = {t for p in result["structures"]["buy_sell_points"] for t in p["types"]}
            self.assertEqual(kinds, expected)
            observed |= kinds
            self.assertTrue(any(p["macd_area"] > 0 for p in result["structures"]["buy_sell_points"]))
            self.assertTrue(any(e["event"] == "superseded" for e in result["structures"]["revisions"]))
        self.assertEqual(observed, {"1", "1p", "2", "2s", "3a", "3b"})

    def test_center_extension_and_structure_revision_are_retained(self):
        centers = self.snapshot["structures"]["centers"]
        self.assertTrue(any(c["extensions"] >= 3 for c in centers))
        events = self.snapshot["structures"]["revisions"]
        self.assertTrue(any(e["event"] == "confirmed" for e in events))
        self.assertEqual(len({e["event_id"] for e in events}), len(events))


if __name__ == "__main__":
    unittest.main()
