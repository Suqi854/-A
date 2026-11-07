"""Bounded public-source checks, isolated from cloud state and trading workers.

Run: python -B -m aq5.source_diagnostics --output /data/source-diagnostics.json
AQ5_DATA must name a separate diagnostics directory. No account/state download,
AI request, notification, scheduler or cloud.run code is imported here.
"""
from __future__ import annotations

import argparse
import gzip
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

CN = timezone(timedelta(hours=8))
CODES = ("sh600000", "sh600519")
CHECKS = ("qt", "sqt", "ths", "sina", "eastmoney", "tdx", "integrated")
HOSTS = ("116.205.183.150:7709", "116.205.171.132:7709")
MAX_BYTES = 2 * 1024 * 1024
ENGINE = Path(__file__).resolve().parents[1]


class InvalidData(ValueError):
    pass


class DependencyFailure(RuntimeError):
    pass


def _now():
    return datetime.now(CN)


def _number(value, positive=False):
    if isinstance(value, bool):
        raise InvalidData("boolean_number")
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise InvalidData("invalid_number") from exc
    if not math.isfinite(value) or value < 0 or (positive and value <= 0):
        raise InvalidData("invalid_number")
    return value


def _stamp(value):
    try:
        if isinstance(value, datetime):
            result = value
        elif isinstance(value, str) and len(value) == 14 and value.isascii() and value.isdecimal():
            result = datetime.strptime(value, "%Y%m%d%H%M%S")
        elif isinstance(value, str):
            result = datetime.fromisoformat(value)
        else:
            raise ValueError
        return result.replace(tzinfo=CN) if result.tzinfo is None else result.astimezone(CN)
    except (ValueError, TypeError) as exc:
        raise InvalidData("invalid_source_timestamp") from exc


def _source_date(value, asof):
    result = _stamp(value).date()
    if result > asof.date() or (asof.date()-result).days > 14:
        raise InvalidData("future_or_old_source_date")
    return result


def _ohlc(row, keys=("open", "high", "low", "close")):
    opening, high, low, closing = (_number(row[k], True) for k in keys)
    if not low <= min(opening, closing) <= max(opening, closing) <= high:
        raise InvalidData("invalid_ohlc")


def validate_quotes(rows, *, attributed=False, asof=None):
    asof = asof or _now()
    if not isinstance(rows, dict) or set(rows) != set(CODES):
        raise InvalidData("quote_codes_incomplete_or_unexpected")
    samples = []
    for code in CODES:
        row = rows[code]
        if row.get("code") != code or not row.get("name"):
            raise InvalidData("quote_identity_mismatch")
        _number(row.get("price"), True)
        _number(row.get("prev"), True)
        _ohlc(row, ("open", "high", "low", "price"))
        stamp = _stamp(row.get("quote_time") or row.get("time"))
        _source_date(stamp, asof)
        if stamp > asof:
            raise InvalidData("future_quote_timestamp")
        sample = {"code": code, "price": row["price"], "source_time": stamp.isoformat()}
        if attributed:
            if row.get("source") not in ("腾讯", "通达信") or not row.get("time_basis"):
                raise InvalidData("quote_source_missing_or_unexpected")
            received = _stamp(row.get("received_at"))
            if not stamp <= received <= asof:
                raise InvalidData("invalid_receipt_order")
            sample.update(source=row["source"], received_at=received.isoformat(), time_basis=row["time_basis"])
        samples.append(sample)
    return samples


def validate_bars(rows, asof=None):
    asof = asof or _now()
    if not isinstance(rows, list) or len(rows) < 3:
        raise InvalidData("fewer_than_three_bars")
    stamps = []
    for row in rows:
        stamps.append(_source_date(row.get("date"), asof))
        _ohlc(row)
        _number(row.get("vol"))
    if stamps != sorted(set(stamps)):
        raise InvalidData("duplicate_or_unordered_bars")
    return {"valid_count": len(rows), "latest_source_date": stamps[-1].isoformat(),
            "last_close": rows[-1]["close"]}


def _http_get(url, referer="https://finance.qq.com", timeout=4, retries=0):
    """Single direct HTTPS request; no retries, provider cooldowns or DB events."""
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": referer,
                                                   "Accept-Encoding": "gzip"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=min(4, max(0.1, timeout))) as response:
        raw = response.read(MAX_BYTES+1)
        if len(raw) > MAX_BYTES:
            raise InvalidData("compressed_response_too_large")
        if response.headers.get("Content-Encoding", "").lower() == "gzip":
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as zipped:
                raw = zipped.read(MAX_BYTES+1)
        if len(raw) > MAX_BYTES:
            raise InvalidData("response_too_large")
        return raw


