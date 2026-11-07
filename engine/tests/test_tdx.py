"""Offline contracts for the optional TDX quote source; never contacts providers."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TEMP = ROOT / "temp"
TEMP.mkdir(exist_ok=True)
for key in ("TEMP", "TMP", "TMPDIR"):
    os.environ[key] = str(TEMP)
os.environ["AQ5_DATA"] = tempfile.mkdtemp(prefix="tdx-test-", dir=TEMP)
os.environ["AQ5_MOCK"] = "0"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))

from aq5 import db, intraday, market, tdx  # noqa: E402

CN = timezone(timedelta(hours=8))
DAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 6, 10, 0, 5, tzinfo=CN)
CODE = "sh600000"


def anchor(**updates):
    row = {"code": CODE, "name": "浦发银行", "prev": 10.0, "price": 10.1,
           "up_limit": 11.0, "dn_limit": 9.0, "time": "20261106100000",
           "source": "腾讯", "received_at": "2026-11-06T10:00:01+08:00"}
    row.update(updates)
    return row


def depth(**updates):
    row = {"exchange": "sh", "market_id": 1, "code": "600000",
           "update_time_raw": 100004, "last_price": 10.1, "last_close_price": 10.0,
           "open_price": 10.02, "high_price": 10.2, "low_price": 9.9,
           "total_hand": 12345, "amount": 12400000.5,
           "inside_dish": 1, "outer_disc": 999999,
           "buy_levels": [{"price": round(10.09-i*0.01, 2), "volume": 20+i} for i in range(5)],
           "sell_levels": [{"price": round(10.10+i*0.01, 2), "volume": 30+i} for i in range(5)]}
    row.update(updates)
    return row


def quote(**updates):
    row = {"code": CODE, "name": "浦发银行", "price": 10.1, "prev": 10.0,
           "open": 10.02, "high": 10.2, "low": 9.9, "pct": 1.0,
           "vol": 1234500, "amount": 12400000.5, "up_limit": 11.0, "dn_limit": 9.0,
           "time": "20261106100004", "quote_time": "2026-11-06T10:00:04+08:00",
           "source": "通达信", "received_at": "2026-11-06T10:00:04.500+08:00",
           "time_basis": "tdx_depth_hhmmss_with_verified_market_date",
           "metadata_source": "腾讯（同交易日证券名称及涨跌停价）"}
    row.update(updates)
    return row


def tencent_wire(codes):
    rows = []
    for code in codes:
        fields = ["0"] * 70
        fields[0:6] = ["1", "样例", code[2:], "10.1", "10.0", "10.02"]
        fields[6] = "12345"
        fields[30:35] = ["20261106100004", "0.1", "1.0", "10.2", "9.9"]
        fields[37] = "1240"
        fields[47:49] = ["11.0", "9.0"]
        rows.append('v_' + code + '="' + '~'.join(fields) + '";')
    return '\n'.join(rows).encode('gbk')


class AdapterContract(unittest.TestCase):
    def convert(self, row=None, *, dates=None, bar_date=None, metadata=None, received=None):
        return tdx.normalize_record(row if row is not None else depth(),
                                    dates if dates is not None else (DAY, DAY),
                                    bar_date if bar_date is not None else DAY,
                                    metadata if metadata is not None else anchor(),
                                    received if received is not None else NOW)

    def test_quote_uses_one_depth_record_and_explicit_units(self):
        row = self.convert()
        self.assertEqual(row["code"], CODE)
        self.assertEqual((row["price"], row["open"], row["high"], row["low"]), (10.1, 10.02, 10.2, 9.9))
        self.assertEqual(row["vol"], 1234500)
        self.assertEqual(row["amount"], 12400000.5)
        self.assertEqual(row["bids"][0], [10.09, 20])
        self.assertEqual(row["asks"][0], [10.10, 30])
        self.assertEqual(len(row["bids"]), 5)
        self.assertEqual(len(row["asks"]), 5)
        self.assertIsNone(row.get("inner"))
        self.assertIsNone(row.get("outer"))
        self.assertEqual(row["source"], "通达信")
        self.assertEqual(row["metadata_source"], "腾讯（同交易日证券名称及涨跌停价）")
        self.assertEqual(row["time"], "20261106100004")
        self.assertEqual(datetime.fromisoformat(row["received_at"]), NOW)
        self.assertEqual(row["time_basis"], "tdx_depth_hhmmss_with_verified_market_date")

    def test_weekend_does_not_relabel_friday_as_saturday(self):
        row = self.convert(depth(update_time_raw=153003),
                           metadata=anchor(time="20261106150000"),
                           received=datetime(2026, 11, 7, 23, 12, tzinfo=CN))
        self.assertEqual(row["time"], "20261106153003")
        self.assertEqual(datetime.fromisoformat(row["quote_time"]).date(), DAY)

    def test_conflicting_dates_and_missing_bar_date_fail_closed(self):
        cases = [((DAY, date(2026, 11, 5)), DAY),
                 ((DAY, DAY), date(2026, 11, 5)),
                 ((None, DAY), DAY), ((DAY, DAY), None)]
        for dates, bar_date in cases:
            with self.subTest(dates=dates, bar_date=bar_date), self.assertRaises(ValueError):
                tdx.normalize_record(depth(), dates, bar_date, anchor(), NOW)

    def test_snapshot_time_encoding_cannot_substitute_for_depth_clock(self):
        row = depth()
        row.pop("update_time_raw")
        row["time_raw"] = 15299298
        with self.assertRaises(ValueError):
            self.convert(row)

    def test_invalid_hhmmss_is_rejected(self):
        for value in (None, True, -1, 0, 100060, 106000, 240000, 15299298):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.convert(depth(update_time_raw=value))

    def test_stale_future_or_previous_day_intraday_quote_is_rejected(self):
        for moment in (NOW+timedelta(seconds=12), NOW-timedelta(seconds=2), NOW+timedelta(days=3)):
            with self.subTest(received=moment), self.assertRaises(ValueError):
                self.convert(received=moment)

    def test_after_hours_data_still_requires_a_close_not_an_old_intraday_tick(self):
        with self.assertRaises(ValueError):
            self.convert(received=datetime(2026, 11, 7, 23, tzinfo=CN))

    def test_bad_prices_volumes_and_depth_fail_closed(self):
        rows = [depth(last_price=float("nan")), depth(last_price=0), depth(last_close_price=0),
                depth(high_price=10.05), depth(low_price=10.15), depth(total_hand=-1),
                depth(amount=float("inf")), depth(buy_levels=depth()["buy_levels"][:4]),
                depth(sell_levels=[])]
        for row in rows:
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.convert(row)

    def test_no_usable_book_is_rejected_but_one_sided_limit_book_is_allowed(self):
        empty_side = [{"price": 0, "volume": 0} for _ in range(5)]
        with self.assertRaises(ValueError):
            self.convert(depth(buy_levels=empty_side, sell_levels=empty_side))
        for missing in ("buy_levels", "sell_levels"):
            with self.subTest(missing=missing):
                row = self.convert(depth(**{missing: empty_side}))
                self.assertEqual(row["price"], 10.1)
        with self.assertRaises(ValueError):
            self.convert(depth(total_hand=0))

    def test_metadata_must_match_symbol_day_previous_close_and_limits(self):
        rows = [anchor(code="sz000001"), anchor(time="20261105100000"),
                anchor(time="20261106100005"), anchor(prev=9.9), anchor(name=""),
                anchor(up_limit=None), anchor(dn_limit=0), anchor(up_limit=8.0)]
        for row in rows:
            with self.subTest(metadata=row), self.assertRaises(ValueError):
                self.convert(metadata=row)

    def test_known_suspension_cannot_be_removed_by_a_new_provider(self):
        with self.assertRaisesRegex(ValueError, "suspended_security"):
            self.convert(metadata=anchor(suspended=True))

    def test_normalization_does_not_mutate_provider_or_metadata_records(self):
        raw, metadata = depth(), anchor()
        before = copy.deepcopy((raw, metadata))
        self.convert(raw, metadata=metadata)
        self.assertEqual((raw, metadata), before)


class MarketRouting(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(market, "MOCK", False),
                        patch.object(market, "now", return_value=NOW),
                        patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden"))]
        for item in self.patches:
            item.start()
        market._cache.clear()
        market._LAST.clear()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        market._cache.clear()
        market._LAST.clear()

    def test_fast_quotes_keep_tdx_and_fetch_only_uncovered_codes(self):
        rows = {CODE: quote()}
        with patch.object(market, "_tdx_quotes", return_value=rows), \
             patch.object(market, "http_get", return_value=tencent_wire(["sz000001", "hkHSI"])) as http:
            result = market.quotes_fast([CODE, "sz000001", "hkHSI"])
        self.assertEqual(set(result), {CODE, "sz000001", "hkHSI"})
        self.assertEqual(result[CODE]["source"], "通达信")
        self.assertEqual(result[CODE]["received_at"], rows[CODE]["received_at"])
        requested = http.call_args.args[0].split("q=", 1)[1].split(",")
        self.assertEqual(set(requested), {"sz000001", "hkHSI"})

    def test_unavailable_tdx_keeps_existing_tencent_route(self):
        with patch.object(market, "_tdx_quotes", return_value={}), \
             patch.object(market, "http_get", return_value=tencent_wire([CODE])):
            result = market.quotes_fast([CODE])
        self.assertEqual(result[CODE]["price"], 10.1)
        self.assertEqual(result[CODE]["source"], "腾讯")

    def test_regular_quotes_use_tdx_only_for_tencent_missing_symbols(self):
        with patch.object(market, "http_get", return_value=tencent_wire(["sz000001"])), \
             patch.object(market, "_tdx_quotes", return_value={CODE: quote()}) as supplementary:
            result = market.quotes([CODE, "sz000001"])
        self.assertEqual(set(result), {CODE, "sz000001"})
        self.assertEqual(set(supplementary.call_args.args[0]), {CODE})
        self.assertEqual(result["sz000001"]["source"], "腾讯")

    def test_cache_read_does_not_rewrite_quote_receipt(self):
        row = quote()
        with patch.object(market, "_tdx_quotes", return_value={CODE: row}), \
             patch.object(market, "http_get", side_effect=AssertionError("covered code must not fetch Tencent")), \
             patch.object(market.time, "time", return_value=1000.0):
            first = market.quotes_fast([CODE])
        with patch.object(market.time, "time", return_value=1001.0), \
             patch.object(market, "quotes_fast", side_effect=AssertionError("recent cache should be reused")):
            second = market.quotes_recent([CODE])
        self.assertEqual(second[CODE]["received_at"], first[CODE]["received_at"])

    def test_tdx_trade_gate_rechecks_current_age_and_limits(self):
        self.assertTrue(market.tdx_trade_eligible(quote(), NOW))
        self.assertFalse(market.tdx_trade_eligible(quote(), NOW+timedelta(seconds=12)))
        self.assertFalse(market.tdx_trade_eligible(quote(up_limit=None), NOW))
        self.assertFalse(market.tdx_trade_eligible(quote(time=None, quote_time=None), NOW))

    def test_stale_tdx_cache_is_replaced_by_tencent_without_waiting_for_ttl(self):
        with patch.object(market, "_tdx_quotes", side_effect=[{CODE: quote()}, {}]), \
             patch.object(market, "http_get", return_value=tencent_wire([CODE])) as http:
            with patch.object(market.time, "time", return_value=1000.0):
                first = market.quotes_fast([CODE])
            self.assertEqual(first[CODE]["source"], "通达信")
            with patch.object(market, "now", return_value=NOW+timedelta(seconds=13)), \
                 patch.object(market.time, "time", return_value=1000.2):
                second = market.quotes_fast([CODE])
            self.assertEqual(second[CODE]["source"], "腾讯")
            self.assertEqual(http.call_count, 1)

    def test_disabled_provider_never_starts_tdx_fetch(self):
        with patch.object(market, "load_settings", return_value={"net": {"tdx_enabled": False}}), \
             patch.object(tdx, "quotes", side_effect=AssertionError("disabled provider must not fetch")):
            self.assertEqual(market._tdx_quotes([CODE]), {})

    def test_slow_other_symbol_refreshes_only_expired_tdx_code_once(self):
        clock = [NOW]
        def request(url, *args, **kwargs):
            codes = url.split("q=", 1)[1].split(",")
            if "sqt.gtimg.cn" in url:
                self.assertEqual(codes, ["sz000001"])
                clock[0] = NOW+timedelta(seconds=13)
            else:
                self.assertIn("qt.gtimg.cn", url)
                self.assertEqual(codes, [CODE])
                self.assertEqual(kwargs.get("timeout"), 2)
                self.assertEqual(kwargs.get("retries"), 0)
            return tencent_wire(codes).replace(b"20261106100004", b"20261106100018")
        with patch.object(market, "now", side_effect=lambda: clock[0]), \
             patch.object(market, "_tdx_quotes", return_value={CODE: quote()}) as tdx_call, \
             patch.object(market, "http_get", side_effect=request) as http:
            result = market.quotes_fast([CODE, "sz000001"])
        self.assertEqual(set(result), {CODE, "sz000001"})
        self.assertEqual(result[CODE]["source"], "腾讯")
        self.assertEqual(result["sz000001"]["source"], "腾讯")
        self.assertEqual(http.call_count, 2)
        self.assertEqual(tdx_call.call_count, 1)


class SimulationSafety(unittest.TestCase):
    def test_pending_simulated_order_cannot_fill_with_stale_or_incomplete_tdx(self):
        from aq5 import sim
        pending = {"id": 1, "status": "pending", "code": CODE, "side": "buy", "kind": "market"}
        rows = [quote(time="20261106095950", quote_time="2026-11-06T09:59:50+08:00"),
                quote(up_limit=None)]
        with patch.object(db, "q1", return_value=pending), patch.object(sim, "can_trade_now", return_value=True), \
             patch.object(market, "now", return_value=NOW), patch.object(sim, "load_settings", return_value={"slippage": 0}), \
             patch.object(sim, "_execute", return_value="executed") as execute:
            for row in rows:
                with self.subTest(row=row):
                    self.assertNotEqual(sim.try_fill(1, row), "executed")
            execute.assert_not_called()
            self.assertEqual(sim.try_fill(1, quote()), "executed")
            execute.assert_called_once()

    def test_legacy_features_do_not_compare_counters_across_sources(self):
        from aq5 import hf
        hf._buf.clear()
        with patch.object(hf.time, "time", side_effect=[1000, 1001, 1002, 1003]):
            first = hf._feat(CODE, quote(source="腾讯", price=10.0, outer=100, inner=50))
            second = hf._feat(CODE, quote(source="腾讯", price=10.1, outer=120, inner=55))
            switched = hf._feat(CODE, quote(price=10.2, outer=None, inner=None))
            reverted = hf._feat(CODE, quote(source="腾讯", price=10.3, outer=130, inner=60))
        self.assertEqual((first["n"], second["n"], switched["n"], reverted["n"]), (1, 2, 1, 1))
        self.assertEqual(switched["flow"], 0)
        self.assertEqual(switched["m30"], 0)
        self.assertEqual(reverted["flow"], 0)
        hf._buf.clear()


class ProviderBudget(unittest.TestCase):
    def setUp(self):
        self.previous = (tdx._future, tdx._retry_at, tdx._client, tdx._host_index,
                         copy.deepcopy(tdx._state), dict(tdx._dates))
        tdx._future, tdx._retry_at, tdx._client, tdx._host_index = None, 0.0, None, 0
        tdx._dates.clear()

    def tearDown(self):
        tdx._future, tdx._retry_at, tdx._client, tdx._host_index, state, dates = self.previous
        tdx._state.clear()
        tdx._state.update(state)
        tdx._dates.clear()
        tdx._dates.update(dates)

    def test_missing_dependency_does_not_start_a_network_worker(self):
        with patch.object(tdx, "available", return_value=False), patch.object(tdx, "_fetch") as fetch:
            self.assertEqual(tdx.quotes([CODE], {CODE: anchor()}), {})
        fetch.assert_not_called()

    def test_missing_metadata_or_unsupported_symbol_never_connects(self):
        with patch.object(tdx, "_connect", side_effect=AssertionError("must not connect")):
            self.assertEqual(tdx.quotes([CODE], {}), {})
            self.assertEqual(tdx.quotes(["hkHSI"], {"hkHSI": anchor(code="hkHSI")}), {})

    def test_connection_failure_cools_down_and_preserves_fallback(self):
        with patch.object(tdx, "available", return_value=True), \
             patch.object(tdx, "_connect", side_effect=ImportError("dependency unavailable")) as connect:
            self.assertEqual(tdx.quotes([CODE], {CODE: anchor()}), {})
            self.assertEqual(tdx.quotes([CODE], {CODE: anchor()}), {})
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(tdx._state["state"], "unavailable")
        self.assertGreater(tdx._retry_at, time.monotonic())

    def test_timeout_returns_promptly_and_busy_request_is_not_queued(self):
        release = threading.Event()
        def wait_for_release(*_):
            release.wait(1)
            return {}
        with patch.object(tdx, "available", return_value=True), \
             patch.object(tdx, "_fetch", side_effect=wait_for_release) as fetch:
            try:
                begin = time.monotonic()
                self.assertEqual(tdx.quotes([CODE], {CODE: anchor()}, timeout=0.01), {})
                self.assertLess(time.monotonic()-begin, 0.3)
                self.assertEqual(tdx.quotes([CODE], {CODE: anchor()}, timeout=0.01), {})
                self.assertEqual(fetch.call_count, 1)
            finally:
                release.set()
                if tdx._future is not None:
                    tdx._future.result(timeout=1)

    def test_wrong_or_duplicate_returned_symbols_are_not_accepted(self):
        serialization = ModuleType("eltdx.serialization")
        serialization.to_jsonable = lambda value: value
        client = SimpleNamespace(
            session=SimpleNamespace(handshake=lambda: SimpleNamespace(server_date_1=DAY, server_date_2=DAY)),
            quotes=SimpleNamespace(get_depth=lambda _: {"records": [depth(), depth()]}),
            close=lambda: None)
        with patch.dict(sys.modules, {"eltdx.serialization": serialization}), \
             patch.object(tdx, "_connect", return_value=client):
            self.assertEqual(tdx._fetch([CODE], {CODE: anchor()}), {})
        self.assertEqual(tdx._state["state"], "unavailable")

    def test_mismatched_bar_date_is_not_cached_and_next_current_bar_recovers(self):
        serialization = ModuleType("eltdx.serialization")
        serialization.to_jsonable = lambda value: value
        old = SimpleNamespace(bars=[SimpleNamespace(time=NOW-timedelta(days=1))])
        current = SimpleNamespace(bars=[SimpleNamespace(time=NOW)])
        from unittest.mock import Mock
        bars = Mock(side_effect=[old, current])
        client = SimpleNamespace(
            session=SimpleNamespace(handshake=lambda: SimpleNamespace(server_date_1=DAY, server_date_2=DAY)),
            quotes=SimpleNamespace(get_depth=lambda _: {"records": [depth()]}),
            bars=SimpleNamespace(get=bars), close=lambda: None)
        metadata = {CODE: anchor(time="20261106090000")}
        with patch.dict(sys.modules, {"eltdx.serialization": serialization}), \
             patch.object(tdx, "_connect", return_value=client), patch.object(tdx, "now", return_value=NOW):
            self.assertEqual(tdx._fetch([CODE], metadata), {})
            self.assertEqual(tdx._dates, {})
            recovered = tdx._fetch([CODE], metadata)
        self.assertEqual(set(recovered), {CODE})
        self.assertEqual(recovered[CODE]["time"], "20261106100004")
        self.assertEqual(bars.call_count, 2)
        self.assertEqual(set(tdx._dates.values()), {DAY})


class IntradayProvenance(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(intraday.SCHEMA)

    def tearDown(self):
        self.conn.close()

    def test_source_switch_breaks_comparison_window(self):
        old = quote(source="腾讯", time="20261106100000", quote_time="2026-11-06T10:00:00+08:00",
                    received_at="2026-11-06T10:00:01+08:00")
        self.assertTrue(intraday._ingest(self.conn, CODE, old, NOW-timedelta(seconds=4), "腾讯"))
        self.assertTrue(intraday._ingest(self.conn, CODE, quote(), NOW, "通达信"))
        coverage = self.conn.execute("SELECT * FROM intraday_coverage WHERE code=?", (CODE,)).fetchone()
        self.assertEqual(coverage["last_gap"], "source_changed")
        self.assertEqual(coverage["resets"], 1)
        history = [json.loads(row[0]) for row in self.conn.execute("SELECT payload FROM intraday_samples ORDER BY quote_time")]
        series = intraday._quote_series(CODE, history, NOW)
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0][1]["source"], "通达信")

    def test_stale_or_future_quote_is_not_ingested(self):
        for stamp in ("2026-11-06T09:59:50+08:00", "2026-11-06T10:00:06+08:00",
                      "2026-11-05T10:00:04+08:00", None):
            with self.subTest(stamp=stamp):
                self.assertFalse(intraday._ingest(self.conn, CODE, quote(quote_time=stamp, time=None), NOW, "通达信"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM intraday_samples").fetchone()[0], 0)

    def test_runtime_tick_retains_provider_attribution_and_receipt(self):
        raw = quote()
        with patch.object(intraday, "MOCK", False), patch.object(intraday, "now", return_value=NOW), \
             patch.object(intraday, "_refresh_inputs"), patch.object(intraday, "observe", return_value={}) as observe, \
             patch.object(market, "quotes_fast", return_value={CODE: raw}):
            result = intraday.runtime_tick([CODE], return_quotes=True)
        self.assertEqual(result["quotes"][CODE]["source"], "通达信")
        self.assertEqual(result["quotes"][CODE]["received_at"], raw["received_at"])
        self.assertEqual(observe.call_args.kwargs["source"], "通达信")

    def test_observe_uses_original_receipt_and_rejects_invalid_receipts(self):
        from contextlib import contextmanager
        @contextmanager
        def transaction():
            yield self.conn
        with patch.object(db, "tx", transaction):
            intraday.observe(CODE, quote(received_at="2026-11-06T10:00:04+08:00"), {}, {}, NOW)
            stored = json.loads(self.conn.execute("SELECT payload FROM intraday_samples").fetchone()[0])
            self.assertEqual(stored["received_at"], "2026-11-06T10:00:04+08:00")
            for receipt in (None, "invalid", "2026-11-06T10:00:06+08:00"):
                before = self.conn.execute("SELECT accepted FROM intraday_coverage WHERE code=?", (CODE,)).fetchone()[0]
                intraday.observe(CODE, quote(received_at=receipt), {}, {}, NOW)
                after = self.conn.execute("SELECT accepted FROM intraday_coverage WHERE code=?", (CODE,)).fetchone()[0]
                self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
