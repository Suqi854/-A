"""Offline regression tests for strict, bounded public-source diagnostics."""
from __future__ import annotations

import contextlib
from datetime import date, datetime, timedelta
import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ENGINE = Path(__file__).resolve().parents[1]
TEST_TEMP = ENGINE / "tmp" / "source-diagnostics-tests"
TEST_TEMP.mkdir(parents=True, exist_ok=True)
for key in ("TEMP", "TMP", "TMPDIR"):
    os.environ[key] = str(TEST_TEMP)
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True
sys.path.insert(0, str(ENGINE))
from aq5 import source_diagnostics as diag  # noqa: E402

NOW = datetime(2026, 11, 8, 1, tzinfo=diag.CN)
DAY = date(2026, 11, 6)


def quotes():
    return {code: {"code": code, "name": "公开样例", "price": 10, "prev": 9.9, "open": 9.95,
                   "high": 10.1, "low": 9.9, "time": "20261106150000", "source": "腾讯",
                   "received_at": NOW.isoformat(), "time_basis": "tencent_quote_timestamp"}
            for code in diag.CODES}


def bars():
    return [{"date": f"2026-11-0{day}", "open": 10, "close": 10.1, "high": 10.2, "low": 9.9, "vol": 100}
            for day in (4, 5, 6)]


def depth():
    return {"exchange": "sh", "market_id": 1, "code": "600000", "last_price": 10,
            "last_close_price": 9.9, "open_price": 9.95, "high_price": 10.1, "low_price": 9.9,
            "total_hand": 100, "amount": 100000, "update_time_raw": 153003,
            "buy_levels": [{"price": 9.99-i*.01, "volume": 10} for i in range(5)],
            "sell_levels": [{"price": 10+i*.01, "volume": 10} for i in range(5)]}


class SourceValidation(unittest.TestCase):
    def test_complete_real_quote_structure_is_required(self):
        result = diag.validate_quotes(quotes(), attributed=True, asof=NOW)
        self.assertEqual(len(result), 2)
        for rows in ({}, {diag.CODES[0]: quotes()[diag.CODES[0]]}):
            with self.assertRaises(diag.InvalidData):
                diag.validate_quotes(rows, asof=NOW)
        for field, value in (("price", float("nan")), ("prev", 0), ("time", "20261109150000"),
                             ("code", "sz000001"), ("source", None), ("received_at", None)):
            rows = quotes()
            rows[diag.CODES[0]][field] = value
            with self.subTest(field=field), self.assertRaises(diag.InvalidData):
                diag.validate_quotes(rows, attributed=True, asof=NOW)

    def test_empty_malformed_and_unordered_bars_fail(self):
        self.assertEqual(diag.validate_bars(bars(), NOW)["valid_count"], 3)
        cases = [[], bars()[:2], list(reversed(bars())), bars()+[bars()[-1]]]
        invalid = bars()
        invalid[0]["low"] = 20
        cases.append(invalid)
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(diag.InvalidData):
                diag.validate_bars(rows, NOW)

    def test_tdx_uses_verified_source_date_not_wall_clock(self):
        row = diag._tdx_record(depth(), DAY, DAY, NOW)
        self.assertEqual(row["source_time"], "2026-11-06T15:30:03+08:00")
        self.assertEqual(row["eligibility"], "not_assessed_raw_probe_only")
        for market_day, bar_day in ((DAY, date(2026, 11, 5)), (date(2026, 11, 9), date(2026, 11, 9))):
            with self.assertRaises(diag.InvalidData):
                diag._tdx_record(depth(), market_day, bar_day, NOW)
        row = depth()
        row["sell_levels"] = []
        with self.assertRaises(diag.InvalidData):
            diag._tdx_record(row, DAY, DAY, NOW)

    def test_zero_count_and_native_import_failure_are_failures(self):
        with patch.object(diag, "_tdx_check", return_value={"valid_count": 0, "attempts": []}):
            self.assertEqual(diag.run_worker("tdx")["status"], "fail")
        with patch.object(diag, "_tdx_check", side_effect=diag.DependencyFailure("missing_native")):
            result = diag.run_worker("tdx")
        self.assertEqual(result["error"]["kind"], "dependency_error")

    def test_tdx_stops_after_a_valid_node_and_uses_second_only_on_failure(self):
        serialization = ModuleType("eltdx.serialization")
        serialization.to_jsonable = lambda value: value
        package = ModuleType("eltdx")
        first, second = depth(), depth()
        second["code"] = "600519"
        client = SimpleNamespace(connect=Mock(), close=Mock(),
            session=SimpleNamespace(handshake=lambda: SimpleNamespace(server_date_1=DAY, server_date_2=DAY)),
            quotes=SimpleNamespace(get_depth=lambda _: {"records": [first, second]}),
            bars=SimpleNamespace(get=lambda *a, **k: SimpleNamespace(bars=[SimpleNamespace(time=NOW-timedelta(days=2))])))
        for outcomes, expected_calls in (([client], 1), ([TimeoutError("offline"), client], 2)):
            package.TdxClient = Mock(side_effect=outcomes)
            with self.subTest(expected_calls=expected_calls), \
                 patch.dict(sys.modules, {"eltdx": package, "eltdx.serialization": serialization}), \
                 patch.object(diag, "_safe_data_dir", return_value=TEST_TEMP), patch.object(diag, "_now", return_value=NOW):
                result = diag._tdx_check()
            self.assertEqual(result["valid_count"], 2)
            self.assertEqual(package.TdxClient.call_count, expected_calls)