def _safe_data_dir():
    raw = os.environ.get("AQ5_DATA")
    if not raw:
        raise InvalidData("AQ5_DATA_must_be_an_isolated_directory")
    path = Path(raw).resolve()
    if path.drive.upper() == "C:":
        raise InvalidData("diagnostic_files_must_not_use_C_drive")
    if any((path / name).exists() for name in ("settings.json", "aq5.sqlite3", "private.enc", "state.enc")):
        raise InvalidData("refusing_account_or_state_data_directory")
    return path


def _load_market():
    data = _safe_data_dir()
    os.environ["AQ5_MOCK"] = "0"
    os.environ["ELTDX_DATA_DIR"] = str(data / "tdx-cache")
    from . import market
    if market.MOCK:
        raise InvalidData("mock_market_not_allowed")
    market.http_get = _http_get
    market._cache.clear()
    return market


def _tdx_record(record, market_day, bar_day, asof):
    code = str(record.get("exchange", "")) + str(record.get("code", ""))
    if code not in CODES or record.get("market_id") != 1 or bar_day != market_day:
        raise InvalidData("tdx_symbol_or_date_mismatch")
    _ohlc(record, ("open_price", "high_price", "low_price", "last_price"))
    _number(record.get("last_close_price"), True)
    _number(record.get("total_hand"), True)
    _number(record.get("amount"), True)
    best = []
    for key, reverse in (("buy_levels", True), ("sell_levels", False)):
        levels = record.get(key)
        if not isinstance(levels, list) or len(levels) != 5:
            raise InvalidData("tdx_incomplete_depth")
        prices = []
        for level in levels:
            price, volume = _number(level.get("price")), _number(level.get("volume"))
            if volume and not price:
                raise InvalidData("tdx_volume_without_price")
            if price:
                prices.append(price)
        if prices != sorted(prices, reverse=reverse):
            raise InvalidData("tdx_depth_order")
        best.append(_number(levels[0]["price"]))
    if not any(best) or (all(best) and best[0] > best[1]):
        raise InvalidData("tdx_empty_or_crossed_book")
    raw = record.get("update_time_raw")
    if type(raw) is not int or not 0 < raw <= 235959:
        raise InvalidData("tdx_invalid_depth_time")
    try:
        stamp = datetime(market_day.year, market_day.month, market_day.day,
                         raw // 10000, raw // 100 % 100, raw % 100, tzinfo=CN)
    except ValueError as exc:
        raise InvalidData("tdx_invalid_depth_time") from exc
    _source_date(stamp, asof)
    if stamp > asof:
        raise InvalidData("tdx_future_quote")
    return {"code": code, "price": record["last_price"], "source_time": stamp.isoformat(),
            "source_market_date": market_day.isoformat(), "daily_bar_date": bar_day.isoformat(),
            "time_basis": "tdx_depth_hhmmss_with_verified_market_date", "depth_levels": 5,
            "eligibility": "not_assessed_raw_probe_only"}


def _tdx_check():
    data = _safe_data_dir()
    os.environ["ELTDX_DATA_DIR"] = str(data / "tdx-cache")
    try:
        from eltdx import TdxClient
        from eltdx.serialization import to_jsonable
    except (ImportError, OSError) as exc:
        raise DependencyFailure("eltdx_native_dependency_unavailable") from exc
    attempts = []
    for host in HOSTS:
        client = None
        try:
            client = TdxClient(host=host, timeout=0.8, probe_hosts=False, heartbeat_interval=None,
                               server_count=1, connections_per_server=1, runtime_workers=1)
            client.connect()
            handshake = client.session.handshake()
            days = (handshake.server_date_1, handshake.server_date_2)
            if not all(type(value) is date for value in days) or days[0] != days[1]:
                raise InvalidData("tdx_market_date_conflict")
            page = to_jsonable(client.quotes.get_depth(list(CODES)))
            received = _now()
            rows = page.get("records", [])
            keys = [str(row.get("exchange", ""))+str(row.get("code", "")) for row in rows]
            if len(keys) != len(set(keys)) or set(keys) != set(CODES):
                raise InvalidData("tdx_missing_or_duplicate_codes")
            samples = []
            for code, row in zip(keys, rows):
                bars = client.bars.get(code, period="day", count=1, adjust="none").bars
                if len(bars) != 1:
                    raise InvalidData("tdx_missing_daily_bar")
                samples.append(_tdx_record(row, days[0], bars[0].time.date(), received))
            attempts.append({"host": host, "status": "pass", "valid_count": len(samples),
                             "source_dates": [value.isoformat() for value in days], "quotes": samples})
            break  # One validated node is sufficient; the second is a bounded fallback.
        except Exception as exc:
            attempts.append({"host": host, "status": "fail", "valid_count": 0, "error": _error(exc)})
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
    passed = [item for item in attempts if item["status"] == "pass"]
    return {"valid_count": max((item["valid_count"] for item in passed), default=0), "attempts": attempts,
            "passed_nodes": len(passed), "scope": "raw_transport_parse_depth_and_source_date_only",
            "note": "Price precision initialization also reads bounded public security metadata; no host scan.",
            "intraday_latency_verified": False}


def _error(exc):
    return {"kind": "dependency_error" if isinstance(exc, DependencyFailure) else type(exc).__name__,
            "reason": str(exc)[:180] if isinstance(exc, (InvalidData, DependencyFailure)) else type(exc).__name__}


def run_worker(name):
    started = time.monotonic()
    result = {"name": name, "status": "fail", "valid_count": 0}
    try:
        if name == "tdx":
            details = _tdx_check()
        else:
            market = _load_market()
            if name in ("qt", "sqt"):
                raw = _http_get("https://" + name + ".gtimg.cn/q=" + ",".join(CODES))
                samples = validate_quotes(market._parse_tencent(raw.decode("gbk", "strict")))
                details = {"valid_count": len(samples), "quotes": samples}
            elif name in ("ths", "sina", "eastmoney"):
                readers = {"ths": market._kline_ths, "sina": market._kline_sina, "eastmoney": market._kline_eastmoney}
                details = validate_bars(readers[name](CODES[0], 3))
                details.update(code=CODES[0], adjustment="unadjusted" if name == "sina" else "forward_adjusted")
            elif name == "integrated":
                normal = validate_quotes(market.quotes(list(CODES)), attributed=True)
                fast = validate_quotes(market.quotes_fast(list(CODES)), attributed=True)
                info = market.tdx_status()
                details = {"valid_count": len(normal)+len(fast), "normal_quotes": normal, "fast_quotes": fast,
                           "tdx": {key: info.get(key) for key in ("available", "enabled", "state", "selected",
                                   "rejected", "last_error", "rejection_reasons")},
                           "note": "TDX not selected can mean older/invalid eligible data; the separate raw TDX check decides transport availability."}
            else:
                raise InvalidData("unknown_check")
        result.update(details=details, valid_count=details.get("valid_count", 0))
        if type(result["valid_count"]) is not int or result["valid_count"] <= 0:
            raise InvalidData("empty_or_invalid_data")
        result["status"] = "pass"
    except Exception as exc:
        result["error"] = _error(exc)
    result["elapsed_ms"] = round((time.monotonic()-started)*1000, 2)
    return result


def run_all(checks=CHECKS, worker_timeout=20, total_timeout=145):
    started = time.monotonic()
    results = []
    child_env = {**os.environ, "AQ5_MOCK": "0", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    for name in checks:
        remaining = total_timeout-(time.monotonic()-started)
        result = {"name": name, "status": "fail", "valid_count": 0}
        try:
            if remaining <= 0:
                raise subprocess.TimeoutExpired(name, total_timeout)
            child = subprocess.run([sys.executable, "-B", "-m", "aq5.source_diagnostics", "--worker", name],
                                   cwd=ENGINE, env=child_env, capture_output=True, text=True, encoding="utf-8",
                                   timeout=min(worker_timeout, remaining), check=False)
            parsed = json.loads(child.stdout)
            if not isinstance(parsed, dict) or parsed.get("name") != name:
                raise InvalidData("invalid_worker_report")
            result = parsed
            count = result.get("valid_count")
            if child.returncode != 0 or result.get("status") != "pass" or type(count) is not int or count <= 0:
                result.update(status="fail", exit_code=child.returncode)
                result.setdefault("error", {"kind": "worker_failure", "reason": "nonzero_exit_or_no_valid_rows"})
        except subprocess.TimeoutExpired:
            result["error"] = {"kind": "timeout", "reason": "bounded_worker_timeout"}
        except Exception as exc:
            result["error"] = _error(exc)
        results.append(result)
    try:
        version = importlib.metadata.version("eltdx")
    except importlib.metadata.PackageNotFoundError:
        version = "not_installed"
    return {"schema": "aq5-public-source-diagnostics-1", "generated_at": _now().isoformat(),
            "commit": os.environ.get("GITHUB_SHA", "local"), "python": sys.version.split()[0],
            "eltdx_version": version, "symbols": list(CODES), "checks": results,
            "status": "pass" if results and all(item["status"] == "pass" for item in results) else "fail",
            "elapsed_ms": round((time.monotonic()-started)*1000, 2), "intraday_latency_verified": False,
            "scope": "public_quote_and_small_daily_sample; no account/state/AI/notification/trading"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", choices=CHECKS, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        result = run_worker(args.worker)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] == "pass" else 1
    if args.output is None:
        parser.error("--output is required")
    _safe_data_dir()
    output = args.output.resolve()
    if output.drive.upper() == "C:":
        raise InvalidData("diagnostic_files_must_not_use_C_drive")
    report = run_all()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {key: report[key] for key in ("status", "commit", "python", "eltdx_version", "elapsed_ms")}
    summary["checks"] = [{key: item.get(key) for key in ("name", "status", "valid_count", "error")}
                         for item in report["checks"]]
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