class BoundedExecution(unittest.TestCase):
    def child(self, name="qt", count=2, status="pass", returncode=0):
        return subprocess.CompletedProcess([], returncode, json.dumps({"name": name, "status": status, "valid_count": count}), "")

    def test_all_sources_are_required_and_children_have_deadlines(self):
        def run(args, **kwargs):
            self.assertLessEqual(kwargs["timeout"], 20)
            self.assertEqual(kwargs["env"]["AQ5_MOCK"], "0")
            return self.child(args[-1])
        with patch.object(diag.subprocess, "run", side_effect=run) as child:
            report = diag.run_all()
        self.assertEqual(report["status"], "pass")
        self.assertEqual(child.call_count, 7)

    def test_zero_rows_nonzero_exit_timeout_or_non_json_cannot_pass(self):
        cases = [self.child(count=0), self.child(returncode=1), self.child(status="fail"),
                 subprocess.CompletedProcess([], 0, "", ""), subprocess.CompletedProcess([], 0, "not-json", "")]
        for child in cases:
            with self.subTest(child=child), patch.object(diag.subprocess, "run", return_value=child):
                self.assertEqual(diag.run_all(checks=("qt",))["status"], "fail")
        with patch.object(diag.subprocess, "run", side_effect=subprocess.TimeoutExpired("probe", 20)):
            report = diag.run_all(checks=("qt",))
        self.assertEqual(report["checks"][0]["error"]["kind"], "timeout")

    def test_mock_or_private_data_directory_is_not_used(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory:
            Path(directory, "settings.json").write_text("{}", encoding="utf-8")
            with patch.dict(os.environ, {"AQ5_DATA": directory}), self.assertRaises(diag.InvalidData):
                diag._safe_data_dir()

    def test_gzip_response_is_checked_after_decompression(self):
        response = io.BytesIO(gzip.compress(b"x" * 101))
        response.headers = {"Content-Encoding": "gzip"}
        class Opener:
            def open(self, request, timeout):
                return contextlib.closing(response)
        with patch.object(diag, "MAX_BYTES", 100), patch.object(diag.urllib.request, "build_opener", return_value=Opener()):
            with self.assertRaisesRegex(diag.InvalidData, "response_too_large"):
                diag._http_get("https://qt.gtimg.cn/q=sh600000")

    def test_main_writes_report_and_returns_nonzero_on_failed_source(self):
        report = {"status": "fail", "commit": "abc", "python": "3.13", "eltdx_version": "3.3.5", "elapsed_ms": 1,
                  "checks": [{"name": "tdx", "status": "fail", "valid_count": 0}]}
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory:
            output = Path(directory) / "report.json"
            with patch.dict(os.environ, {"AQ5_DATA": str(Path(directory) / "data")}), \
                 patch.object(diag, "run_all", return_value=report), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(diag.main(["--output", str(output)]), 1)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "fail")


if __name__ == "__main__":
    unittest.main(verbosity=2)
